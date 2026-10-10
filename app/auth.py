"""Server-owned accounts, opaque sessions and request-scoped user registries."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
import getpass
import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from .config import ROOT
from .registry import AgentRegistry, _current
from .request_security import trusted_rag_request
from .store import Store

ACCOUNTS_PATH = ROOT / "data" / "accounts.db"
USERS_ROOT = ROOT / "data" / "users"
COOKIE = "agent_session"
SESSION_SECONDS = 7 * 86400
current_principal = ContextVar("principal", default=None)
_hasher = PasswordHasher()
_accounts = None
_attempts = {}
_cleanup_at = 0.0
_rate_lock = threading.Lock()
_password_gate = threading.Lock()


async def password_job(function, *args, **kwargs):
    # Admit before submitting to the executor; Argon2 work never queues there.
    if not _password_gate.acquire(blocking=False):
        raise HTTPException(429, "Password service is busy; try again later")
    job = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    def finished(task):
        _password_gate.release()
        if not task.cancelled():
            task.exception()  # Consume errors if the HTTP request disconnected.
    job.add_done_callback(finished)
    # Cancelling HTTP cannot stop Argon2; retain the bound until its worker ends.
    return await asyncio.shield(job)


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    role: str
    session_hash: str = ""

    def public(self):
        return {"id": self.id, "username": self.username, "role": self.role}


def rate_limit(key, count, seconds, *, record=True):
    global _cleanup_at
    now = time.monotonic()
    with _rate_lock:
        if now >= _cleanup_at:
            for name in list(_attempts):
                entries = _attempts[name]
                while entries and entries[0] <= now:
                    entries.popleft()
                if not entries:
                    del _attempts[name]
            _cleanup_at = now + 60
        entries = _attempts.get(key)
        if entries is not None:
            while entries and entries[0] <= now:
                entries.popleft()
            if len(entries) >= count:
                raise HTTPException(429, "Too many requests; try again later")
        if record:
            if entries is None:
                if len(_attempts) >= 4096:
                    raise HTTPException(429, "Too many requests; try again later")
                entries = _attempts[key] = deque()
            entries.append(now + seconds)


def password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 1024:
        raise HTTPException(422, "Password must contain 8..1024 characters")
    return value


def username(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 64 or any(ord(c) < 32 for c in value):
        raise HTTPException(422, "Username must contain 1..64 characters")
    return value.strip()


class Accounts:
    def __init__(self, path=ACCOUNTS_PATH, users_root=USERS_ROOT):
        self.path, self.users_root = Path(path), Path(users_root)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (
                hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires REAL NOT NULL);
        """)
        self.registries = {}
        self.loop = None

    def user(self, user_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def registry(self, user):
        with self.lock:
            if user["id"] not in self.registries:
                directory = self.users_root / user["id"]
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.registries[user["id"]] = AgentRegistry(store=Store(directory / "agents.db").init(), allow_tools=user["role"] == "admin")
            return self.registries[user["id"]]

    def admin_registry(self):
        with self.lock:
            user = self.db.execute("SELECT * FROM users WHERE role='admin' AND enabled=1 ORDER BY rowid LIMIT 1").fetchone()
            return self.registry(dict(user)) if user else None

    def create(self, name, secret, *, role="user", bootstrap=False):
        name, secret = username(name), password(secret)
        encoded = _hasher.hash(secret)
        with self.lock, self.db:
            if bootstrap and self.db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
                raise HTTPException(409, "Administrator already exists")
            try:
                user_id = str(uuid.uuid4())
                self.db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(?,?,?,?)", (user_id, name, encoded, role))
            except sqlite3.IntegrityError:
                raise HTTPException(409, "Username already exists") from None
        return {"id": user_id, "username": name, "role": role, "enabled": True}

    def authenticate(self, name, secret):
        with self.lock:
            user = self.db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
        try:
            accepted = user is not None and _hasher.verify(user["password_hash"], secret)
        except (VerificationError, InvalidHashError):
            accepted = False
        if not accepted or not user["enabled"]:
            raise HTTPException(401, "Invalid username or password")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock, self.db:
            # Reset/disable may have happened during password verification.
            fresh = self.db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not fresh["enabled"] or fresh["password_hash"] != user["password_hash"]:
                raise HTTPException(401, "Invalid username or password")
            self.db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            self.db.execute("INSERT INTO sessions VALUES(?,?,?)", (digest, user["id"], time.time() + SESSION_SECONDS))
        return token, Principal(user["id"], user["username"], user["role"], digest)

    def resolve(self, token):
        if not token or len(token) > 256:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.hash=? AND s.expires>? AND u.enabled=1", (digest, time.time())).fetchone()
        return Principal(row["id"], row["username"], row["role"], digest) if row else None

    def valid(self, principal):
        with self.lock:
            return bool(self.db.execute("SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.user_id=? AND s.expires>? AND u.enabled=1", (principal.session_hash, principal.id, time.time())).fetchone())

    def revoke(self, user_id, session_hash=None):
        with self.lock, self.db:
            if session_hash is None:
                self.db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            else:
                self.db.execute("DELETE FROM sessions WHERE hash=? AND user_id=?", (session_hash, user_id))
            registry = self.registries.get(user_id)
            if registry:
                for agent in registry.list():
                    if self.loop is not None and self.loop.is_running():
                        self.loop.call_soon_threadsafe(agent.cancel)
                    else:
                        agent.cancel()

    def update(self, user_id, values, *, principal=None, expected_hash=None):
        if not isinstance(values, dict) or not values or set(values) - {"password", "enabled"}:
            raise HTTPException(422, "Only password and enabled are accepted")
        encoded = _hasher.hash(password(values["password"])) if "password" in values else None
        if "enabled" in values and type(values["enabled"]) is not bool:
            raise HTTPException(422, "enabled must be boolean")
        with self.lock, self.db:
            user = self.user(user_id)
            if user is None:
                raise HTTPException(404, "User not found")
            if principal is not None and (not self.valid(principal) or user["password_hash"] != expected_hash):
                raise HTTPException(401, "Session expired; sign in again")
            if values.get("enabled") is False and user["role"] == "admin" and user["enabled"]:
                if self.db.execute("SELECT count(*) FROM users WHERE role='admin' AND enabled=1").fetchone()[0] <= 1:
                    raise HTTPException(409, "Cannot disable the only administrator")
            if encoded:
                self.db.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, user_id))
            if "enabled" in values:
                self.db.execute("UPDATE users SET enabled=? WHERE id=?", (values["enabled"], user_id))
            self.revoke(user_id)
            user = self.user(user_id)
            return {k: bool(user[k]) if k == "enabled" else user[k] for k in ("id", "username", "role", "enabled")}


