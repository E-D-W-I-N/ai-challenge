"""One private connection file; public settings contain no credential."""
import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path
from shared_models import Connection, load_connection, validate_url
from .config import ROOT

DEFAULT_CONNECTION_PATH = ROOT / "data" / "model-connection.json"
_lock = threading.Lock()


def connection_path(store=None):
    return DEFAULT_CONNECTION_PATH


def current_connection(store=None):
    if os.environ.get("INFERENCE_CAPACITY_FILE") is not None and not connection_path().exists():
        return Connection("http://127.0.0.1:11434/v1")
    return load_connection(connection_path())


def settings(store=None):
    return current_connection(store).public()


def validate_settings(payload, old):
    if not isinstance(payload, dict) or not payload or set(payload) - {"base_url", "api_key"}:
        raise ValueError("Only base_url and api_key are accepted")
    url = validate_url(payload.get("base_url", old.base_url))
    secret = payload.get("api_key", old.api_key)
    if not isinstance(secret, str) or len(secret) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
        raise ValueError("API key must be text without control characters")
    secret = secret.strip()
    return Connection(url, secret, old.revision + int((url, secret) != (old.base_url, old.api_key)))


def save_settings(payload, store=None, *, expected=None):
    with _lock:
        old = current_connection(store)
        if expected is not None and old != expected:
            from fastapi import HTTPException
            raise HTTPException(409, "Подключение уже изменилось. Повторите проверку.")
        saved = validate_settings(payload, old)
        path = connection_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".model-connection-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"base_url": saved.base_url, "api_key": saved.api_key, "revision": saved.revision}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return saved.public()


async def connect_settings(payload, store=None, *, commit=None):
    """Probe a fresh bounded catalogue before replacing the working connection."""
    from fastapi import HTTPException
    from .rag_models import models
    old = current_connection(store)
    candidate = validate_settings(payload, old)
    try:
        catalogue = await asyncio.wait_for(models(candidate, "all"), timeout=15)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Сервер не ответил за 15 секунд. Проверьте URL и доступность сервера.") from None
    saved = (save_settings(payload, store, expected=old) if commit is None
             else await commit(payload, store, expected=old))
    return {**saved, "total": catalogue["total"]}
