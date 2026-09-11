"""Главный критерий Дня 7: диалог переживает перезапуск программы.

    .venv/bin/python checks/restart.py

Не «тот же объект в памяти»: скрипт запускает **два отдельных процесса**
питона, один за другим, и даёт им один файл базы. Первый знакомится с агентом
и умирает, второй поднимается с нуля и продолжает тот же чат.

Живых вызовов к модели нет: заглушка печатает ровно то, что агент собрался
отправить. По этому промпту и видно, что память вернулась из базы, — сам
вопрос второго процесса ни имени, ни города не содержит.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

INTRO = "Меня зовут Нина, еду в Казань на три дня, я вегетарианка."
FOLLOW_UP = "Что мне взять из одежды?"
"""Ни имени, ни города: всё, что агент про них скажет, придёт из базы."""


def _child(db: str, session: str | None, text: str) -> dict:
    """Один «запуск программы» в отдельном процессе — его и убивает перезапуск."""
    from checks import _stub

    _stub.use_temp_db(db)
    _stub.install(reply=lambda messages, i: f"хорошо, Нина: {len(messages)} сообщений в промпте")

    from app import cli

    args = cli._parse_args(
        ["--model", "stub/model", "--label", "Перезапуск"]
        + (["--session", session] if session else [])
    )
    agent = cli.build_agent(args)
    restored = len(agent.history)

    import io

    asyncio.run(cli.ask(agent, text, io.StringIO()))
    return {
        "session": agent.id,
        "restored": restored,
        "prompt": _stub.CALLS[-1]["messages"],
        "history": [t.role for t in agent.history],
    }


def _run_child(db: str, session: str | None, text: str) -> dict:
    """Запускает этот же файл отдельным процессом и читает его JSON."""
    argv = [sys.executable, os.path.abspath(__file__), "--child", "--db", db, "--text", text]
    if session:
        argv += ["--session", session]
    result = subprocess.run(argv, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    db = os.path.join(tempfile.mkdtemp(prefix="restart-"), "agents.db")

    # --- запуск 1: знакомство -------------------------------------------------
    first = _run_child(db, None, INTRO)
    session = first["session"]
    assert first["restored"] == 0, first
    assert first["history"] == ["user", "assistant"], first
    print(f"[1] процесс #1 (pid уже мёртв): сессия {session}, история {first['history']}")

    # Процесс #1 завершился. В памяти не осталось ничего — только файл базы.
    assert os.path.exists(db), db
    print(f"[2] процесс #1 закрыт, база на диске: {os.path.getsize(db)} байт")

    # --- запуск 2: продолжение ------------------------------------------------
    second = _run_child(db, session, FOLLOW_UP)
    assert second["session"] == session, (second["session"], session)
    assert second["restored"] == 2, f"история не вернулась из базы: {second}"

    roles = [m["role"] for m in second["prompt"]]
    assert roles == ["system", "user", "assistant", "user"], roles
    joined = " ".join(m["content"] for m in second["prompt"])
    for word in ("Нина", "Казань", "вегетарианка"):
        assert word in joined, f"после перезапуска агент забыл «{word}»: {joined[:200]}"
    assert second["prompt"][-1]["content"] == FOLLOW_UP, second["prompt"][-1]
    assert FOLLOW_UP.count("Казань") == 0, "вопрос второго процесса не должен подсказывать"
    print(f"[3] процесс #2: поднято {second['restored']} реплик, в промпте {len(roles)} сообщений")
    print("[4] в промпте после перезапуска есть: Нина, Казань, вегетарианка")

    # --- изоляция: свежая сессия в той же базе ничего не знает ---------------
    third = _run_child(db, None, "Как меня зовут?")
    assert third["session"] != session, third
    assert third["restored"] == 0, third
    fresh = " ".join(m["content"] for m in third["prompt"])
    for word in ("Нина", "Казань"):
        assert word not in fresh, f"чужая сессия видит «{word}»: истории слились"
    print(f"[5] новая сессия {third['session']} в той же базе не видит чужой истории")

    print("\nОК: диалог продолжился в новом процессе, чужая сессия его не видит.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--db", default="")
    parser.add_argument("--session", default=None)
    parser.add_argument("--text", default="")
    parsed = parser.parse_args()
    if parsed.child:
        print(json.dumps(_child(parsed.db, parsed.session, parsed.text), ensure_ascii=False))
        raise SystemExit(0)
    raise SystemExit(main())