def accounts():
    global _accounts
    if _accounts is None:
        _accounts = Accounts(ACCOUNTS_PATH, USERS_ROOT)
    return _accounts


class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        request = Request(scope, receive=receive)
        try:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                trusted_rag_request(request)
            if scope["path"] == "/api/auth/login":
                return await self.app(scope, receive, send)
            principal = accounts().resolve(request.cookies.get(COOKIE))
            if principal is None:
                raise HTTPException(401, "Authentication required")
            path = scope["path"]
            admin_only = (path.startswith(("/api/users", "/api/mcp", "/api/rag")) or "/reminders/" in path or (path == "/api/model-settings" and request.method != "GET"))
            if admin_only and principal.role != "admin":
                raise HTTPException(403, "Administrator access required")
            if request.method == "POST" and path.endswith(("/messages", "/regenerate")):
                rate_limit(("generation", principal.id), 10, 60)
        except HTTPException as error:
            return await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
        accounts().loop = asyncio.get_running_loop()
        registry = accounts().registry(principal.public())
        token = current_principal.set(principal)
        registry_token = _current.set(registry)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(registry_token)
            current_principal.reset(token)


router = APIRouter()


@router.get("/api/auth/me")
def me():
    return current_principal.get().public()


@router.post("/api/auth/login")
async def login(request: Request, response: Response, body: dict = Body(...)):
    name = username(body.get("username"))
    secret = body.get("password")
    if not isinstance(secret, str) or len(secret) > 1024:
        raise HTTPException(401, "Invalid username or password")
    keys = (("login-ip", request.client.host if request.client else ""), ("login-name", name.casefold()))
    for key in keys:
        rate_limit(key, 5, 900, record=False)
    try:
        token, principal = await password_job(accounts().authenticate, name, secret)
    except HTTPException as error:
        if error.status_code != 401:
            raise
        for key in keys:
            rate_limit(key, 5, 900)
        raise
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=request.url.scheme == "https", samesite="lax")
    return principal.public()


@router.post("/api/auth/logout", status_code=204)
def logout(response: Response):
    principal = current_principal.get()
    accounts().revoke(principal.id, principal.session_hash)
    response.delete_cookie(COOKIE)


@router.post("/api/auth/password", status_code=204)
async def change_password(response: Response, body: dict = Body(...)):
    principal = current_principal.get()
    user = accounts().user(principal.id)
    try:
        await password_job(_hasher.verify, user["password_hash"], body.get("current_password", ""))
    except (VerificationError, InvalidHashError, TypeError):
        raise HTTPException(403, "Current password is incorrect") from None
    await password_job(accounts().update, principal.id, {"password": body.get("new_password")}, principal=principal, expected_hash=user["password_hash"])
    response.delete_cookie(COOKIE)


@router.get("/api/users")
def list_users():
    with accounts().lock:
        return {"users": [{**dict(row), "enabled": bool(row["enabled"])} for row in accounts().db.execute("SELECT id,username,role,enabled FROM users ORDER BY rowid")]}


@router.post("/api/users", status_code=201)
async def create_user(body: dict = Body(...)):
    if set(body) != {"username", "password"}:
        raise HTTPException(422, "Provide username and password")
    return await password_job(accounts().create, body["username"], body["password"])


@router.patch("/api/users/{user_id}")
async def update_user(user_id: str, body: dict = Body(...)):
    return await password_job(accounts().update, user_id, body)


def main():
    parser = argparse.ArgumentParser(description="Explicit operator account bootstrap")
    parser.add_argument("command", choices=["bootstrap-admin"])
    parser.parse_args()
    name = input("Administrator username: ")
    secret = getpass.getpass("Password: ")
    if secret != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords differ")
    try:
        accounts().create(name, secret, role="admin", bootstrap=True)
    except HTTPException as error:
        raise SystemExit(error.detail) from None
    print("Administrator created")


if __name__ == "__main__":
    main()
