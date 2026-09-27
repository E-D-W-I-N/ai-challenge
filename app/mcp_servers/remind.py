"""Напоминания: инструмент с отложенным и периодическим выполнением.

«Сработало» нигде не пишется — вычисляется при чтении из `due_at`: строка
знает, когда ей сработать, и фонового цикла у сервера нет. Фоновый писатель
был бы вторым живым процессом у того же файла.

Хранение — своя база сервера (`data/reminders.db`), не стор приложения:
сервер — её единственный писатель, и схема стора она не трогает. В историю
чата сработавшее не пишется никогда: история строго попарная, человек видит
напоминания во вкладке «Инструменты».
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    text   TEXT NOT NULL,
    due_at REAL NOT NULL,
    every  REAL
)
"""
"""`every` NULL — одноразовое. Номера устойчивы: снятый не выдаётся заново."""


def db_path() -> Path:
    """Файл базы сервера: из REMIND_DB_PATH, по умолчанию — data/reminders.db."""
    raw = os.environ.get("REMIND_DB_PATH")
    return Path(raw) if raw else ROOT / "data" / "reminders.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def add_reminder(text: str, in_seconds: float, every: float | None = None, *,
                 path: Path | None = None, now: float | None = None) -> int:
    """Новая строка: номер из AUTOINCREMENT, первый срок — сейчас плюс задержка."""
    now = time.time() if now is None else now
    with contextlib.closing(_connect(path or db_path())) as conn:
        cur = conn.execute(
            "INSERT INTO reminders (text, due_at, every) VALUES (?, ?, ?)",
            (text, now + in_seconds, every),
        )
        conn.commit()
        return cur.lastrowid


def _item(row: tuple, now: float) -> dict:
    """Состояние строки на момент now: сколько раз сработала и когда следующая.

    У повторяющейся `due_at` при чтении — всегда следующее: срабатывания
    не пишутся в базу, а отсчитываются от первого срока.
    """
    rid, text, due_at, every = row
    fired = 0
    if now >= due_at:
        fired = 1 + (int((now - due_at) // every) if every else 0)
    if every:
        due_at += fired * every
    return {
        "id": rid,
        "text": text,
        "every": every,
        "due_at": round(due_at, 3),
        "fired": fired,
        "state": "сработало" if fired else "ждёт",
    }


def list_reminders(*, path: Path | None = None, now: float | None = None) -> dict:
    """Агрегированный результат: список с состояниями плюс счётчики по ним."""
    now = time.time() if now is None else now
    with contextlib.closing(_connect(path or db_path())) as conn:
        rows = conn.execute(
            "SELECT id, text, due_at, every FROM reminders ORDER BY id"
        ).fetchall()
    items = [_item(row, now) for row in rows]
    waiting = sum(1 for item in items if item["state"] == "ждёт")
    return {
        "items": items,
        "total": len(items),
        "waiting": waiting,
        "fired": len(items) - waiting,
    }


def cancel_reminder(reminder_id: int, *, path: Path | None = None) -> bool:
    """Снять по номеру. False — такого номера нет, и это не ошибка."""
    with contextlib.closing(_connect(path or db_path())) as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        return cur.rowcount > 0


server = FastMCP("remind")


@server.tool(
    description="Напоминание через in_seconds секунд; every — повторять каждые N секунд"
)
def remind(text: str, in_seconds: float, every: float | None = None) -> str:
    if not text.strip():
        return "пустой текст: напоминать не о чем"
    if in_seconds < 0:
        return "отрицательная задержка: напоминание в прошлом не завести"
    if every is not None and every <= 0:
        return "период обязан быть больше нуля"
    rid = add_reminder(text.strip(), in_seconds, every)
    again = f", повтор каждые {every:g} с" if every else ""
    return f"напоминание №{rid} заведено: через {in_seconds:g} с{again} — «{text.strip()}»"


@server.tool(description="Список напоминаний с состояниями и счётчики: ждёт / сработало")
def reminders() -> str:
    return json.dumps(list_reminders(), ensure_ascii=False)


@server.tool(description="Снять напоминание по номеру")
def cancel(id: int) -> str:  # noqa: A002 — имя параметра из протокола
    if cancel_reminder(id):
        return f"напоминание №{id} снято"
    return f"напоминания №{id} нет"


if __name__ == "__main__":
    server.run()  # stdio: менеджер говорит с процессом по stdin/stdout
