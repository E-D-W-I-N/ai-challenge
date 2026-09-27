"""Durable reminder jobs. Only this MCP server writes their SQLite database.

Deadlines make jobs eligible, never successful. The application claims a job,
executes its originating chat, and reports the actual outcome with a claim token.
Legacy unbound reminders are retained but cannot execute in an arbitrary chat.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sqlite3
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent
CLAIM_SECONDS = 300
SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    due_at REAL NOT NULL,
    every REAL,
    context_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    fired INTEGER NOT NULL DEFAULT 0,
    token TEXT,
    claimed_at REAL,
    error TEXT NOT NULL DEFAULT ''
)
"""
STATES = {"pending": "ждёт", "running": "выполняется", "done": "сработало",
          "failed": "ошибка"}


def db_path() -> Path:
    raw = os.environ.get("REMIND_DB_PATH")
    return Path(raw) if raw else ROOT / "data" / "reminders.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    # Existing day-18 databases keep ids, text and first deadlines intact.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(reminders)")}
    for name, spec in {"context_id": "TEXT NOT NULL DEFAULT ''",
                       "status": "TEXT NOT NULL DEFAULT 'pending'",
                       "fired": "INTEGER NOT NULL DEFAULT 0", "token": "TEXT",
                       "claimed_at": "REAL", "error": "TEXT NOT NULL DEFAULT ''"}.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE reminders ADD COLUMN {name} {spec}")
    conn.commit()
    return conn


def add_reminder(text: str, in_seconds: float, every: float | None = None, *,
                 path: Path | None = None, now: float | None = None,
                 context_id: str = "") -> int:
    now = time.time() if now is None else now
    with contextlib.closing(_connect(path or db_path())) as conn:
        cur = conn.execute(
            "INSERT INTO reminders (text, due_at, every, context_id) VALUES (?, ?, ?, ?)",
            (text, now + in_seconds, every, context_id),
        )
        conn.commit()
        return cur.lastrowid


def list_reminders(*, path: Path | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    with contextlib.closing(_connect(path or db_path())) as conn:
        # An interrupted claim is not retried: external actions may have run.
        conn.execute("UPDATE reminders SET status='failed', error=?, token=NULL "
                     "WHERE status='running' AND claimed_at + ? <= ?",
                     ("исполнение прервано; автоматического повтора нет", CLAIM_SECONDS, now))
        conn.commit()
        items = [dict(row) for row in conn.execute(
            "SELECT id,text,due_at,every,context_id,status,fired,error FROM reminders ORDER BY id"
        )]
    for item in items:
        item["state"] = STATES[item["status"]] if item["context_id"] else "не привязано к чату"
    return {"items": items, "total": len(items),
            "waiting": sum(i["status"] == "pending" and bool(i["context_id"]) for i in items),
            "unbound": sum(not i["context_id"] for i in items),
            "fired": sum(i["fired"] > 0 for i in items),
            "running": sum(i["status"] == "running" for i in items),
            "failed": sum(i["status"] == "failed" for i in items)}


def cancel_reminder(reminder_id: int, *, path: Path | None = None,
                    context_id: str = "") -> bool:
    with contextlib.closing(_connect(path or db_path())) as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id = ?" +
                           (" AND context_id = ?" if context_id else ""),
                           (reminder_id, context_id) if context_id else (reminder_id,))
        conn.commit()
        return cur.rowcount > 0


def claim_reminder(rid: int, token: str, context_id: str, *, check: bool = False,
                   path: Path | None = None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with contextlib.closing(_connect(path or db_path())) as conn:
        if check:
            return conn.execute("SELECT 1 FROM reminders WHERE id=? AND context_id=? "
                                "AND token=? AND status='running' AND claimed_at + ? > ?",
                                (rid, context_id, token, CLAIM_SECONDS, now)).fetchone() is not None
        cur = conn.execute("UPDATE reminders SET status='running',token=?,claimed_at=? "
                           "WHERE id=? AND context_id=? AND context_id!='' "
                           "AND status='pending' AND due_at <= ? AND NOT EXISTS "
                           "(SELECT 1 FROM reminders AS active WHERE "
                           "active.context_id=reminders.context_id AND active.status='running')",
                           (token, now, rid, context_id, now))
        conn.commit()
        return cur.rowcount == 1


def finish_reminder(rid: int, token: str, error: str = "", *,
                    path: Path | None = None, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    with contextlib.closing(_connect(path or db_path())) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM reminders WHERE id=? AND token=? "
                           "AND status='running'", (rid, token)).fetchone()
        if row is None:
            return False
        due = row["due_at"]
        status = "failed" if error else "done"
        if not error and row["every"]:
            # Fixed cadence; skip missed slots, never overlap or catch up in a burst.
            due += (math.floor(max(0, now - due) / row["every"]) + 1) * row["every"]
            status = "pending"
        conn.execute("UPDATE reminders SET status=?,due_at=?,fired=fired+?,error=?,"
                     "token=NULL,claimed_at=NULL WHERE id=? AND token=?",
                     (status, due, int(not error), error, rid, token))
        conn.commit()
        return True


server = FastMCP("remind", host=os.environ.get("REMIND_HOST", "127.0.0.1"),
                 port=int(os.environ.get("REMIND_PORT", "8001")))


@server.tool(description="Запланировать выполнение задачи text в этом чате через in_seconds секунд; "
             "every повторяет задачу каждые N секунд. Сейчас только подтверждение; "
             "действия и итог выполняются автоматически после срока.")
def remind(text: str, in_seconds: float, every: float | None = None, context_id: str = "") -> str:
    if not text.strip():
        return "пустой текст: напоминать не о чем"
    if not math.isfinite(in_seconds) or in_seconds < 0:
        return "отрицательная или бесконечная задержка: напоминание в прошлом не завести"
    if every is not None and (not math.isfinite(every) or every <= 0):
        return "период обязан быть больше нуля и конечным"
    now = time.time()
    rid = add_reminder(text.strip(), in_seconds, every, context_id=context_id, now=now)
    again = f", повтор каждые {every:g} с" if every else ""
    return json.dumps({"scheduled": True, "id": rid, "due_at": now + in_seconds,
                       "message": f"напоминание №{rid} заведено: через {in_seconds:g} с{again} — «{text.strip()}»"},
                      ensure_ascii=False)


@server.tool(description="Список напоминаний с фактическими состояниями выполнения")
def reminders() -> str:
    return json.dumps(list_reminders(), ensure_ascii=False)


@server.tool(description="Снять напоминание по номеру в этом чате, остановить будущие повторы")
def cancel(id: int, context_id: str = "") -> str:
    if cancel_reminder(id, context_id=context_id):
        return f"напоминание №{id} снято"
    return f"напоминания №{id} нет"


# Application protocol, filtered out of the model/UI registry by McpManager.
@server.tool(meta={"host_only": True})
def _reminder_claim(id: int, token: str, context_id: str, check: bool = False) -> str:
    return json.dumps(claim_reminder(id, token, context_id, check=check))


@server.tool(meta={"host_only": True})
def _reminder_finish(id: int, token: str, error: str = "") -> str:
    return json.dumps(finish_reminder(id, token, error))


if __name__ == "__main__":
    server.run(transport="streamable-http")
