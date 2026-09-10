"""Два процесса на одной базе: id не пересекаются, чужой диалог цел.

    .venv/bin/python checks/two_processes.py

Стенд и консоль по умолчанию работают с одним файлом `data/agents.db` — README
прямо предлагает запустить `python -m app.cli` рядом с поднятым `uvicorn`.
Значит, одновременные писатели — штатный режим, а не экзотика, и проверять его
надо процессами, которые **живут одновременно**, а не по очереди.

Две части:

1. **Долгий процесс и второй рядом.** A заводит три чата и остаётся жить,
   B занимает `ag_00004` и уходит, после чего A заводит ещё одного — со
   счётчиком в памяти он выдал бы `ag_00004` второй раз, и первая же запись
   истории (`DELETE FROM messages WHERE session_id = ?`) стёрла бы диалог B.

2. **Четыре писателя одновременно**, по общему сигналу: в базе должны
   оказаться все двадцать чатов, ни одного потерянного и ни одного общего id.

Живых вызовов к модели нет: стрим подменён заглушкой в каждом процессе.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GATE_TIMEOUT = 30.0
"""Сколько ребёнок ждёт общего сигнала: лучше упасть с внятным текстом,
чем висеть вечно, если родитель умер."""


def _wait_for(path: str) -> None:
    deadline = time.monotonic() + GATE_TIMEOUT
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise SystemExit(f"не дождался сигнала {path} за {GATE_TIMEOUT} с")
        time.sleep(0.01)


def _prepare(db: str):
    """Поднимает агентскую часть в дочернем процессе: своя база, заглушка стрима."""
    from checks import _stub

    _stub.use_temp_db(db)
    _stub.install(reply=lambda messages, i: f"ответ на {messages[-1]['content'][:40]}")

    from app.registry import REGISTRY
    from app.schema import AgentSpec

    return REGISTRY, AgentSpec


def _talk(agent, text: str) -> None:
    """Один обмен: он и записывает историю в базу."""
    asyncio.run(_drain(agent.ask(text)))


async def _drain(agen) -> None:
    async for _ in agen:
        pass


def _child_long(db: str, folder: str) -> dict:
    """Процесс A: три сессии, ожидание сигнала, ещё одна сессия."""
    registry, AgentSpec = _prepare(db)

    early = []
    for i in range(3):
        agent = registry.create(AgentSpec(label=f"A-ранняя-{i}", model="stub/m"))
        _talk(agent, f"A-РАННЯЯ-{i}")
        early.append(agent.id)

    open(os.path.join(folder, "ready-A"), "w").close()
    _wait_for(os.path.join(folder, "gate"))

    # Тот самый момент: процесс живёт давно, а база с тех пор ушла вперёд.
    late = registry.create(AgentSpec(label="A-поздняя", model="stub/m"))
    _talk(late, "A-ПОЗДНЯЯ")
    return {"early": early, "late": late.id}


def _child_second(db: str) -> dict:
    """Процесс B: стартует позже, заводит одну сессию с секретом и уходит."""
    registry, AgentSpec = _prepare(db)
    agent = registry.create(AgentSpec(label="B-консоль", model="stub/m"))
    _talk(agent, "СЕКРЕТ_B: пароль от сейфа 1234")
    return {"session": agent.id}


def _child_writer(db: str, folder: str, index: int, count: int) -> dict:
    """Один из четырёх одновременных писателей."""
    registry, AgentSpec = _prepare(db)
    open(os.path.join(folder, f"ready-{index}"), "w").close()
    _wait_for(os.path.join(folder, "gate"))

    made = []
    for j in range(count):
        agent = registry.create(AgentSpec(label=f"писатель{index}-{j}", model="stub/m"))
        _talk(agent, f"СЕКРЕТ-{index}-{j}")
        made.append(agent.id)
    return {"sessions": made}


def _spawn(*args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect(process: subprocess.Popen, who: str) -> dict:
    out, err = process.communicate(timeout=120)
    assert process.returncode == 0, f"{who} упал:\n{out}\n{err}"
    return json.loads(out.strip().splitlines()[-1])


def main() -> int:
    from app.store import Store

    folder = tempfile.mkdtemp(prefix="concurrent-")
    db = os.path.join(folder, "agents.db")
    gate = os.path.join(folder, "gate")

    # --- 1. долгий процесс и второй рядом ------------------------------------
    long_process = _spawn("--long", "--db", db, "--folder", folder)
    _wait_for(os.path.join(folder, "ready-A"))

    second = _collect(_spawn("--second", "--db", db), "процесс B")
    print(f"[1] процесс B занял сессию {second['session']} и вышел, A ещё жив")

    open(gate, "w").close()
    long_result = _collect(long_process, "процесс A")
    print(f"[2] процесс A завёл после этого {long_result['late']}")

    assert long_result["late"] != second["session"], (
        f"процесс A выдал занятый id {long_result['late']} — второй процесс "
        "затирает чужую сессию"
    )

    store = Store(db).init()
    survived = store.load_session(second["session"])
    assert survived is not None, "сессия второго процесса исчезла из базы"
    assert survived["label"] == "B-консоль", survived["label"]
    texts = [row[2] for row in store.message_rows(second["session"])]
    assert any("СЕКРЕТ_B" in t for t in texts), f"реплики второго процесса стёрты: {texts}"
    print(f"[3] сессия B цела: label={survived['label']}, реплик {len(texts)}")

    ids = long_result["early"] + [long_result["late"], second["session"]]
    assert len(set(ids)) == len(ids), f"id повторились: {ids}"
    print(f"[4] пять сессий, пять разных id: {', '.join(sorted(ids))}")

    # --- 2. четыре писателя одновременно -------------------------------------
    folder2 = tempfile.mkdtemp(prefix="concurrent-many-")
    db2 = os.path.join(folder2, "agents.db")
    writers, per_writer = 4, 5

    processes = [
        _spawn("--writer", str(i), "--db", db2, "--folder", folder2, "--count", str(per_writer))
        for i in range(writers)
    ]
    for i in range(writers):
        _wait_for(os.path.join(folder2, f"ready-{i}"))
    # Все четверо уже подняли базу и ждут — сигнал пускает их писать вперехлёст.
    open(os.path.join(folder2, "gate"), "w").close()

    results = [_collect(p, f"писатель {i}") for i, p in enumerate(processes)]
    made = [session for r in results for session in r["sessions"]]
    assert len(made) == writers * per_writer, len(made)
    assert len(set(made)) == len(made), f"id пересеклись между процессами: {sorted(made)}"

    store2 = Store(db2).init()
    stored = {row["id"]: row for row in store2.list_sessions(limit=1000)}
    assert len(stored) == len(made), (
        f"в базе {len(stored)} сессий вместо {len(made)}: чьи-то записи затёрты"
    )
    for index in range(writers):
        for j in range(per_writer):
            secret = f"СЕКРЕТ-{index}-{j}"
            found = [
                session_id
                for session_id in made
                if any(secret == row[2] for row in store2.message_rows(session_id))
            ]
            assert len(found) == 1, f"{secret} найден в {len(found)} сессиях вместо одной"
    print(
        f"[5] {writers} процесса писали одновременно: {len(stored)} сессий в базе, "
        f"все {writers * per_writer} секретов на месте, каждый ровно в одной"
    )

    store.close()
    store2.close()
    print("\nОК: два процесса на одной базе не мешают друг другу.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Одновременные писатели на одной базе.")
    parser.add_argument("--db", default="")
    parser.add_argument("--folder", default="")
    parser.add_argument("--long", action="store_true")
    parser.add_argument("--second", action="store_true")
    parser.add_argument("--writer", type=int, default=None)
    parser.add_argument("--count", type=int, default=5)
    parsed = parser.parse_args()

    if parsed.long:
        print(json.dumps(_child_long(parsed.db, parsed.folder), ensure_ascii=False))
    elif parsed.second:
        print(json.dumps(_child_second(parsed.db), ensure_ascii=False))
    elif parsed.writer is not None:
        print(
            json.dumps(
                _child_writer(parsed.db, parsed.folder, parsed.writer, parsed.count),
                ensure_ascii=False,
            )
        )
    else:
        raise SystemExit(main())
