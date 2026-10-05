"""Ядро проверок дня 22 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Проверки измеряют учебные контракты и сквозные свойства. Отдельные
скрипты (`restart.py`, `two_processes.py`) запускаются отсюда же.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from checks import _stub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_stub.install_offline()

# MCP в наборе выключен по умолчанию: проверки Дней 6–15 обязаны пройти
# в точности как были, без процесса на каждый TestClient. Проверки MCP
# включают его сами, с фикстурным конфигом (`_mcp_env`).


import app.agent as agent_module  # noqa: E402
import app.main as main  # noqa: E402
from app.llm import Metrics  # noqa: E402
import app.mcp as mcp_module
from app.mcp import McpManager  # noqa: E402
from app.registry import REGISTRY  # noqa: E402
from app.schema import (  # noqa: E402
    MOVE_COMMANDS,
    MOVES,
    RESUME,
    STAGE_EXPECTS,
    STAGE_LABELS,
    STAGE_RULES,
    STAGES,
    TRANSITIONS,
    AgentSpec,
)
from app.store import Store, StoreBusyError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

CHECKS: list = []
"""Проверки в порядке объявления. Наполняет декоратор `check`."""


def check(name):
    def wrap(fn):
        def run():
            _stub.reset()
            REGISTRY.kill_all()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
            except AssertionError as exc:
                RESULTS.append((name, False, str(exc)))
            except Exception as exc:  # noqa: BLE001
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))

        run.__name__ = fn.__name__
        CHECKS.append(run)
        return run

    return wrap


async def drain(agen) -> list[dict]:
    return [event async for event in agen]


def sse(text: str) -> list[dict]:
    """Разбирает тело SSE-ответа в список событий."""
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def new_agent(client, **fields) -> str:
    payload = {"model": "stub/model", "label": "тест", **fields}
    response = client.post("/api/agents", json={"agent": payload})
    assert response.status_code == 200, response.text
    return response.json()["agents"][0]["id"]


KEEP, EVERY = 6, 10
"""Окно памяти и порог, на которых проверяется сжатие. Порог выше самого
длинного сценария остальных проверок (3 обмена, 6 реплик): опусти его — и
поплывут счётчики вызовов к модели в чужих проверках, где сжатия быть не должно."""


def _service_kind(messages) -> str | None:
    """Чем был этот вызов: `summary` — сжатие, `None` — обычный обмен.
    У служебного вызова свой системный промпт, по нему и различаем.

    Вопрос остался «какой это вызов?», хотя ответов снова два: вызовов
    было три вида, потом два, и счётчики чужих проверок каждый раз плыли.
    Спрашивать «это сжатие?» — значит заводить второй ответ заново при
    первом же новом вызове."""
    if not messages:
        return None
    return {agent_module.COMPRESS_SYSTEM: "summary"}.get(messages[0].get("content"))


def _service_calls(kind: str | None = None) -> list[dict]:
    """Служебные вызовы: все или только одного типа."""
    return [
        call
        for call in _stub.CALLS
        if _service_kind(call["messages"]) is not None
        and (kind is None or _service_kind(call["messages"]) == kind)
    ]


def _service_aware(messages, index):
    """Ответ заглушки, по которому видно, чем был вызов: сводка узнаётся
    в промпте следующего обмена по слову СВОДКА."""
    if _service_kind(messages) == "summary":
        return f"СВОДКА {index}"
    return f"ответ {index}"


def _memory_aware(messages, index):
    """Ответ, который зависит от **содержимого запроса**, а не от его номера.

    Заглушка не модель и моделью не притворяется. Но врезку памяти она читает
    ровно там же, где прочитала бы её модель, — в тексте промпта, — и этого
    довольно, чтобы «память влияет на ответы» стало утверждением о самом
    ответе, а не о составе запроса. Всё, чего проверка не вправе требовать
    от настоящей модели, — что ответ будет именно этими словами; чего она
    вправе требовать и здесь, и у живого прогона, — что ответ **разойдётся**.

    Читаются обе врезки: слои разные, а показывают они себя одинаково —
    вписанная запись доезжает до ответа.
    """
    knows = any(
        "пишу на Kotlin" in m.get("content", "")
        or "цель: пример на Kotlin" in m.get("content", "")
        for m in messages
    )
    return "Держи пример на Kotlin." if knows else "На каком языке показать пример?"


def _profile_aware(messages, index):
    """Ответ, который зависит от **профиля** в запросе, а не от его номера.

    Профиль заглушка читает там же, где прочитала бы его модель, — в блоке
    `[как отвечать]` системного сообщения, — и этого довольно, чтобы «разные
    профили дают разные ответы» стало утверждением об ответе, а не о составе
    запроса. Словами живой модели заглушка не притворяется: обещать она
    вправе только то, что разница доезжает до ответа, а не теряется
    по дороге. Довод тот же, что у `_memory_aware`.
    """
    head = messages[0].get("content", "") if messages else ""
    if "стиль: кратко, на ты" in head:
        return "Смотри: начни с макета."
    if "стиль: подробно, с примерами" in head:
        return "Давайте разберём по шагам, с примерами: начните с макета."
    return "С чего вам удобнее начать?"


def _invariant_aware(messages, index):
    """Ответ, который зависит от **инварианта** в запросе, а не от его номера.

    Инвариант заглушка читает там же, где прочитала бы его модель, — в блоке
    `[чего нельзя]` системного сообщения, — и этого довольно, чтобы «ассистент
    отказывается предлагать запрещённое» стало утверждением об ответе, а не
    о составе запроса. Довод тот же, что у `_memory_aware` и `_profile_aware`.

    Вид назван **исключительным** для слоя: «решение» лежит и в долговременной
    памяти, и в рабочей, и узнавай заглушка его — она отвечала бы так же
    на чужую запись, а проверка была бы зелена по неверной причине.
    """
    head = messages[0].get("content", "") if messages else ""
    if "ограничение стека: бэкенд только на Python" in head:
        return "Только Python: нарушается инвариант «ограничение стека»."
    return "Возьмите Java со Spring — обычный выбор под такую задачу."


def _stage_aware(messages, index):
    """Ответ, который зависит от **блока жизненного цикла** в запросе.

    Заглушка читает автомат там же, где прочитала бы его модель, — в блоке
    `[жизненный цикл задачи]` системного сообщения, — и отказ собирает
    из него же: этап берёт из строки «сейчас здесь», команду — из хода,
    ведущего к «выполнению». Этого довольно, чтобы «ассистент не делает
    работу чужого этапа» стало утверждением об **ответе**, а не о составе
    запроса. Довод тот же, что у `_memory_aware` и `_invariant_aware`.

    Блока нет — работа делается: знание машины и есть вся разница между
    двумя ответами.
    """
    head = messages[0].get("content", "") if messages else ""
    asked = messages[-1].get("content", "") if messages else ""
    here = next((line for line in head.splitlines() if " — сейчас здесь: " in line), "")
    stage, _, moves = here.partition(" — сейчас здесь: ")
    if "напиши код" not in asked or stage == "выполнение":
        return f"ответ {index}"
    if not here:
        return "Держи код: def main(): ..."
    way = next((m for m in moves.split("; ") if m.endswith("→ выполнение")), "")
    command = way.partition("(")[2].partition(")")[0]
    return (
        f"Кода сейчас не будет: это работа этапа «выполнение», "
        f"а мы на этапе «{stage}». "
        + (f"Перейдите {command}." if command else "Отсюда туда хода нет.")
    )


def _temp_db(name: str) -> str:
    """Свежий файл базы под одну проверку. Каталога заранее нет — его создаёт Store."""
    import tempfile

    return os.path.join(tempfile.mkdtemp(prefix=f"check-{name}-"), "nested", "agents.db")


def _script(name: str, expected: str, line: int) -> str:
    """Отдельный скрипт-проверка: он поднимает свои процессы сам."""
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "checks", name)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout, result.stdout
    return result.stdout.strip().splitlines()[line]


# --- Подготовка сцены: общее для многих проверок ------------------------------
#
# Помощники ниже **ничего не утверждают** — утверждение, уехавшее из проверки
# в помощник, стало бы самопроверкой стенда, а их в этом наборе нет. И сюда же
# не уезжает ничего, что проверка **измеряет**: число обменов, счётчики вызовов
# к заглушке и сами сравнения остаются на виду в проверке.
#
# Своего помощника на «подменить и вернуть» здесь нет намеренно: это делают
# `patch.object` и `patch.dict` из стандартной библиотеки. Возврат у подмены
# двоякий — атрибут модуля кладут обратно, а подменённый метод класса надо
# снять, — и ошибиться в этом руками проще, чем взять готовое.


def _frames(client, agent_id: str, text: str) -> list[dict]:
    """Обмен через веб-слой, разобранный в список кадров SSE."""
    return sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": text}).text)


def _frame(frames, event: str) -> dict:
    """Первый кадр названного события."""
    return next(e for e in frames if e["event"] == event)


def _talk(client, agent_id: str, turns, text: str = "вопрос {i}") -> list:
    """Обмены подряд через веб-слой; отдаёт ответы ручки. Сколько их —
    остаётся у проверки: это её счёт, а не подробность сцены. Число значит
    «столько с нуля», готовый `range` — «как просили»: продолжение чата
    нумеруется дальше, и номер уезжает в текст вопроса."""
    turns = range(turns) if isinstance(turns, int) else turns
    return [
        client.post(f"/api/agents/{agent_id}/messages", json={"text": text.format(i=i)})
        for i in turns
    ]


def _ask(agent, turns, text: str = "вопрос {i}") -> None:
    """То же самое, но мимо веба — прямо через `Agent.ask`."""
    for i in range(turns) if isinstance(turns, int) else turns:
        asyncio.run(drain(agent.ask(text.format(i=i))))


def _bare(label: str, **fields):
    """Агент без хранилища: чату нужен только конфиг."""
    return agent_module.Agent(AgentSpec(label=label, model="stub/model", **fields))


def _fill(agent, count: int, text: str = "реплика {i}", persist=False, role=None):
    """История из `count` реплик: роли чередуются от `user`, а `role` берёт
    одну на все — чат, набранный вопросами без ответов, тоже бывает."""
    for i in range(count):
        said = role or ("user" if i % 2 == 0 else "assistant")
        agent.remember(said, text.format(i=i), persist=persist)
    return agent


@contextlib.contextmanager
def _reopened(path: str):
    """Настоящее переоткрытие файла базы, а не тот же объект в памяти.
    Закрывается и при падении: незакрытое соединение держит WAL, и соседка
    на том же файле получила бы чужую блокировку вместо своего падения."""
    store = Store(path).init()
    try:
        yield store
    finally:
        store.close()


@contextlib.contextmanager
def _restarted(store, agent_id: str):
    """Перезапуск программы целиком: закрыть файл, открыть заново и поднять
    из него чат. Конфиг у поднятого заведомо пустой — всё, что он о себе
    знает, приехало из базы."""
    path = store.path
    store.close()
    with _reopened(path) as fresh:
        yield fresh, agent_module.Agent(
            AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=fresh
        )


ALL_TABLES = (
    "sessions", "messages", "meta", "summaries", "branches", "memory",
    "working_memory", "task_state", "profile", "invariants",
)
"""Все таблицы схемы: перебор идёт по ним целиком и по всем колонкам каждой,
чтобы ключ искался и в колонках, которых ещё не придумали."""


def _columns_holding(conn, needle: str, tables=ALL_TABLES, exclude=()) -> list[str]:
    """Колонки, в которых лежит `needle`, — списком `таблица.колонка`."""
    found = set()
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            for name in row.keys():
                if f"{table}.{name}" not in exclude and isinstance(row[name], str) and needle in row[name]:
                    found.add(f"{table}.{name}")
    return sorted(found)


def _body_rules(payload) -> list[str]:
    """Три правила тела запроса — списком нарушенных, словами. Проверяются
    они на путях **обмена** и на обоих служебных вызовах и раньше стояли
    дословной копией в каждом из трёх мест; копии расходятся молча — поправят
    два места из трёх, и третий путь уедет к провайдеру без правила, ничего
    не уронив. Утверждение осталось в проверках, здесь только его текст."""
    broken = []
    provider = payload.get("provider")
    if not (provider and provider.get("require_parameters") is True):
        broken.append(f"нет provider.require_parameters: {provider!r}")
    if {"id": "context-compression", "enabled": False} not in (payload.get("plugins") or []):
        broken.append(f"сжатие контекста отдано провайдеру: {payload.get('plugins')!r}")
    if payload.get("usage") != {"include": True}:
        broken.append(f"нет просьбы о usage: {payload.get('usage')!r}")
    return broken


# --- Очистка: таблица «слой × путь» -------------------------------------------

CLEANUP_PATHS = {
    "forget": lambda store, chat: chat.forget(),
    "delete_session": lambda store, chat: store.delete_session(chat.id),
    "clear": lambda store, chat: store.clear(),
}
"""Три пути, которыми у чата что-нибудь отнимают, в порядке возрастания охвата:
забыть разговор, удалить чат, стереть базу."""

CLEANUP_TABLE = {
    # слой              forget delete_session clear
    "summaries":       (True,  True,  True),
    "working":         (True,  True,  True),
    "task":            (True,  True,  True),
    "branches":        (False, True,  True),
    "long_term":       (False, False, True),
    "profile":         (False, False, True),
    "invariants":      (False, False, True),
}
"""Чего после какого пути очистки не остаётся. Каждый `True` — место, где
`DELETE` обязан стоять руками: каскада нет, внешние ключи не объявлены.

`False` — не пробелы в таблице, а вторая её половина, и такая же
обязательная. Состояние задачи уносится со всех трёх: правило этапа уезжает **системным**
сообщением, и забытый разговор оставил бы следующему чужое «не продолжай».
Родство переживает `forget()`: это не содержимое разговора,
а то, откуда чат взялся, и ветка, забывшая историю, осталась веткой того же
родителя. Долговременная память переживает и `forget()`, и удаление чата:
чат ей не владелец, а читатель, — и стирает её ровно один путь, `clear()`,
потому что он не «ещё одна таблица чата», а вся база разом. Профиль — тем же
рядом и по тому же доводу: он один на всю базу и про человека, а не про чат,
и забытый разговор своего собеседника не меняет. Инварианты — третьим тем же
рядом: архитектура продукта не меняется от того, в каком чате о ней спросили,
и забытый разговор её не отменяет. `clear()` все три всё-таки стирает, и это
критично вдвойне: и профиль, и инварианты заводят **системное** сообщение,
и утёкшее в чужую проверку сдвинуло бы там номера всех врезок разом. Проверка,
потребовавшая бы чистки везде, сломала бы задуманное так же, как забытый
`DELETE`.
"""


def _filled_chat(store, label: str):
    """Чат, у которого непусты **все семь** слоёв сразу: сводка, рабочая
    память, состояние задачи, родство, долговременная память, профиль
    и инварианты.

    Обменов три: порог сжатия при `keep_last=2` и `compress_every=2`
    набирается только на третьем. Запись рабочей памяти и задачу кладёт
    человек — других путей в эти слои нет. Родство пишется прямо
    в хранилище: ветвить настоящего родителя ради одной строки незачем,
    а `parent_id` тут и не разглядывается.
    """
    chat = agent_module.Agent(
        AgentSpec(label=label, model="stub/model", strategy="summary",
                  keep_last=2, compress_every=2),
        store=store,
    )
    _ask(chat, 3, label + " {i}")
    chat.add_working_record("goal", f"цель чата {label}")
    chat.start_task(f"задача чата {label}")
    store.save_branch(chat.id, parent_id="ag_00001", forked_at=2)
    store.add_memory("knowledge", f"запись рядом с чатом {label}")
    store.save_profile({"style": f"кратко, рядом с чатом {label}"})
    store.add_invariant("stack", f"инвариант рядом с чатом {label}", ["Java"])
    return chat


def _leftovers(store, session_id: str) -> dict:
    """Что осталось от чата в каждом слое: пустое значение — слой унесён."""
    return {
        "summaries": store.load_summaries(session_id),
        "working": store.list_working(session_id),
        "task": store.load_task(session_id),
        "branches": store.load_branch(session_id),
        "long_term": store.list_memory(),
        "profile": store.load_profile(),
        "invariants": store.list_invariants(),
    }


# --- Изоляция конфигов и CLI ------------------------------------------------


@check("конфиг копируется вглубь: два агента не делят extra_body")
def check_spec_deep_copy():
    from app.registry import AgentRegistry

    shared = AgentSpec(
        label="общий",
        model="stub/model",
        stop=["\n"],
        response_format={"type": "json_object"},
        extra_body={"provider": {"allow_fallbacks": False}},
    )
    registry = AgentRegistry(max_agents=100)
    first, second = registry.create_many([shared, shared])

    assert first.spec.extra_body is not shared.extra_body
    assert first.spec.extra_body["provider"] is not shared.extra_body["provider"]
    assert first.spec.extra_body is not second.spec.extra_body
    assert first.spec.stop is not shared.stop
    assert first.spec.response_format is not shared.response_format

    first.spec.extra_body["provider"]["order"] = ["only-me"]
    first.spec.stop.append("ДРУГОЕ")
    first.spec.response_format["type"] = "json_schema"
    assert "order" not in shared.extra_body["provider"], shared.extra_body
    assert "order" not in second.spec.extra_body["provider"], second.spec.extra_body
    assert shared.stop == ["\n"], shared.stop
    assert shared.response_format == {"type": "json_object"}, shared.response_format
    assert second.spec.stop == ["\n"], second.spec.stop
    return "правка у одного агента не задела ни общий конфиг, ни соседа"


@check("агент живёт без веб-слоя: CLI говорит с ним напрямую")
def check_cli():
    _stub.install(reply="привет из консоли")
    import io

    from app import cli

    args = cli._parse_args(["--model", "stub/m", "--label", "консоль"])
    agent = cli.build_agent(args)
    out = io.StringIO()
    answer = asyncio.run(cli.ask(agent, "как дела?", out))
    assert answer == "привет из консоли", answer
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.id in {a.id for a in REGISTRY.list()}, "CLI-агент виден в реестре процесса"

    assert "привет из консоли" in out.getvalue(), out.getvalue()
    return "ответ напечатан, история записана, агент в реестре"


# --- История: помнится и уезжает в модель целиком ------------------------------


@check("обрезка: full/window/summary сохраняют историю и различают пропущенное")
def check_cut_only_where_chosen():
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        full = new_agent(client, system="СИС", keep_last=2, compress_every=2)
        _talk(client, full, 4)
        agent = REGISTRY.require(full)
        sent = _stub.CALLS[-1]["messages"]
        assert len(sent) == 8 and sent[1]["content"] == "вопрос 0", sent
        assert len(agent.history) == 8 and not _service_calls()
        assert not any(k in agent.history[-1].metrics for k in ("summarized", "dropped"))

        _stub.reset()
        window = new_agent(client, strategy="window", keep_last=2)
        _talk(client, window, 4)
        win = REGISTRY.require(window)
        sent = _stub.CALLS[-1]["messages"]
        assert [m["content"] for m in sent] == ["вопрос 2", "ответ 2", "вопрос 3"], sent
        assert win.history[-1].metrics["dropped"] == 4
        assert "summarized" not in win.history[-1].metrics
        assert len(win.history) == 8 and len(_stub.CALLS) == 4 and not _service_calls()
        assert client.patch(f"/api/agents/{window}", json={"keep_last": None}).status_code == 200
        assert win.context_cut() == (0, None)
        assert len(win.build_prompt("ещё")) == 9
        assert client.patch(f"/api/agents/{window}", json={"keep_last": 0}).status_code == 200
        assert win.build_prompt("ещё") == [{"role": "user", "content": "ещё"}]

        _stub.reset()
        folded = new_agent(client, strategy="summary", keep_last=2, compress_every=2,
                           stop=["СТОП"], response_format={"type": "json_object"})
        _talk(client, folded, 2)
        assert not _service_calls() and len(_stub.CALLS[-1]["messages"]) == 3
        _talk(client, folded, range(2, 4))
        chat = REGISTRY.require(folded)
        sent = _stub.CALLS[-1]["messages"]
        assert len(chat.history) == 8 and chat.summary_cover() == 4
        assert len(sent) == 4 and "СВОДКА" in sent[0]["content"]
        assert sent[1]["content"] == "вопрос 2"
        assert chat.history[-1].metrics["summarized"] == 4
        assert "dropped" not in chat.history[-1].metrics
        assert chat.summary_cover() + len(sent[1:-1]) == len(chat.history) - 2
        assert not any(m["role"] == "system" for m in sent)
        summary_call = _service_calls("summary")[0]
        assert "вопрос 0" in summary_call["messages"][1]["content"]
        assert "вопрос 2" not in summary_call["messages"][1]["content"]
        assert "stop" not in summary_call["payload"] and "response_format" not in summary_call["payload"]
        assert _stub.CALLS[-1]["payload"]["stop"] == ["СТОП"]
        assert _stub.CALLS[-1]["payload"]["response_format"] == {"type": "json_object"}
        assert summary_call["payload"]["model"] == _stub.CALLS[-1]["payload"]["model"]
        _talk(client, folded, range(4, 6))
        incremental = _service_calls("summary")[-1]["messages"][1]["content"]
        assert "СВОДКА" in incremental and "вопрос 3" in incremental
        assert "вопрос 0" not in incremental
        assert chat.summary_cover() == 8 and len(chat.history) == 12
        assert len(chat.summaries) == 4
        # Switching only changes the view of complete history, never stored summaries.
        client.patch(f"/api/agents/{folded}", json={"strategy": "full"})
        assert len(chat.build_prompt("full")) == 13 and chat.context_cut() == (0, None)
        client.patch(f"/api/agents/{folded}", json={"strategy": "window"})
        assert len(chat.build_prompt("window")) == 3
        assert not any("пересказ начала" in m["content"] for m in chat.build_prompt("window"))
        client.patch(f"/api/agents/{folded}", json={"strategy": "summary"})
        assert chat.summary_cover() == 8 and len(chat.summaries) == 4
        assert client.patch(f"/api/agents/{folded}", json={"strategy": "unknown"}).status_code == 400
        assert chat.spec.strategy == "summary"
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        assert (fresh["strategy"], fresh["keep_last"], fresh["compress_every"]) == ("full", None, None)

    # Pair boundary and regeneration cover are distinct from strategy switching.
    odd = _fill(_bare("odd", strategy="summary", keep_last=5, compress_every=2), 20)
    asyncio.run(odd.compress(odd.spec))
    assert odd.summary_cover() == 14 and odd.history[14].role == "user"
    short = _fill(_bare("regen", strategy="summary", keep_last=0, compress_every=2), 10)
    asyncio.run(short.compress(short.spec))
    assert short.take_last_exchange() is not None
    assert short.summary_cover() == 8 == len(short.history)
    return "full без неявного сжатия; window без вызова модели; summary инкрементален, история цела"


@check("два параллельных запроса к одному агенту: 409, история не перемешана")
def check_parallel():
    _stub.install(reply=lambda m, i: f"ответ {i}", chunks=8, delay=0.02)

    async def scenario():
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://bench") as client:
            created = await client.post(
                "/api/agents",
                json={"agent": {"model": "stub/model", "label": "п"}},
            )
            agent_id = created.json()["agents"][0]["id"]
            first, second = await asyncio.gather(
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"}),
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "второй"}),
            )
            return agent_id, sorted([first.status_code, second.status_code])

    agent_id, codes = asyncio.run(scenario())
    # Память в этом чате выключена: вызов на её ведение — второе обращение
    # к модели за обмен, и «в модель ушёл ровно один вызов» перестало бы
    # говорить про то, ради чего проверка написана.
    assert codes == [200, 409], codes
    agent = REGISTRY.require(agent_id)
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert len(_stub.CALLS) == 1, f"в модель ушло {len(_stub.CALLS)} вызовов, а должен один"
    return f"коды {codes}, в истории 2 реплики, вызов к модели один"


# --- День 9: сжатие истории ---------------------------------------------------


@check("сжатие экономит входные токены, и сводка покрывает выброшенное")
def check_compression_saves_input():
    """Сравнение расхода «до/после», которого просит задание, — два одинаковых
    чата рядом: у одного окно памяти задано, у другого нет. `prompt_tokens`
    у заглушки считается по длине **отправленных** сообщений, поэтому экономия
    видна и без живых вызовов.

    Экономия считается честно: в итог по чату у сжатого входит и вызов на
    сжатие. Меньше должно стать не только у последнего обмена, но и по чату.
    """
    _stub.install(reply=_service_aware)
    question = "вопрос {i}: " + "довольно длинный текст вопроса, " * 20

    # События каждого обмена — по ним видно, что наружу сказано про сжатие:
    # оно идёт **до** ответа, и клиенту надо узнать о паузе, пока она идёт.
    events: dict[str, list[list[dict]]] = {}

    with TestClient(main.app) as client:
        # Память обоим выключена: проверка про экономию сжатия, и вызов
        # на ведение памяти — лишнее обращение к модели в обеих колонках
        # сравнения сразу.
        plain = new_agent(client, label="без сжатия")
        folded = new_agent(
            client, label="со сжатием", strategy="summary", keep_last=KEEP,
            compress_every=EVERY,
        )
        events = {plain: [], folded: []}
        for i in range(12):
            for agent_id in (plain, folded):
                response = client.post(
                    f"/api/agents/{agent_id}/messages", json={"text": question.format(i=i)}
                )
                assert response.status_code == 200, response.text
                events[agent_id].append(sse(response.text))
        totals = {
            agent_id: client.get(f"/api/agents/{agent_id}").json()
            for agent_id in (plain, folded)
        }

    def last_prompt(agent_id):
        calls = [c for c in _stub.CALLS if c["label"] == totals[agent_id]["label"]]
        return calls[-1]["payload"]

    plain_in = last_prompt(plain)["messages"]
    folded_in = last_prompt(folded)["messages"]
    plain_chars = sum(len(m["content"]) for m in plain_in)
    folded_chars = sum(len(m["content"]) for m in folded_in)

    # 12 обменов: у несжатого в промпте 11 пар и вопрос, у сжатого — сводка,
    # 6 непокрытых пар и вопрос. Сворачивалось один раз, на девятом обмене.
    assert len(plain_in) == 23, len(plain_in)
    assert len(folded_in) == 14, len(folded_in)
    assert folded_chars < plain_chars / 1.5, (folded_chars, plain_chars)

    # И ничего не потеряно: выброшенное покрыто сводкой ровно по границе.
    agent = REGISTRY.require(folded)
    # Системный промпт сдвигает сводку на одно место — и `summary_at` едет
    # вместе с ней: число считается тем же знанием о порядке, что и сборка.
    with_system = agent_module.copy_spec(agent.spec)
    with_system.system = "ты бот"
    slot = agent.prompt_slots(with_system)["summary_at"]
    shifted = agent.build_prompt("ещё", spec=with_system)
    assert slot == 1, slot
    assert "пересказ начала разговора" in shifted[slot]["content"], shifted[slot]

    covered = agent.summary_cover()
    assert covered == 10, covered
    assert covered + (len(folded_in) - 2) == len(agent.history) - 2, (covered, len(folded_in))
    assert "пересказ начала разговора" in folded_in[0]["content"], folded_in[0]

    # Вызов на сжатие — такой же вызов к модели, и три правила тела на нём
    # тоже. Особенно третье: собери сводку провайдер со включённым
    # `context-compression`, и он молча выбросил бы середину того самого
    # куска, который мы отдали пересказать, — сводка вышла бы дырявой,
    # а узнать об этом было бы неоткуда.
    folding_body = _service_calls("summary")[-1]["payload"]
    assert not _body_rules(folding_body), f"вызов на сжатие: {_body_rules(folding_body)}"

    # Про сворачивание сказано наружу — и ровно тогда, когда оно случилось.
    # Событие нужно **до** вызова на сжатие: он идёт к модели раньше ответа,
    # и карточка, узнавшая о паузе после неё, показала бы строку состояния
    # на пустом месте. «Сворачиваю» на каждом обмене было бы враньём — порог
    # не набран, сворачивания нет, и события тоже нет.
    def kinds(items):
        return [[e["event"] for e in one] for one in items]

    folding_at = [i for i, one in enumerate(kinds(events[folded])) if "compressing" in one]
    assert folding_at == [8], f"о сворачивании сказано на обменах {folding_at}, а свернулось на 8-м"
    assert not any("compressing" in one for one in kinds(events[plain])), (
        "чат без сжатия получил событие о сворачивании"
    )
    ninth = kinds(events[folded])[8]
    assert ninth.index("compressing") < ninth.index("start"), (
        f"о сворачивании сказано после промпта, а не до вызова: {ninth}"
    )
    # И **чем** занята пауза, называет сервер, а не догадка клиента: служебных
    # вызовов два, а кадр один, и без этого поля строка состояния не знала бы,
    # что писать. Промолчи сервер — клиент не покажет её вовсе, ни фактам,
    # ни сворачиванию.
    folding_frame = _frame(events[folded][8], "compressing")
    assert folding_frame.get("strategy") == "summary", folding_frame

    # И сводку в промпте клиенту показывает сервер, а не разбор текста:
    # `summary_at` — её место в `resolved_messages`. Порядок сборки промпта
    # живёт в `build_prompt`, и вторая его копия в браузере разошлась бы молча.
    def start_of(agent_id, index):
        return _frame(events[agent_id][index], "start")

    assert start_of(folded, 7)["summary_at"] is None, "сводка объявилась раньше сворачивания"
    assert start_of(plain, 11)["summary_at"] is None, "в чате без сжатия нашлась сводка"
    last = start_of(folded, 11)
    assert last["summary_at"] == 0, last["summary_at"]
    assert "пересказ начала разговора" in last["resolved_messages"][last["summary_at"]]["content"], (
        f"summary_at показывает не на сводку: {last['resolved_messages'][last['summary_at']]}"
    )
    # Место врезки без её имени неполно: подпись роли в просмотре промпта
    # берётся из того же поля. Промолчи сервер — сводка перестала бы
    # называться сводкой.
    assert last.get("strategy") == "summary", last.get("strategy")

    # Итог по чату — со стоимостью сжатия внутри, и всё равно меньше.
    plain_total = totals[plain]["usage_total"]["prompt_tokens"]
    folded_total = totals[folded]["usage_total"]["prompt_tokens"]
    assert folded_total < plain_total, (folded_total, plain_total)
    assert totals[folded]["history_len"] == 24, totals[folded]["history_len"]
    saved = 100 - round(100 * folded_total / plain_total)
    return (
        f"12 обменов: без сжатия {plain_total} входных токенов, со сжатием "
        f"{folded_total} (с вызовом на сжатие внутри) — на {saved}% меньше"
    )


@check("сводка живёт отдельно от истории, а очистка уносит ровно свои слои")
def check_summary_apart_and_cleanup():
    """Половины здесь две, и обе про одно: у сводки своя таблица.

    **Первая** — сводка лежит не строкой в `messages`: `save_history` на
    каждой записи делает `DELETE FROM messages`, и лежи сводка там, её стирал
    бы каждый следующий обмен. Доказательство — настоящее переоткрытие файла
    и обмен уже после него: сводка та же, история полная, `seq` без дыр,
    и текста сводки в репликах нет.

    **Вторая** — плата за отдельные таблицы: каскада нет, и каждый слой
    обязан уноситься с каждого пути очистки руками. Пути и слои сведены
    в таблицу (`CLEANUP_TABLE`), там же записано, почему одни клетки
    уносят, а другие **оставляют**. Раньше клетки проверялись врозь, по одной
    в четырёх проверках; здесь они проходятся разом и на чате, у которого
    непусты все семь слоёв, — утверждение об очистке обязано стоять
    на непустом значении, иначе оно показывает покрытие, которого нет.
    """
    from app.agent import Agent

    _stub.install(reply=_service_aware)
    path = _temp_db("summary-restart")
    store = Store(path).init()
    # Память выключена: первая половина про сводку, и врезка рабочей памяти
    # стояла бы в промпте перед ней, сдвигая всё, на что проверка смотрит
    # по номеру. У чатов второй половины она включена — им нужен непустой слой.
    spec = AgentSpec(
        label="сжатый", model="stub/model", strategy="summary",
        keep_last=KEEP, compress_every=EVERY,
    )
    agent = Agent(spec, store=store)
    _ask(agent, 9)
    before = [(s["upto"], s["content"]) for s in agent.summaries]
    agent_id = agent.id
    assert len(before) == 1 and before[0][0] == 10, before

    with _restarted(store, agent_id) as (again, revived):
        after = [(s["upto"], s["content"]) for s in revived.summaries]
        assert after == before, (before, after)
        # Конфиг сжатия поднялся вместе со сводкой — иначе она бы не работала.
        assert revived.spec.keep_last == KEEP, revived.spec.keep_last
        assert len(revived.history) == 18, len(revived.history)
        assert [r[0] for r in again.message_rows(agent_id)] == list(range(18)), "дыры в seq"
        assert revived.history[0].content == "вопрос 0", revived.history[0]

        # Сводки нет среди реплик: она в своей таблице, а не в ленте.
        contents = [r[2] for r in again.message_rows(agent_id)]
        assert not any("СВОДКА" in c for c in contents), contents
        assert revived.build_prompt("ещё")[0]["content"].count("СВОДКА") == 1, "сводка не подставилась"

        # И обмен после перезапуска её не стирает: `save_history` переписывает
        # `messages` целиком, а `summaries` не трогает вовсе.
        asyncio.run(drain(revived.ask("вопрос после перезапуска")))
        assert [(s["upto"], s["content"]) for s in again.load_summaries(agent_id)] == before, (
            "обмен после перезапуска затёр сводку"
        )
        assert len(revived.history) == 20, len(revived.history)

        # --- таблица «слой × путь очистки» -----------------------------------
        #
        # Путь получает свой чат: `clear()` стирает базу целиком, и общий чат
        # на три пути оставил бы последним двум пустую сцену. Идут они в том
        # же порядке, что в `CLEANUP_PATHS`, — от самого узкого к самому
        # широкому, и `clear()` последним.
        for column, (path_name, wipe) in enumerate(CLEANUP_PATHS.items()):
            chat = _filled_chat(again, path_name)
            full = _leftovers(again, chat.id)
            # Сцена непуста во всех семи слоях: без этого «после очистки
            # пусто» держалось бы само собой и стерегло бы воздух.
            assert all(full.values()), (path_name, full)
            wipe(again, chat)
            left = _leftovers(again, chat.id)
            for layer, row in CLEANUP_TABLE.items():
                if row[column]:
                    assert not left[layer], f"{path_name} оставил «{layer}»: {left[layer]!r}"
                else:
                    assert left[layer] == full[layer], (
                        f"{path_name} унёс «{layer}», а тот обязан остаться: {full[layer]!r}"
                    )

        # И в памяти объекта сводка тоже пуста, а не только в файле.
        # `summary_cover` зажат длиной истории: оставь её в памяти — и на
        # первых же репликах нового разговора она снова стала бы действующей,
        # накрыв собой его начало. Поэтому смотрим не на пустую таблицу,
        # а на отросшую заново историю: она обязана уехать в модель целиком.
        revived.forget()
        _ask(revived, 2, "новый вопрос {i}")
        assert revived.summaries == [], revived.summaries
        assert revived.summary_cover() == 0, revived.summary_cover()
        fresh_prompt = revived.build_prompt("ещё")
        assert len(fresh_prompt) == 5, len(fresh_prompt)
        assert fresh_prompt[0]["content"] == "новый вопрос 0", fresh_prompt[0]
    cells = sum(len(row) for row in CLEANUP_TABLE.values())
    wiped = sum(sum(row) for row in CLEANUP_TABLE.values())
    return (
        f"сводка на {before[0][0]} реплик пережила перезапуск и обмен после него; "
        f"{cells} клеток «слой × путь очистки» на непустой сцене — "
        f"{wiped} уносят, {cells - wiped} оставляют"
    )


@check("записи: различающие типы, partial PATCH, устойчивые id и область working")
def check_record_routes():
    with TestClient(main.app) as client:
        mine = new_agent(client)
        neighbour = new_agent(client)
        # Exclusive kinds prove that each route has its own allowed list.
        cases = [(f"/api/agents/{mine}/working", "question", "limit", "profile"),
                 ("/api/memory", "profile", "knowledge", "question"),
                 ("/api/invariants", "stack", "architecture", "decision")]
        for url, kind, other_kind, foreign_kind in cases:
            for bad in ({"content": "без типа"}, {"kind": foreign_kind, "content": "чужой тип"},
                        {"kind": kind, "content": " "}, {"kind": kind},
                        {"kind": kind, "content": "x", "author": "human"}):
                result = client.post(url, json=bad)
                assert result.status_code == 400, (url, bad, result.text)
            body = {"kind": kind, "content": "  запись  "}
            if url == "/api/invariants":
                for banned in ("Java", {"word": "Java"}, [" "], [7]):
                    assert client.post(url, json={**body, "banned": banned}).status_code == 400
                body["banned"] = [" Java ", "C++"]
            result = client.post(url, json=body)
            assert result.status_code == 200, result.text
            record = result.json()
            assert record["kind"] == kind and record["content"] == "запись", record
            assert set(record) == {"seq", "kind", "content", "at"} | ({"banned"} if url == "/api/invariants" else set())
            if url == "/api/invariants":
                assert record["banned"] == ["Java", "C++"]
            item = f"{url}/{record['seq']}"
            changed = client.patch(item, json={"kind": other_kind}).json()
            assert (changed["seq"], changed["kind"], changed["content"]) == (record["seq"], other_kind, "запись")
            updated = client.patch(item, json={"content": "новая"}).json()
            assert (updated["seq"], updated["kind"], updated["content"]) == (record["seq"], other_kind, "новая")
            for bad in ({}, {"kind": foreign_kind}, {"seq": 99}, {"content": " "}):
                assert client.patch(item, json=bad).status_code == 400, (url, bad)
            assert client.patch(url + "/99999", json={"content": "x"}).status_code == 404
            assert client.delete(url + "/99999").status_code == 404
            if url == "/api/invariants":
                assert client.patch(item, json={"banned": "Java"}).status_code == 400
                cleared = client.patch(item, json={"banned": []}).json()
                assert cleared["banned"] == [] and cleared["content"] == "новая"
            if url.endswith("/working"):
                foreign = f"/api/agents/{neighbour}/working/{record['seq']}"
                assert client.patch(foreign, json={"content": "чужая"}).status_code == 404
                assert client.delete(foreign).status_code == 404
                assert client.get(url).json()["records"][0]["content"] == "новая"
            twin = client.post(url, json={"kind": other_kind, "content": "тот же тип"}).json()
            listed = client.get(url).json()["records"]
            assert [r["content"] for r in listed] == ["новая", "тот же тип"], listed
            assert client.delete(f"{url}/{twin['seq']}").status_code == 200
            fresh = client.post(url, json={"kind": kind, "content": "после удаления"}).json()
            assert fresh["seq"] > twin["seq"] > record["seq"]
            assert client.delete(item).json() == {"deleted": record["seq"]}
            assert client.delete(item).status_code == 404
    return "три ручки проверены отдельно; рабочая память чужого чата недоступна"


@check("рабочая память: окно не удаляет записи, снимок/слот согласован, файл переживает reopen")
def check_working_memory():
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИС", strategy="window", keep_last=2)
        url = f"/api/agents/{agent_id}/working"
        record = client.post(url, json={"kind": "question", "content": "успеем к маю?"}).json()
        _talk(client, agent_id, 4)
        agent = REGISTRY.require(agent_id)
        start = _frame(sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": "ещё"}).text), "start")
        sent = start["resolved_messages"]
        assert start["working_at"] == 1 and start["summary_at"] is None
        assert sent[1] == {"role": "user", "content": "[факты о разговоре]\nоткрытый вопрос: успеем к маю?\n[конец фактов о разговоре]"}
        assert sent[2]["content"] == "вопрос 3" and sent[-1]["content"] == "ещё"
        assert len(agent.history) == 10 and agent.history[-1].metrics["dropped"] == 6
        assert not _service_calls() and len(_stub.CALLS) == 5
        assert client.get(url).json()["records"] == [record]
        assert client.get(f"/api/agents/{agent_id}/memory").json()["working"]["records"] == [record]
    store = Store(_temp_db("working-reopen")).init()
    agent = agent_module.Agent(AgentSpec(label="working", model="stub/model"), store=store)
    agent.add_working_record("question", "успеем к маю?")
    _ask(agent, 1)
    before = store.list_working(agent.id)
    assert before and before[0]["content"] == "успеем к маю?"
    with _restarted(store, agent.id) as (fresh, revived):
        assert revived.working == before
        assert "успеем к маю?" in revived.build_prompt("ещё")[0]["content"]
        _ask(revived, 1)
        assert fresh.list_working(revived.id) == before
        assert not _columns_holding(fresh.conn, "успеем к маю?", ("messages", "summaries"), exclude=("messages.request_bodies",))
        revived.forget()
        assert fresh.list_working(revived.id) == [] and revived.working == []
        assert revived.build_prompt("после очистки") == [{"role": "user", "content": "после очистки"}]
        neighbour = agent_module.Agent(AgentSpec(label="neighbour", model="stub/model"), store=fresh)
        neighbour.add_working_record("goal", "чужая цель")
        foreign = fresh.list_working(neighbour.id)[0]
        assert fresh.update_working(revived.id, foreign["seq"], kind="goal", content="взлом") is None
        assert fresh.delete_working(revived.id, foreign["seq"]) is False
        assert fresh.list_working(neighbour.id) == [foreign]
    return "записи поверх window, без служебных вызовов; reopen и владение отдельно от HTTP"


@check("ветка уносит ровно просимое, живёт независимо и переживает перезапуск")
def check_branch_independent():
    """Ветвление Дня 10: «сохраните checkpoint, создайте 2 ветки от одного
    места, продолжите диалог в каждой независимо, переключайтесь между ними».

    Ветка — **отдельный чат**: своя строка в `sessions`, свой id от базы, своя
    история под своим `session_id`. Схему `messages` ветвление не тронуло
    вовсе. Отсюда и разделы проверки:

    * **унесено ровно просимое** — первые `at` сообщений и копия конфига,
      с номерами от нуля и без дыр;
    * **сводка едет только своя** — она заменяет собой начало истории,
      и заменять им можно ровно то начало, которое в ветке есть: ветка,
      унёсшая половину разговора, не вправе унести сводку про весь.
      А записи рабочей памяти едут все: их вписал человек, они ничего
      не заменяют, и задача у ветки та же;
    * **дальше чат сам по себе** — продолжение ветки не трогает историю
      родителя, а родителя — историю ветки; удаление родителя ветку
      не удаляет; перезапуск процесса родства не теряет, и обмен после него
      его не затирает — `save_history` переписывает `messages` целиком,
      а `branches` не трогает.

    Отдельного раздела про «переключайтесь между ветками» здесь нет
    намеренно: переключиться — это открыть чат из списка слева, ветка там уже
    есть, и то, что открытый чат отдаёт свою историю, а не чужую, стережёт
    `check_session_isolation`. Писать это второй раз значило бы проверять
    список, а не ветвление.
    """
    # --- две ветки от одного места, и каждая живёт своей жизнью ------------
    _stub.install(reply=lambda messages, i: "ответ на " + messages[-1]["content"])
    with TestClient(main.app) as client:
        # Память выключена: этот раздел про копию истории и конфига, и
        # врезка встала бы в каждый промпт, который здесь сверяется дословно.
        # Копию самой памяти разбирает раздел ниже, там она включена.
        parent = new_agent(
            client, label="родитель", system="СИС", temperature=0.5,
            extra_body={"provider": {"order": ["stub"]}},
        )
        _talk(client, parent, 3)
        spoken = len(_stub.CALLS)
        store = REGISTRY.store
        # Снимок ленты родителя **до** ветвления. Это единственное окно,
        # в котором видно, тронуло ли ветвление чужую сессию: заговори
        # родитель снова — и `persist()` живого объекта перепишет `messages`
        # из памяти целиком, затерев любое повреждение в базе.
        before_fork = [row[2] for row in store.message_rows(parent)]
        assert len(before_fork) == 6, before_fork

        # Чекпойнт — два первых обмена, четыре сообщения.
        first = client.post(f"/api/agents/{parent}/fork", json={"at": 4})
        assert first.status_code == 200, first.text
        body = first.json()
        # Ответ — в том же виде, что отдаёт создание: ветка и есть обычный чат,
        # и клиенту незачем различать, как он появился.
        assert body["created"] == 1 and len(body["agents"]) == 1, body
        one = body["agents"][0]
        two = client.post(f"/api/agents/{parent}/fork", json={"at": 4}).json()["agents"][0]

        # К модели ветвление не ходит: оно копирует, а не спрашивает.
        assert len(_stub.CALLS) == spoken, "ветвление сходило к модели"

        # Для родителя ветвление — операция **только на чтение**: лента
        # в базе та же, и родства ему не записано. Смотрим сразу, пока никто
        # не заговорил снова. Одна лишняя строка в записи ветки —
        # `save_history(parent_id, ...)` — стёрла бы родителю хвост
        # разговора насовсем: `save_history` начинается с `DELETE`, и
        # перезапустись процесс сразу после ветвления, восстанавливать было
        # бы нечего. После первой же реплики родителя это уже не видно.
        assert [row[2] for row in store.message_rows(parent)] == before_fork, (
            "ветвление тронуло ленту родителя"
        )
        assert store.load_branch(parent) is None, "ветвление записало родство родителю"
        # И то же окно с другой стороны: в ветке лежит ровно унесённое
        # и ничего чужого.
        assert [row[2] for row in store.message_rows(one["id"])] == before_fork[:4], (
            store.message_rows(one["id"])
        )
        assert store.message_rows(two["id"]) == store.message_rows(one["id"]), (
            "две ветки от одного места унесли разное"
        )

        assert one["id"] not in (parent, two["id"]) and two["id"] != parent, (one, two)
        # Имена разные: две ветки от одного места — это ровно то, о чём
        # просит задание, и в списке слева их надо отличать друг от друга.
        assert one["label"] != two["label"], (one["label"], two["label"])
        for branch in (one, two):
            # Пометка ветки: от кого и с какого места. Имени родителя в ней
            # нет — только id: имя меняют из списка, и копия разошлась бы с ним.
            assert branch["branch"] == {"parent_id": parent, "forked_at": 4}, branch["branch"]
            assert branch["history_len"] == 4, branch["history_len"]
            # Конфиг скопирован целиком, а не собран из умолчаний.
            assert branch["system"] == "СИС" and branch["temperature"] == 0.5, branch
            assert branch["extra_body"] == {"provider": {"order": ["stub"]}}, branch["extra_body"]
        # У родителя пометки нет: он ничей потомок.
        assert client.get(f"/api/agents/{parent}").json()["branch"] is None

        # Копия конфига — копия, а не ссылка: правка у ветки не задевает
        # родителя. Вглубь копирует конструктор агента, одним местом на всех.
        live_one, live_parent = REGISTRY.require(one["id"]), REGISTRY.require(parent)
        assert live_one.spec.extra_body is not live_parent.spec.extra_body, "конфиг общий"
        client.patch(f"/api/agents/{one['id']}", json={"strategy": "window", "keep_last": 2})
        assert live_parent.spec.strategy == "full", live_parent.spec.strategy

        # Продолжаем каждую ветку и родителя — и смотрим, что уехало в модель.
        client.post(f"/api/agents/{two['id']}/messages", json={"text": "во второй ветке"})
        sent = [m["content"] for m in _stub.CALLS[-1]["messages"]]
        assert sent == [
            "СИС", "вопрос 0", "ответ на вопрос 0", "вопрос 1", "ответ на вопрос 1",
            "во второй ветке",
        ], sent

        client.post(f"/api/agents/{parent}/messages", json={"text": "у родителя"})
        asked = " ".join(m["content"] for m in _stub.CALLS[-1]["messages"])
        assert "во второй ветке" not in asked, f"родитель видит ветку: {asked}"
        assert "вопрос 2" in asked, asked

        # И в базе три разные ленты: ни одна запись не затёрла чужую.
        # `save_history` начинается с `DELETE` по `session_id` — общая сессия
        # тут стоила бы разговора.
        rows = {ident: store.message_rows(ident) for ident in (parent, one["id"], two["id"])}
        assert [r[2] for r in rows[parent]] == [
            "вопрос 0", "ответ на вопрос 0", "вопрос 1", "ответ на вопрос 1",
            "вопрос 2", "ответ на вопрос 2", "у родителя", "ответ на у родителя",
        ], [r[2] for r in rows[parent]]
        assert [r[2] for r in rows[two["id"]]] == [
            "вопрос 0", "ответ на вопрос 0", "вопрос 1", "ответ на вопрос 1",
            "во второй ветке", "ответ на во второй ветке",
        ], [r[2] for r in rows[two["id"]]]
        # Первая ветка не тронута вовсе: в ней по-прежнему ровно унесённое.
        assert [r[2] for r in rows[one["id"]]] == [
            "вопрос 0", "ответ на вопрос 0", "вопрос 1", "ответ на вопрос 1",
        ], [r[2] for r in rows[one["id"]]]
        # Номера у копии — от нуля и без дыр, как у любой истории.
        assert [r[0] for r in rows[two["id"]]] == list(range(6)), rows[two["id"]]

        # Переключаться между ветками нечем: обе уже в списке слева, и
        # открываются тем же путём, что любой чат.
        listed = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
        assert {parent, one["id"], two["id"]} <= set(listed), sorted(listed)
        assert listed[one["id"]]["branch"]["forked_at"] == 4, listed[one["id"]]
        assert listed[parent]["branch"] is None, listed[parent]

        # Тот же список, но **холодным** путём: выгружаем ветку из памяти —
        # так выглядит и вытеснение по потолку, и перезапуск сервера, — и
        # читаем список снова. Пометка обязана быть та же, а приезжает она
        # другой ветвью кода: у живого чата её отдаёт он сам, у выгруженного
        # — строка из базы. Не проверь холодную, и пометка ветки пропадала бы
        # после перезапуска, оставаясь на месте до него.
        assert REGISTRY._unload(one["id"]) is True, "ветка не была живой"
        cold = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
        assert cold[one["id"]]["branch"] == {"parent_id": parent, "forked_at": 4}, cold[one["id"]]
        assert cold[one["id"]]["history_len"] == 4, cold[one["id"]]
        assert cold[parent]["branch"] is None, cold[parent]

        # Ветка от ветки. Родство называет того, от кого отделились, а не
        # деда: иначе пометка внучки указывала бы на живого деда, а после
        # удаления настоящего родителя говорила бы «ветка от удалённого
        # чата» про чат, который на месте. Ветвимся от только что
        # выгруженной ветки — заодно видно, что поднятая из базы ветка
        # остаётся веткой и годится в родители.
        grand = client.post(f"/api/agents/{one['id']}/fork", json={"at": 2}).json()["agents"][0]
        assert grand["branch"] == {"parent_id": one["id"], "forked_at": 2}, grand["branch"]
        assert [row[2] for row in store.message_rows(grand["id"])] == [
            "вопрос 0", "ответ на вопрос 0",
        ], store.message_rows(grand["id"])
        # А у самой ветки родство прежнее: ветвление от неё её не переписало.
        assert store.load_branch(one["id"]) == {"parent_id": parent, "forked_at": 4}, (
            store.load_branch(one["id"])
        )

        # Кривое `N` — 400 с текстом, а не 500 и не молчаливый зажим.
        history_len = len(live_parent.history)
        for bad in ({"at": -1}, {"at": history_len + 1}, {"at": "два"}, {"at": True}, {},
                    {"at": 2, "label": "нельзя"}):
            answer = client.post(f"/api/agents/{parent}/fork", json=bad)
            assert answer.status_code == 400, (bad, answer.status_code, answer.text)
            assert answer.json()["detail"], bad
        # Ноль — не кривое: ветка без истории, но с конфигом родителя.
        empty = client.post(f"/api/agents/{parent}/fork", json={"at": 0}).json()["agents"][0]
        assert empty["history_len"] == 0 and empty["system"] == "СИС", empty
        assert empty["branch"] == {"parent_id": parent, "forked_at": 0}, empty["branch"]

        # И чат, в котором ещё не сказано ни слова, ветвится так же: унести
        # нечего, но конфиг у ветки его. Это не край, а первый день чата —
        # падать на нём нельзя, а `at = 0` у чата с историей этого случая
        # не покрывает: там длина не ноль.
        fresh = new_agent(client, label="свежий")
        blank = client.post(f"/api/agents/{fresh}/fork", json={"at": 0})
        assert blank.status_code == 200, blank.text
        sprout = blank.json()["agents"][0]
        assert sprout["history_len"] == 0, sprout["history_len"]
        assert sprout["branch"] == {"parent_id": fresh, "forked_at": 0}, sprout["branch"]
        # А `at = 1` у такого чата — 400: уносить нечего.
        assert client.post(f"/api/agents/{fresh}/fork", json={"at": 1}).status_code == 400

    # --- ветвление у занятого родителя ------------------------------------
    #
    # Ручка обещает, что занятость родителя ветвлению не мешает: история
    # не меняется до конца обмена, и ветка унесёт то, что записано прямо
    # сейчас. Обещание без утверждения отменяется одной строкой — 409
    # у занятого, «занят — уноси всю историю», «занят — уноси на пару меньше»,
    # — и набор этого не заметит. Поэтому ветвимся **посреди** ответа,
    # и точку берём меньше длины истории: сравнение «унесено ровно `at`»
    # тогда отличает её и от полной истории, и от укороченной.
    async def fork_mid_answer():
        from httpx import ASGITransport, AsyncClient

        _stub.reset()
        _stub.install(
            reply=lambda messages, i: "ответ на " + messages[-1]["content"],
            chunks=8,
            delay=0.03,
        )
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://stub") as client:
            made = await client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "занятый"}}
            )
            busy_id = made.json()["agents"][0]["id"]
            for i in range(2):
                await client.post(f"/api/agents/{busy_id}/messages", json={"text": f"реплика {i}"})
            talking = asyncio.create_task(
                client.post(f"/api/agents/{busy_id}/messages", json={"text": "долгий вопрос"})
            )
            await asyncio.sleep(0.05)
            live = REGISTRY.require(busy_id)
            assert live.busy, "родитель не занят — проверять нечего"
            # Это и есть то, на чём держится обещание: история не растёт
            # до конца обмена, поэтому ветвиться посреди ответа безопасно.
            assert len(live.history) == 4, len(live.history)
            forked = await client.post(f"/api/agents/{busy_id}/fork", json={"at": 2})
            answered = await talking
        assert answered.status_code == 200, answered.text
        return forked, live

    forked, busy_parent = asyncio.run(fork_mid_answer())
    assert forked.status_code == 200, forked.text
    mid = forked.json()["agents"][0]
    assert mid["history_len"] == 2, mid["history_len"]
    assert mid["branch"] == {"parent_id": busy_parent.id, "forked_at": 2}, mid["branch"]
    assert [row[2] for row in REGISTRY.store.message_rows(mid["id"])] == [
        "реплика 0", "ответ на реплика 0",
    ], REGISTRY.store.message_rows(mid["id"])
    # А обмен родителя тем временем дописался целиком: ветвление его
    # не оборвало и не потеряло.
    assert len(busy_parent.history) == 6, len(busy_parent.history)
    assert busy_parent.history[-1].content == "ответ на долгий вопрос", busy_parent.history[-1]

    # --- врезка едет только та, что покрывает одно унесённое ---------------
    #
    # Обрезать сводку по смыслу нельзя — она связный текст, а выписка снимок
    # без привязки к репликам. Поэтому правило одно на обе и по границе: едет
    # то, что покрывает только унесённое. Сводки инкрементальны, и префикс их
    # списка сам по себе готовая сводка своего начала; выписке остаётся
    # «всё или ничего».
    from app.registry import AgentRegistry

    _stub.reset()
    _stub.install(reply=_service_aware)
    path = _temp_db("branch-cut")
    store = Store(path).init()
    reg = AgentRegistry(store=store)
    folded = agent_module.Agent(
        AgentSpec(label="сжатый", model="stub/model", strategy="summary",
                  keep_last=KEEP, compress_every=EVERY),
        store=store,
    )
    _ask(folded, 9)
    assert folded.summary_cover() == 10 and len(folded.history) == 18, folded.summary_cover()

    # Границу берём **вплотную**: на единицу и ошибаются в таком сравнении,
    # а ветка в четырёх сообщениях от границы сдвига на единицу не заметит.
    edge = reg.fork(folded, 10, label="ровно по границе сводки")
    near = reg.fork(folded, 9, label="на одно раньше границы")
    far = reg.fork(folded, 18, label="ветка после сводки")
    assert (len(edge.history), len(near.history), len(far.history)) == (10, 9, 18), (
        len(edge.history), len(near.history), len(far.history)
    )
    # `upto == at`: сводка покрывает ровно унесённое — едет. Хвоста у такой
    # ветки нет вовсе, и свёрнутое плюс хвост по-прежнему равно её истории.
    assert [item["upto"] for item in edge.summaries] == [10], edge.summaries
    assert edge.summary_cover() == 10, edge.summary_cover()
    assert len(edge.build_prompt("ещё")) == 2, edge.build_prompt("ещё")
    # `upto == at + 1`: одной из покрытых сводкой реплик в ветке уже нет —
    # не едет. Резать в такой ветке нечем, её история уезжает целиком.
    assert near.summaries == [], near.summaries
    assert store.load_summaries(near.id) == [], store.load_summaries(near.id)
    assert near.summary_cover() == 0 and len(near.build_prompt("ещё")) == 10, near.summary_cover()
    # А у дальней сводка своя и покрывает ровно унесённое.
    assert [item["upto"] for item in far.summaries] == [10], far.summaries
    assert [item["upto"] for item in store.load_summaries(far.id)] == [10]
    far_prompt = far.build_prompt("ещё")
    assert "СВОДКА" in far_prompt[0]["content"], far_prompt[0]
    assert far.summary_cover() + (len(far_prompt) - 2) == len(far.history) == 18, far_prompt

    # Реплики у ветки свои: общий объект сделал бы два чата одним в той
    # части, которую они делят.
    assert far.history[0] is not folded.history[0], "реплика у ветки и родителя — один объект"

    # И продолжение родителя ветку не трогает **и на уровне врезки**: список
    # сводок у неё свой. Общий дал бы ветке сводку про реплики, которых в ней
    # нет, — молча, следующим сворачиванием родителя.
    _ask(folded, range(9, 14))
    assert len(folded.summaries) == 2 and folded.summary_cover() == 20, folded.summaries
    assert [item["upto"] for item in far.summaries] == [10], far.summaries
    assert far.summary_cover() == 10 and len(far.history) == 18, far.summary_cover()
    assert far.build_prompt("ещё") == far_prompt, "сворачивание родителя сменило промпт ветки"

    listing = agent_module.Agent(
        AgentSpec(label="память", model="stub/model", strategy="window", keep_last=KEEP),
        store=store,
    )
    _ask(listing, 5)
    listing.add_working_record("goal", "собрать ТЗ")
    listing.add_working_record("limit", "только Kotlin")
    said = [(r["kind"], r["content"]) for r in listing.working]
    assert len(said) == 2, said

    # Рабочая память едет в ветку **вся**, и границы у неё нет ни при каком
    # `at`. Раньше была — по `upto`, докуда её дочитал служебный вызов: записи
    # были пересказом прочитанных реплик, и ветка, унёсшая половину разговора,
    # не вправе была унести память про весь. Теперь их вписал человек: это его
    # слова о задаче, они не заменяют собой ни одной реплики, а ветка
    # продолжает ту же задачу.
    early = reg.fork(listing, 2, label="ветка от второй реплики")
    late = reg.fork(listing, 10, label="ветка от конца")
    for branch in (early, late):
        assert [(r["kind"], r["content"]) for r in branch.working] == said, branch.working
        assert store.list_working(branch.id) == branch.working, store.list_working(branch.id)
        assert branch.build_prompt("ещё")[0]["content"].startswith("[факты о разговоре]"), (
            "память не встала в промпт ветки"
        )
    # Номера у копий свои: номер принадлежит одному чату, и две записи под
    # одним номером в разных разговорах — ровно та путаница, из-за которой
    # он и стал сквозным на всю базу.
    assert {r["seq"] for r in late.working}.isdisjoint(
        {r["seq"] for r in listing.working}
    ), (late.working, listing.working)
    assert {r["seq"] for r in late.working}.isdisjoint({r["seq"] for r in early.working}), (
        late.working, early.working
    )
    # Копия глубокая: правка у ветки не видна родителю.
    late.add_working_record("limit", "чужое ограничение")
    assert len(listing.working) + 1 == len(late.working), "память общая"
    assert len(early.working) == 2, early.working

    branch_id, parent_id = far.id, folded.id

    # --- перезапуск, обмен после него и исходы очистки ----------------------
    with _restarted(store, branch_id) as (again, revived):
        assert revived.branch == {"parent_id": parent_id, "forked_at": 18}, revived.branch
        assert len(revived.history) == 18, len(revived.history)
        assert revived.spec.strategy == "summary", revived.spec.strategy
        assert [t.content for t in revived.history if t.role == "user"] == [
            f"вопрос {i}" for i in range(9)
        ], [t.content for t in revived.history if t.role == "user"]

        # Обмен после перезапуска родство не стирает: `save_history`
        # переписывает `messages` целиком, а `branches` не трогает вовсе.
        asyncio.run(drain(revived.ask("после перезапуска")))
        assert again.load_branch(branch_id) == {"parent_id": parent_id, "forked_at": 18}
        assert len(revived.history) == 20, len(revived.history)

        # Удаление родителя ветку **не** удаляет: она самостоятельный чат.
        # Пометка остаётся честной — родителя больше нет, и имени у него нет;
        # кто это говорит, решает клиент по отсутствию id в списке.
        assert again.delete_session(parent_id) is True
        assert again.load_branch(branch_id) == {"parent_id": parent_id, "forked_at": 18}, (
            "удаление родителя унесло родство ветки"
        )
        assert len(again.message_rows(branch_id)) == 20, "ветка ушла вслед за родителем"
        orphan = {row["id"]: row for row in again.list_sessions()}[branch_id]
        assert orphan["branch"]["parent_id"] == parent_id, orphan["branch"]

        # `forget()` родства не трогает: это не содержимое разговора, а то,
        # откуда чат взялся. Ветка, забывшая историю, осталась веткой — и это
        # одна из трёх клеток, где очистка обязана слой **оставить**. Путей
        # у родства два, а не три, и все три его клетки вместе с остальными
        # девятью проходит `check_summary_apart_and_cleanup`; здесь остаётся
        # то, чего в таблице нет: забытая история и живая пометка рядом.
        revived.forget()
        assert again.message_rows(branch_id) == [], again.message_rows(branch_id)
        assert again.load_branch(branch_id) == {"parent_id": parent_id, "forked_at": 18}
    return (
        "две ветки от одного места унесли по 4 сообщения, лента родителя "
        "сразу после ветвления та же; ветка от ветки называет родителя, "
        "а не деда; пометка та же и у выгруженной; ветвление посреди ответа "
        "унесло ровно записанное; сводка с границей 10 уехала в ветку на 10 "
        "и не уехала в ветку на 9, а записи рабочей памяти — в обе ветки "
        "целиком; "
        "перезапуск, удаление родителя и forget() ветка пережила вместе "
        "со своим родством"
    )


@check("долговременная память: слой глобальный, пишет в него только человек")
def check_long_term_memory():
    from app.agent import Agent

    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        assert client.get("/api/memory").json() == {"total": 0, "records": []}, "память не пуста"
        plain = new_agent(client, system="СИС")
        start = _frame(_frames(client, plain, "первый"), "start")
        assert start["memory_at"] is None, start["memory_at"]
        assert start["working_at"] is None, start["working_at"]
        assert [m["role"] for m in _stub.CALLS[-1]["messages"]] == ["system", "user"], _stub.CALLS[-1]

        profile = client.post(
            "/api/memory", json={"kind": "profile", "content": "  пишу на Kotlin  "}
        ).json()
        decision = client.post(
            "/api/memory", json={"kind": "decision", "content": "оплата только картой"}
        ).json()
        knowledge = client.post(
            "/api/memory", json={"kind": "knowledge", "content": "релиз в мае"}
        ).json()
        assert profile["content"] == "пишу на Kotlin", profile
        assert profile["seq"] < decision["seq"] < knowledge["seq"], (profile, decision, knowledge)
        listed = client.get("/api/memory").json()
        assert listed["total"] == 3 and len(listed["records"]) == 3, listed

        _stub.reset()
        client.post(f"/api/agents/{plain}/messages", json={"text": "второй"})
        sent = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "user", "assistant", "user"], sent
        block = sent[1]["content"]
        assert block.startswith("[долговременная память]"), block
        assert block.endswith("[конец долговременной памяти]"), block
        assert "о собеседнике: пишу на Kotlin" in block, block
        assert "решение: оплата только картой" in block, block
        assert "факт: релиз в мае" in block, block
        assert len([m for m in sent if m["role"] == "system"]) == 1, sent
        assert "как есть" not in block, block

        bare = new_agent(client, system="")
        start = _frame(_frames(client, bare, "голый"), "start")
        assert start["memory_at"] == 0, start["memory_at"]
        assert start["working_at"] is None, start["working_at"]
        assert start["summary_at"] is None, start["summary_at"]
        assert start["resolved_messages"][0]["content"].startswith("[долговременная память]")

        _stub.install(reply=_service_aware)
        both = new_agent(
            client, system="СИС", strategy="summary", keep_last=2, compress_every=2
        )
        client.post(
            f"/api/agents/{both}/working", json={"kind": "goal", "content": "собрать ТЗ"}
        )
        _talk(client, both, 3)
        _stub.reset()
        frames = _frames(client, both, "вопрос 3")
        start = _frame(frames, "start")
        assert start["memory_at"] == 1, start["memory_at"]
        assert start["working_at"] == 2, start["working_at"]
        assert start["summary_at"] == 3, start["summary_at"]
        assert start["strategy"] == "summary", start["strategy"]
        prompt = start["resolved_messages"]
        assert prompt[start["memory_at"]]["content"].startswith("[долговременная память]"), prompt
        assert prompt[start["working_at"]]["content"].startswith("[факты о разговоре]"), prompt
        assert "пересказ начала разговора" in prompt[start["summary_at"]]["content"], prompt
        assert [m["role"] for m in prompt] == [
            "system", "user", "user", "user", "user", "assistant", "user"
        ], prompt
        assert prompt[-1]["content"] == "вопрос 3", prompt[-1]

        went = [_service_kind(call["messages"]) for call in _stub.CALLS]
        assert went == ["summary", None], went
        promised = [e["strategy"] for e in frames if e["event"] == "compressing"]
        assert promised == ["summary"], promised

        folding = _service_calls("summary")[-1]["messages"]
        assert not any("[долговременная память]" in m["content"] for m in folding), folding
        assert not any("о собеседнике: пишу на Kotlin" in m["content"] for m in folding), folding
        assert not any("цель: собрать ТЗ" in m["content"] for m in folding), folding

        _stub.reset()
        _stub.install(reply=lambda m, i: f"ответ {i}")
        store = REGISTRY.store
        real_list, reads = store.list_memory, []

        def counted():
            reads.append(1)
            return real_list()

        with patch.object(store, "list_memory", counted):
            client.post(f"/api/agents/{plain}/messages", json={"text": "а у меня память есть"})
        assert len(reads) == 1, f"чтений памяти за обмен: {len(reads)}, а должно быть одно"
        assert any(
            "[долговременная память]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]

        layers = client.get(f"/api/agents/{both}/memory").json()
        assert layers["short_term"]["messages"] == len(REGISTRY.require(both).history), layers
        assert layers["short_term"]["summaries"], layers["short_term"]
        assert layers["short_term"]["summaries"][0]["upto"] == 2, layers["short_term"]
        assert "summaries" not in layers["working"], layers["working"]
        assert [(r["kind"], r["content"]) for r in layers["working"]["records"]] == [
            ("goal", "собрать ТЗ")
        ], layers["working"]
        assert [r["seq"] for r in layers["long_term"]["records"]] == [
            r["seq"] for r in client.get("/api/memory").json()["records"]
        ], layers["long_term"]
        other = client.get(f"/api/agents/{bare}/memory").json()
        assert "enabled" not in other["long_term"], other["long_term"]
        assert other["long_term"]["records"] == layers["long_term"]["records"], other["long_term"]
        assert other["working"]["records"] == [], other["working"]

        store = REGISTRY.store
        columns = {r["name"] for r in store.conn.execute("PRAGMA table_info(memory)")}
        assert columns == {"seq", "kind", "content", "at"}, columns
        assert "session_id" not in columns, "у глобального слоя завёлся владелец"
        assert client.post(
            "/api/memory", json={"kind": "profile", "content": "х", "author": "human"}
        ).status_code == 400
        working_columns = {
            r["name"] for r in store.conn.execute("PRAGMA table_info(working_memory)")
        }
        assert working_columns == {"seq", "session_id", "kind", "content", "at"}, working_columns
        spilled = _columns_holding(
            store.conn, "пишу на Kotlin", ("messages", "summaries", "working_memory"), exclude=("messages.request_bodies",)
        )
        assert not spilled, f"долговременная память утекла в чужие таблицы: {spilled}"

        before_fork = client.get("/api/memory").json()["total"]
        assert before_fork, "память пуста — копировать нечего, проверять тоже"
        branch = client.post(f"/api/agents/{plain}/fork", json={"at": 2}).json()["agents"][0]["id"]
        _stub.reset()
        client.post(f"/api/agents/{branch}/messages", json={"text": "вопрос ветки"})
        assert any(
            "о собеседнике: пишу на Kotlin" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]
        assert client.get("/api/memory").json()["total"] == before_fork, (
            "ветвление завело копии записей долговременной памяти"
        )

    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("memory")
    store = Store(path).init()
    spec = AgentSpec(label="с памятью", model="stub/model")
    agent = Agent(spec, store=store)
    store.add_memory("profile", "пишу на Kotlin")
    asyncio.run(drain(agent.ask("вопрос")))
    kept = store.list_memory()[0]
    assert (kept["kind"], kept["content"]) == ("profile", "пишу на Kotlin"), kept
    assert "[долговременная память]" in agent.build_prompt("ещё")[0]["content"], "врезка не встала"

    agent.forget()
    assert store.list_memory() == [kept], store.list_memory()
    assert "[долговременная память]" in agent.build_prompt("после forget")[0]["content"]

    import sqlite3

    old_path = _temp_db("memory-old")
    os.makedirs(os.path.dirname(old_path), exist_ok=True)
    old = sqlite3.connect(old_path)
    old.executescript(
        """
        CREATE TABLE memory (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            content TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'human',
            at REAL NOT NULL
        );
        INSERT INTO memory (kind, content, author, at)
            VALUES ('profile', 'набрано руками', 'human', 1.0);
        CREATE TABLE working_memory (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            kind TEXT NOT NULL, content TEXT NOT NULL, author TEXT NOT NULL,
            at REAL NOT NULL
        );
        INSERT INTO working_memory (session_id, kind, content, author, at)
            VALUES ('ag_00001', 'goal', 'цель из вчерашней базы', 'agent', 1.0);
        CREATE TABLE working_state (
            session_id TEXT PRIMARY KEY, upto INTEGER NOT NULL, metrics TEXT,
            at REAL NOT NULL
        );
        INSERT INTO working_state VALUES ('ag_00001', 4, NULL, 1.0);
        CREATE TABLE facts (
            session_id TEXT NOT NULL, seq INTEGER NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, upto INTEGER NOT NULL, metrics TEXT, at REAL NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        INSERT INTO facts VALUES ('ag_00001', 0, 'цель', 'снимок Дня 10', 2, NULL, 1.0);
        """
    )
    old.commit()
    old.close()

    with _reopened(old_path) as migrated:
        assert migrated.list_memory() == [
            {"seq": 1, "kind": "profile", "content": "набрано руками", "at": 1.0}
        ], migrated.list_memory()
        assert migrated.list_working("ag_00001") == [
            {"seq": 1, "kind": "goal", "content": "цель из вчерашней базы", "at": 1.0}
        ], migrated.list_working("ag_00001")
        for table, columns in (
            ("memory", {"seq", "kind", "content", "at"}),
            ("working_memory", {"seq", "session_id", "kind", "content", "at"}),
        ):
            left = {r["name"] for r in migrated.conn.execute(f"PRAGMA table_info({table})")}
            assert left == columns, (table, left)
        tables = {
            row["name"]
            for row in migrated.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "facts" not in tables, tables
        assert "working_state" not in tables, tables
        assert {"memory", "working_memory"} <= tables, tables
        assert migrated._migrate() == [], migrated._migrate()
        fresh_row = migrated.add_memory("knowledge", "после миграции")
        assert fresh_row["seq"] == 2, fresh_row

    with _restarted(store, agent.id) as (again, revived):
        assert again.list_memory() == [kept], again.list_memory()
        assert "[долговременная память]" in revived.build_prompt("после перезапуска")[0]["content"]

        homeless = Agent(AgentSpec(label="без базы", model="stub/model"))
        assert homeless.memory_items() == [], homeless.memory_items()
        assert homeless.prompt_slots()["memory_at"] is None, homeless.prompt_slots()
        assert homeless.build_prompt("вопрос") == [{"role": "user", "content": "вопрос"}]

        again.clear()
        assert again.list_memory() == [], again.list_memory()
        assert revived.memory_items() == [], revived.memory_items()
        assert revived.working_items() == [], revived.working_items()
        assert revived.build_prompt("после очистки") == [
            {"role": "user", "content": "после очистки"}
        ], revived.build_prompt("после очистки")

    return "глобальная память: промпт/слоты, один снимок, миграция без потери записей и reopen"


@check("память меняет ответ: тот же вопрос с врезкой и без неё расходится")
def check_memory_changes_answer():
    """Единственный пункт задания Дня 11, который просили именно **проверить**:
    «как это влияет на ответы».

    Все прочие проверки памяти смотрят на **запрос** — что уехало в модель,
    каким по счёту сообщением, с какой подписью. Здесь впервые смотрим
    на **ответ**: тот же вопрос, те же настройки, пустой слой против
    непустого — и два разных ответа.

    Сравнение шло по выключателю памяти, пока выключатель был. Его не стало,
    и сравнение стало честнее: проверяется то, ради чего слой заведён, —
    **запись**, а не настройка. Вписали руками — ответ один, слой пуст —
    другой.

    Держится это на том, что заглушка умеет отвечать по содержимому запроса
    (`reply` принимает `(messages, index)`), а не по его номеру. Заглушка
    моделью не притворяется, и обещать, что живая модель ответит этими же
    словами, проверка не может и не обещает. Она обещает ровно то, что можно
    обещать: разница во врезке доезжает до ответа, а не теряется по дороге.

    Порядок здесь и есть проверка, и первый шаг в нём — пустая память.
    Утверждение о разнице обязано стоять на **непустом** значении с обеих
    сторон: чат с пустым слоем отвечает то же, что чат, заведённый до
    записи, — иначе «пустая память неотличима от отсутствующей» держалось
    бы только на составе промпта, а на ответах разъехалось бы.
    """
    _stub.install(reply=_memory_aware)
    question = "покажи пример"

    def answer(agent_id, client) -> tuple[str, list[dict]]:
        """Вопрос и ответ на него: текст последнего кадра `done` и тот промпт,
        по которому он получен."""
        done = _frame(_frames(client, agent_id, question), "done")
        return done["text"], _stub.CALLS[-1]["messages"]

    with TestClient(main.app) as client:
        # --- 1. Память пуста: врезки нет вовсе --------------------------------
        assert client.get("/api/memory").json()["total"] == 0, "память не пуста"
        blank = new_agent(client, label="пустая память", system="СИС")
        empty_answer, blank_prompt = answer(blank, client)

        # --- 2. Запись вписали руками — и только руками -----------------------
        #
        # Другого пути в этот слой нет: служебного вызова, который писал бы
        # туда сам, не существует. Ровно это и делает пользователь на экране.
        written = client.post(
            "/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"}
        )
        assert written.status_code == 200, written.text

        knows = new_agent(client, label="с памятью", system="СИС")
        loud, loud_prompt = answer(knows, client)

        # --- 3. Главное: ответ разошёлся ---------------------------------------
        assert loud != empty_answer, f"ответ не изменился: {loud!r}"
        assert "Kotlin" in loud, loud
        assert "Kotlin" not in empty_answer, empty_answer

        # --- 4. И разошёлся **от памяти**, а не от чего-нибудь ещё -------------
        #
        # Разные ответы сами по себе ничего не доказывают: разойтись они могли
        # бы и от разного вопроса, и от разного системного промпта, и от номера
        # вызова. Поэтому сверяем сами запросы: всё, кроме врезки памяти, в них
        # совпадает слово в слово.
        assert [m for m in loud_prompt if "[долговременная память]" not in m["content"]] \
            == blank_prompt, (loud_prompt, blank_prompt)
        assert blank_prompt[-1]["content"] == question, blank_prompt[-1]
        assert loud_prompt[-1]["content"] == question, loud_prompt[-1]
        assert not any(
            "[долговременная память]" in m["content"] for m in blank_prompt
        ), blank_prompt

        # --- 5. И то же самое в рабочем слое -----------------------------------
        #
        # Слои разные — область и срок жизни, — а показывают они себя одинаково:
        # вписанная запись доезжает до ответа. Запись здесь **своя**, под чатом,
        # и соседнего чата она не касается.
        task = new_agent(client, label="с задачей", system="СИС")
        client.post(
            f"/api/agents/{task}/working", json={"kind": "goal", "content": "пример на Kotlin"}
        )
        working_answer, working_prompt = answer(task, client)
        assert working_answer != empty_answer, working_answer
        assert "Kotlin" in working_answer, working_answer
        assert any("[факты о разговоре]" in m["content"] for m in working_prompt), working_prompt

        # --- 6. Ответ разный, а счёт обменов одинаковый ------------------------
        #
        # Врезка памяти — часть промпта, а не лишний вызов: разницу в ответе
        # она даёт, не добавляя обращений к модели. Вызовов ровно столько,
        # сколько обменов, и служебных среди них нет ни одного.
        assert len(_stub.CALLS) == 3, len(_stub.CALLS)
        assert not _service_calls(), "за памятью сходили к модели"

    return (
        f"с записью — {loud!r}; без неё — {empty_answer!r}; "
        f"рабочая память меняет ответ так же — {working_answer!r}; "
        "запросы различаются одной врезкой"
    )


@check("профиль меняет ответ: один вопрос, два профиля — два разных ответа")
def check_profile_changes_answer():
    """Единственный пункт задания Дня 12, который просили именно **проверить**:
    «ответы для разных профилей».

    Профиль — про то, **как** с человеком разговаривать: стиль, формат,
    контекст его работы. От записи памяти он отличается не местом хранения,
    а наклонением: память это факт («пишет на Kotlin»), профиль —
    распоряжение («отвечай кратко»). Отсюда и роль сообщения: **системным
    едет то, что задал человек**, а выведенное из разговора — обычным,
    с подписью.

    Порядок здесь и есть проверка. Сперва пустой профиль: ни блока, ни
    системного сообщения — пустое обязано быть неотличимо от отсутствующего,
    и у профиля это строже, чем у памяти, потому что системное сообщение
    сдвигает номера **всех** врезок разом. Потом два разных профиля на один
    и тот же вопрос. Потом ловушка: чат **без** системного промпта, у которого
    профиль непуст, — номера врезок обязаны учесть появившееся системное
    сообщение, и ошибка здесь не падает, а расходится молча.

    Держится всё на том, что заглушка отвечает по содержимому запроса
    (`reply` принимает `(messages, index)`), а не по его номеру, — тем же
    способом, каким проверяется «память меняет ответ».
    """
    _stub.install(reply=_profile_aware)
    question = "с чего начать?"

    def answer(agent_id, client) -> tuple[str, list[dict]]:
        """Ответ и тот промпт, по которому он получен."""
        done = _frame(_frames(client, agent_id, question), "done")
        return done["text"], _stub.CALLS[-1]["messages"]

    with TestClient(main.app) as client:
        # --- 1. Профиль пуст: ни блока, ни системного сообщения ---------------
        assert client.get("/api/profile").json() == {"profile": {}}, "профиль не пуст"
        blank = new_agent(client, label="без профиля")
        empty_answer, blank_prompt = answer(blank, client)
        assert blank_prompt == [{"role": "user", "content": question}], blank_prompt

        # --- 2. Профиль вписал человек — и только человек ---------------------
        #
        # Другого пути сюда нет: агент профиль не выводит из разговора.
        # Ровно это и делает пользователь на экране, во вкладке «Профиль».
        terse = client.patch("/api/profile", json={"style": "кратко, на ты"})
        assert terse.status_code == 200, terse.text
        assert terse.json() == {"profile": {"style": "кратко, на ты"}}, terse.text
        short = new_agent(client, label="краткий")
        short_answer, short_prompt = answer(short, client)

        # --- 3. Профиль другой — и вопрос тот же ------------------------------
        client.patch("/api/profile", json={"style": "подробно, с примерами"})
        wordy = new_agent(client, label="подробный")
        long_answer, long_prompt = answer(wordy, client)

        # --- 4. Главное: ответы разошлись --------------------------------------
        assert short_answer != long_answer, (short_answer, long_answer)
        assert short_answer != empty_answer, short_answer
        assert len(long_answer) > len(short_answer), (short_answer, long_answer)

        # --- 5. И разошлись **от профиля**, а не от чего-нибудь ещё ------------
        #
        # Разные ответы сами по себе не доказывают ничего: разойтись они могли
        # бы и от разного вопроса, и от номера вызова. Поэтому сверяем сами
        # запросы: всё, кроме системного сообщения, в них совпадает слово
        # в слово, а вопрос — тот же самый.
        assert short_prompt[1:] == long_prompt[1:] == blank_prompt, (short_prompt, long_prompt)
        assert short_prompt[0]["content"] != long_prompt[0]["content"], short_prompt

        # --- 6. Профиль едет **системным** сообщением, а не обычным -----------
        #
        # Это и есть главное решение дня: роль сообщения определяется
        # источником. Профиль задал человек — значит, система.
        assert short_prompt[0]["role"] == "system", short_prompt[0]
        assert short_prompt[0]["content"] == (
            "[как отвечать]\nстиль: кратко, на ты"
        ), short_prompt[0]
        assert not any(
            m["role"] != "system" and "[как отвечать]" in m["content"] for m in short_prompt
        ), short_prompt

        # --- 7. В блок попадают только заполненные поля -----------------------
        #
        # Пустое поле уехало бы строкой «формат: » и сказало бы модели ровно
        # ничего, заняв место распоряжения. И системный промпт чата стоит
        # в том же сообщении, а не вторым системным: «кто ты» и «как отвечать»
        # читаются как одно распоряжение.
        client.patch("/api/profile", json={"context": "собираю ТЗ на мобильное приложение"})
        both = new_agent(client, label="с промптом", system="СИС")
        _, both_prompt = answer(both, client)
        assert both_prompt[0] == {
            "role": "system",
            "content": (
                "СИС\n\n[как отвечать]\nстиль: подробно, с примерами\n"
                "контекст: собираю ТЗ на мобильное приложение"
            ),
        }, both_prompt[0]
        assert "формат" not in both_prompt[0]["content"], both_prompt[0]
        assert len([m for m in both_prompt if m["role"] == "system"]) == 1, both_prompt

        # --- 8. Ловушка: профиль сдвигает номера врезок -----------------------
        #
        # Номера берутся из длины уже собранного начала промпта, а системное
        # сообщение теперь заводится и у чата **без** системного промпта —
        # если профиль непуст. Забудь об этом — и все врезки уедут на единицу,
        # причём молча: промпт останется собранным верно, а подписи ролей
        # в просмотре запроса встанут над чужими сообщениями.
        naked = new_agent(client, label="без промпта, с профилем")
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{naked}/working", json={"kind": "goal", "content": "собрать ТЗ"})
        start = _frame(_frames(client, naked, question), "start")
        assert start["memory_at"] == 1 and start["working_at"] == 2, start
        assert start["resolved_messages"][0]["role"] == "system", start["resolved_messages"][0]

        # И обратно: сняли профиль — системного сообщения снова нет, номера
        # вернулись на место. Пустая строка поле снимает, и пустой профиль
        # неотличим от отсутствовавшего.
        client.patch("/api/profile", json={"style": "", "context": ""})
        assert client.get("/api/profile").json() == {"profile": {}}, "профиль не снялся"
        bare = _frame(_frames(client, naked, question), "start")
        assert bare["memory_at"] == 0 and bare["working_at"] == 1, bare
        assert bare["resolved_messages"][0]["role"] == "user", bare["resolved_messages"][0]

        # --- 9. Профиль пишет только человек ----------------------------------
        #
        # Шесть обменов позади, и ни один не дописал в профиль ни строки:
        # пути туда у агента нет вовсе. И лишнего вызова к модели профиль
        # не стоит — он часть промпта, а не служебный вызов.
        client.patch("/api/profile", json={"style": "кратко, на ты"})
        _talk(client, naked, 2, "ещё вопрос {i}")
        assert client.get("/api/profile").json() == {
            "profile": {"style": "кратко, на ты"}
        }, "профиль изменился сам"
        assert len(_stub.CALLS) == 8, len(_stub.CALLS)
        assert not _service_calls(), "за профилем сходили к модели"

        # --- 10. Границы ручки — те же, что у памяти --------------------------
        for body in ({"kind": "profile"}, {"style": None}, {}):
            assert client.patch("/api/profile", json=body).status_code == 400, body

    return (
        f"кратко — {short_answer!r}; подробно — {long_answer!r}; без профиля — "
        f"{empty_answer!r}; запросы различаются одним системным сообщением; "
        "профиль без системного промпта сдвинул врезки на 1, снятый — вернул"
    )


# --- День 14: инварианты — чего ассистент не вправе предлагать ----------------


@check("инварианты: слой глобальный, едет системным, запрещённых слов в промпте нет")
def check_invariants_layer():
    from app.agent import Agent, PROMPT_SLOTS

    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        assert client.get("/api/invariants").json() == {"total": 0, "records": []}
        bare = new_agent(client, system="")
        start = _frame(_frames(client, bare, "первый"), "start")
        assert _stub.CALLS[-1]["messages"] == [
            {"role": "user", "content": "первый"}
        ], _stub.CALLS[-1]["messages"]
        assert start["memory_at"] is None, start["memory_at"]

        stack = client.post("/api/invariants", json={
            "kind": "stack", "content": "  бэкенд только на Python  ",
            "banned": ["Java", " Groovy "],
        }).json()
        plain = client.post("/api/invariants", json={
            "kind": "architecture", "content": "монолит, микросервисов не предлагать",
        }).json()
        assert stack["content"] == "бэкенд только на Python", stack
        assert stack["banned"] == ["Java", "Groovy"], stack
        assert plain["banned"] == [], plain
        assert plain["seq"] > stack["seq"], (stack, plain)
        assert client.get("/api/invariants").json()["total"] == 2

        _stub.reset()
        client.patch("/api/profile", json={"style": "кратко, на ты"})
        talky = new_agent(client, system="СИС")
        client.post(f"/api/agents/{talky}/task", json={"description": "собрать ТЗ"})
        client.post(f"/api/agents/{talky}/messages", json={"text": "вопрос"})
        sent = _stub.CALLS[-1]["messages"]
        assert len([m for m in sent if m["role"] == "system"]) == 1, sent
        head = sent[0]
        assert head["role"] == "system", head
        blocks = head["content"].split("\n\n")
        assert [part.partition("\n")[0] for part in blocks] == [
            "СИС", "[как отвечать]", "[этап задачи: планирование]",
            "[жизненный цикл задачи]", "[чего нельзя]",
        ], blocks
        assert blocks[1] == "[как отвечать]\nстиль: кратко, на ты", blocks[1]
        assert blocks[4] == (
            "[чего нельзя]\n"
            "1. ограничение стека: бэкенд только на Python\n"
            "2. архитектура: монолит, микросервисов не предлагать\n"
            "Соблюдай это. Если просьба противоречит любому пункту — откажись "
            "и назови, какой именно нарушается."
        ), blocks[4]
        assert not any(
            m["role"] != "system" and "[чего нельзя]" in m["content"] for m in sent
        ), sent

        for word in ("Java", "Groovy"):
            assert not any(word in m["content"] for m in sent), (word, sent)

        assert PROMPT_SLOTS == ("memory_at", "working_at", "task_at", "summary_at"), PROMPT_SLOTS

        client.patch("/api/profile", json={"style": ""})
        assert client.get("/api/profile").json() == {"profile": {}}, "профиль не снялся"
        naked = new_agent(client, system="")
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на бэкенде"})
        shifted = _frame(_frames(client, naked, "вопрос"), "start")
        assert shifted["memory_at"] == 1, shifted["memory_at"]
        assert shifted["resolved_messages"][0]["role"] == "system", shifted["resolved_messages"][0]

        for record in client.get("/api/invariants").json()["records"]:
            assert client.delete(f"/api/invariants/{record['seq']}").status_code == 200
        back = _frame(_frames(client, naked, "вопрос"), "start")
        assert back["memory_at"] == 0, back["memory_at"]
        assert back["resolved_messages"][0]["role"] == "user", back["resolved_messages"][0]

        client.post("/api/invariants", json={"kind": "stack", "content": "бэкенд только на Python", "banned": ["Java"]})

        store = REGISTRY.store
        real_list, reads = store.list_invariants, []

        def counted():
            reads.append(1)
            return real_list()

        _stub.reset()
        with patch.object(store, "list_invariants", counted):
            client.post(f"/api/agents/{naked}/messages", json={"text": "сколько раз читали"})
        assert len(reads) == 1, f"чтений слоя за обмен: {len(reads)}, а должно быть одно"
        assert any(
            "[чего нельзя]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]

        _stub.install(reply=_service_aware)
        folding = new_agent(
            client, system="СИС", strategy="summary", keep_last=2, compress_every=2
        )
        _talk(client, folding, 3)
        call = _service_calls("summary")[-1]["messages"]
        assert not any("[чего нельзя]" in m["content"] for m in call), call
        assert not any("бэкенд только на Python" in m["content"] for m in call), call
        assert not any("Java" in m["content"] for m in call), call

        before = client.get("/api/invariants").json()["total"]
        assert before, "слой пуст — копировать нечего, проверять тоже"
        branch = client.post(
            f"/api/agents/{folding}/fork", json={"at": 2}
        ).json()["agents"][0]["id"]
        _stub.install(reply=lambda m, i: f"ответ {i}")
        _stub.reset()
        client.post(f"/api/agents/{branch}/messages", json={"text": "вопрос ветки"})
        assert any(
            "[чего нельзя]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]
        assert client.get("/api/invariants").json()["total"] == before, (
            "ветвление завело копии инвариантов"
        )

    path = _temp_db("invariants")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="с инвариантом", model="stub/model"), store=store)
    store.add_invariant("stack", "бэкенд только на Python", ["Java"])
    asyncio.run(drain(agent.ask("вопрос")))
    assert "[чего нельзя]" in agent.build_prompt("ещё")[0]["content"], "врезки нет"
    agent.forget()
    assert len(store.list_invariants()) == 1, store.list_invariants()
    assert "[чего нельзя]" in agent.build_prompt("после forget")[0]["content"]

    with _restarted(store, agent.id) as (fresh, revived):
        assert len(fresh.list_invariants()) == 1, fresh.list_invariants()
        assert "[чего нельзя]" in revived.build_prompt("после перезапуска")[0]["content"]

        homeless = Agent(AgentSpec(label="без базы", model="stub/model"))
        assert homeless.invariant_items() == [], homeless.invariant_items()
        assert homeless.build_prompt("вопрос") == [{"role": "user", "content": "вопрос"}]

        fresh.clear()
        assert fresh.list_invariants() == [], fresh.list_invariants()
        assert revived.build_prompt("после очистки") == [
            {"role": "user", "content": "после очистки"}
        ], revived.build_prompt("после очистки")

    return "инварианты: единственный system, banned вне промпта, один снимок, глобальность и reopen"


@check("инварианты меняют ответ: тот же вопрос с записью и без неё расходится")
def check_invariants_change_answer():
    """Единственный пункт задания Дня 14, который просили именно **проверить**:
    «отказывается предлагать решения, которые их нарушают».

    Все прочие проверки слоя смотрят на **запрос** — что уехало в модель,
    каким сообщением, с какой ролью. Здесь смотрим на **ответ**: тот же
    вопрос, те же настройки, пустой слой против непустого — и два разных
    ответа, причём во втором отказ назван своим инвариантом.

    Заглушка не модель и моделью не притворяется: обещать, что живая модель
    ответит этими словами, проверка не вправе. Она обещает ровно то, что
    можно обещать и здесь, и на живом прогоне: разница доезжает до ответа,
    а не теряется по дороге. Довод и устройство те же, что
    у `_memory_aware` и `_profile_aware`.
    """
    _stub.install(reply=_invariant_aware)
    question = "на чём писать бэкенд?"

    def answer(agent_id, client) -> tuple[str, list[dict]]:
        done = _frame(_frames(client, agent_id, question), "done")
        return done["text"], _stub.CALLS[-1]["messages"]

    with TestClient(main.app) as client:
        # --- 1. Слой пуст: ни блока, ни системного сообщения ------------------
        assert client.get("/api/invariants").json()["total"] == 0, "слой не пуст"
        blank = new_agent(client, label="без инвариантов")
        free_answer, blank_prompt = answer(blank, client)
        assert blank_prompt == [{"role": "user", "content": question}], blank_prompt

        # --- 2. Инвариант вписал человек — и только человек -------------------
        #
        # Другого пути сюда нет: агент инварианты не выводит из разговора,
        # это распоряжение. Ровно это и делает пользователь на экране.
        written = client.post("/api/invariants", json={
            "kind": "stack", "content": "бэкенд только на Python",
            "banned": ["Java", "Groovy"],
        })
        assert written.status_code == 200, written.text
        bound = new_agent(client, label="с инвариантом")
        strict_answer, strict_prompt = answer(bound, client)

        # --- 3. Главное: ответ разошёлся, и отказ назвал инвариант ------------
        assert strict_answer != free_answer, f"ответ не изменился: {strict_answer!r}"
        assert "ограничение стека" in strict_answer, strict_answer
        assert "Java" in free_answer, free_answer
        assert "Java" not in strict_answer, strict_answer

        # --- 4. И разошёлся **от инварианта**, а не от чего-нибудь ещё --------
        #
        # Разные ответы сами по себе не доказывают ничего: разойтись они могли
        # бы и от разного вопроса, и от номера вызова. Поэтому сверяем сами
        # запросы: всё, кроме системного сообщения, совпадает слово в слово.
        assert strict_prompt[1:] == blank_prompt, (strict_prompt, blank_prompt)
        assert strict_prompt[0]["role"] == "system", strict_prompt[0]
        assert strict_prompt[0]["content"].startswith("[чего нельзя]"), strict_prompt[0]

        # --- 5. Запрещённых слов нет ни в одном сообщении ни одного промпта ---
        #
        # Они лежат в той же записи и в том же ответе ручки — и всё равно
        # не уезжают: в блок едут только вид и текст.
        assert client.get("/api/invariants").json()["records"][0]["banned"] == [
            "Java", "Groovy"
        ], client.get("/api/invariants").json()
        for word in ("Java", "Groovy"):
            assert not any(word in m["content"] for m in strict_prompt), (word, strict_prompt)

        # --- 6. Ответ разный, а счёт обменов одинаковый -----------------------
        #
        # Блок — часть промпта, а не лишний вызов: разницу в ответе он даёт,
        # не добавляя обращений к модели.
        assert len(_stub.CALLS) == 2, len(_stub.CALLS)
        assert not _service_calls(), "за инвариантами сходили к модели"

    return (
        f"с инвариантом — {strict_answer!r}; без него — {free_answer!r}; "
        "запросы различаются одним системным сообщением, запрещённых слов "
        "в промпте нет ни одного"
    )

# --- День 14, второй PR: сторож смотрит на готовый ответ ----------------------


def _marks(metrics) -> list[dict]:
    """Отметки сторожа в метриках обмена — их может не быть вовсе."""
    return (metrics or {}).get("banned_hits") or []


def _guard_answers(table: dict):
    """Заглушка, отвечающая по вопросу, а не по номеру вызова: сторож смотрит
    на **текст ответа**, и каждый случай задаётся своей парой «вопрос → ответ»."""
    return lambda messages, index: table[messages[-1]["content"]]


@check("сторож ловит запрещённое слово целиком — и только его")
def check_guard_finds_banned_words():
    """Слова лежали в базе с прошлого PR и ждали, кто на них посмотрит.
    Смотрит сторож — в **готовый** ответ, собранный целиком: слово,
    разорванное между кусками потока, по кускам не нашлось бы.

    Граница слова здесь не `\\b`: та держится на `\\w` с обеих сторон и рвётся
    на `C++`. Проверяются оба края сразу — `Java` внутри `JavaScript`
    не отмечается, а `C++` посреди фразы отмечается.

    Словоформы не ловятся, и это записано утверждением, а не умолчанием:
    пробел, названный проверкой, завтра не сойдёт за свойство.
    """
    answers = {
        "стек?": "Берите Java и Kotlin.",
        "фронт?": "JavaScript и TypeScript — обычный выбор.",
        "точно?": "java тоже сойдёт",
        "язык?": "Лучше Котлин.",
        "плюсы?": "Возьмите C++ и не думайте.",
        "формы?": "Пишите Котлином, не пожалеете.",
        "чисто?": "Возьмите Python, он тут к месту.",
    }
    _stub.install(reply=_guard_answers(answers))

    with TestClient(main.app) as client:
        written = client.post("/api/invariants", json={
            "kind": "stack", "content": "бэкенд только на Python",
            "banned": ["Java", "Котлин", "C++"],
        })
        assert written.status_code == 200, written.text
        agent_id = new_agent(client, label="сторож")
        marks = {
            question: _marks(_frame(_frames(client, agent_id, question), "done")["metrics"])
            for question in answers
        }

    # --- 1. Слово из списка в ответе — отметка есть, и она называет оба ----
    #
    # И слово, и правило: отметка без правила оставила бы читателя гадать,
    # чем именно «Java» плоха в этом чате.
    assert marks["стек?"] == [
        {"word": "Java", "rule": "ограничение стека: бэкенд только на Python"}
    ], marks["стек?"]

    # --- 2. Слова нет — отметки нет вовсе ----------------------------------
    assert marks["чисто?"] == [], marks["чисто?"]

    # --- 3. Слово целиком: `Java` внутри `JavaScript` не отмечается ---------
    assert marks["фронт?"] == [], marks["фронт?"]

    # --- 4. Регистр не важен ------------------------------------------------
    assert [m["word"] for m in marks["точно?"]] == ["Java"], marks["точно?"]

    # --- 5. Кириллическое слово ловится тем же правилом ---------------------
    assert [m["word"] for m in marks["язык?"]] == ["Котлин"], marks["язык?"]

    # --- 6. Слово со знаком ловится: граница не `\b` ------------------------
    assert [m["word"] for m in marks["плюсы?"]] == ["C++"], marks["плюсы?"]

    # --- 7. Словоформы не ловятся — это пробел, и он назван -----------------
    assert marks["формы?"] == [], marks["формы?"]

    # --- 8. Сторож ничего не запускает: обменов столько же, сколько вопросов -
    assert len(_stub.CALLS) == len(answers), len(_stub.CALLS)
    assert not _service_calls(), "за сторожем сходили к модели"
    return (
        f"задели запрет {sum(1 for m in marks.values() if m)} ответа из {len(answers)}; "
        "`Java` в `JavaScript` и «Котлином» — нет"
    )


@check("отметка сторожа живёт с репликой: поток, база, суммы, сжатие")
def check_guard_mark_lives_with_turn():
    """Отметка кладётся в метрики обмена — тем же образцом, что `dropped`.
    Отсюда три свойства разом, и каждое проверяется своим утверждением.

    Колонки под отметку нет: метрики пишутся с репликой одной транзакцией,
    значит отметка видна и в старых обменах после перезапуска.
    """
    from app.agent import Agent

    word = "Kafka"
    text = f"Поставьте {word} и живите спокойно."
    path = _temp_db("guard-restart")
    store = Store(path).init()
    store.add_invariant("technical", "очередь — Redis", [word])

    def answer_and_rewrite(messages, index):
        """Соседняя вкладка правит инвариант, пока модель отвечает. Ответ
        обязан судиться тем правилом, что уехало в промпт: перечитанное
        осудило бы его правилом, которого модель не видела."""
        store.update_invariant(
            1, kind="technical", content="очередь — теперь Kafka", banned=["живите"]
        )
        return text

    # Каждый кусок потока — один символ: слово, собранное только целиком,
    # ни в одной дельте не лежит. Искал бы сторож в кусках — не нашёл бы.
    _stub.install(reply=answer_and_rewrite, chunks=len(text))

    async def one_turn():
        agent = Agent(AgentSpec(label="сторож", model="stub/model"), store=store)
        return agent.id, await drain(agent.ask("чем очередь?"))

    agent_id, frames = asyncio.run(one_turn())
    deltas = [f["text"] for f in frames if f["type"] == "delta"]
    done = next(f for f in frames if f["type"] == "done")

    # --- 1. Ответ разрезан на символы, а слово всё равно найдено -----------
    assert max(len(d) for d in deltas) == 1, max(len(d) for d in deltas)
    assert not any(word in d for d in deltas), deltas[:5]
    assert _marks(done["metrics"]) == [
        {"word": word, "rule": "техническое решение: очередь — Redis"}
    ], done["metrics"]

    # --- 2. Правило в отметке — то, что уехало в промпт ---------------------
    #
    # Слой переписан посреди обмена, и новое слово в ответе есть («живите»):
    # перечитай сторож слой — отметка назвала бы его и чужое правило.
    assert store.list_invariants()[0]["content"] == "очередь — теперь Kafka", (
        store.list_invariants()
    )

    # --- 3. Отметка пережила перезапуск: она лежит в базе, а не в памяти ---
    with _restarted(store, agent_id) as (again, revived):
        answer = revived.history[-1]
        assert _marks(answer.metrics) == [
            {"word": word, "rule": "техническое решение: очередь — Redis"}
        ], answer.metrics
        # --- 4. Суммы по чату от отметки не изменились --------------------
        #
        # Сверяются они с числами **заглушки**, а не с копией тех же метрик:
        # копия сдвинулась бы вместе с ними, и утверждение было бы зелёным
        # при любом слагаемом. Слагаемых в итоге ровно столько, сколько
        # полей в USAGE_FIELDS — и ключа отметки среди них нет.
        total = revived.usage_summary()
        assert agent_module.BANNED_METRIC not in total, sorted(total)
        assert total["total_tokens"] == 100, total
        assert total["cost_usd"] == 0.000123, total
        assert total["completion_tokens"] == len(text) // 4, total
        assert again.list_invariants()[0]["content"] == "очередь — теперь Kafka", (
            again.list_invariants()
        )

    # --- 5. Сжатие сторож не трогает ---------------------------------------
    #
    # Служебный вызов пересказывает разговор, а не отвечает собеседнику:
    # запрещённое слово в пересказе — это слово из истории, а не предложение
    # его употребить. Инварианты сжатию не достаются вовсе, и судить его
    # было бы нечем.
    _stub.install(reply=lambda messages, index: text)
    with TestClient(main.app) as client:
        client.post("/api/invariants", json={
            "kind": "technical", "content": "очередь — Redis", "banned": [word],
        })
        folded = new_agent(
            client, label="со сжатием", strategy="summary",
            keep_last=KEEP, compress_every=EVERY,
        )
        _talk(client, folded, 11)
        agent = REGISTRY.require(folded)
        assert agent.summaries, "сворачивания не было — проверять нечего"
        for item in agent.summaries:
            assert _marks(item["metrics"]) == [], item["metrics"]
        # А у ответов того же чата отметка есть: слово одно и то же,
        # и отсутствие у сводки — решение, а не совпадение.
        answers = [t for t in agent.history if t.role == "assistant"]
        assert all(_marks(t.metrics) for t in answers), [t.metrics for t in answers]

    return (
        f"слово собрано из {len(deltas)} дельт по одному символу; отметка пережила "
        f"перезапуск с прежним правилом; сумм не изменила; "
        f"у сводок ({len(agent.summaries)}) отметок нет"
    )


# --- День 13: состояние задачи — строгая машина, ручное управление ------------


def _task_url(agent_id: str) -> str:
    return f"/api/agents/{agent_id}/task"


def _task_of(client, agent_id: str) -> dict | None:
    """Состояние задачи так, как его видит клиент: полем `task` самого чата.
    Отдельной ручки чтения нет — второе место «состояние наружу» обязано было
    бы совпадать с первым, и совпадало бы только пока за ним следят."""
    return client.get(f"/api/agents/{agent_id}").json()["task"]


def _at_stage(client, stage: str, label: str = "задача", *, worked: bool = True):
    """Чат, доведённый до нужного этапа **командами** — других путей нет.

    Перед каждым «дальше» — обмен: ворота не выпускают с этапа, на котором
    не было работы. `worked=False` оставляет последний этап пустым — ровно
    таким, каким его видят ворота.

    Утверждений здесь нет: на какой этап он встал, проверяет сама проверка.
    Пауза берётся с планирования, значит снятие вернёт туда же.
    """
    agent_id = new_agent(client, label=f"{label} {stage}")
    client.post(_task_url(agent_id), json={"description": f"{label} — собрать ТЗ"})
    moves = {
        "planning": (),
        "execution": ("next",),
        "validation": ("next", "next"),
        "done": ("next", "next", "next"),
        "paused": ("pause",),
    }[stage]
    for move in moves:
        if move == "next":
            _talk(client, agent_id, 1, "работа этапа")
        client.patch(_task_url(agent_id), json={"move": move})
    if worked:
        _talk(client, agent_id, 1, "работа этапа")
    return agent_id


@check("этап меняется только переходом из таблицы, и двигает его человек")
def check_task_machine():
    """Автомат Дня 13: планирование → выполнение → проверка → готово, пауза
    с любого незавершённого этапа.

    Ходов три и все три — нажатия человека: у модели нет ни одного рычага.
    Таблица переходов одна, и проходится она целиком: пятнадцать клеток
    «этап × ход», где семь ведут дальше, а восемь обязаны отказать, оставив
    состояние прежним.

    Ожидаемое действие — третья ось задания, хранится наравне с этапом
    и шагом: не задавали — едет умолчание этапа, задали — своё, сменили
    этап — снова умолчание.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Вся таблица переходов, клетка за клеткой --------------------
        walked, refused = [], []
        for stage in STAGES:
            for move in MOVES:
                agent_id = _at_stage(client, stage, "обход")
                before = _task_of(client, agent_id)
                assert before["stage"] == stage, (stage, before)
                answer = client.patch(_task_url(agent_id), json={"move": move})
                target = TRANSITIONS.get((stage, move))
                if target is None:
                    # Незаконный ход: отказ, и состояние цело. Сделать
                    # соседний ход за человека было бы хуже отказа.
                    assert answer.status_code == 409, (stage, move, answer.text)
                    assert _task_of(client, agent_id) == before, (
                        f"{stage} + {move}: отказали, а состояние сдвинулось"
                    )
                    refused.append((stage, move))
                    continue
                # Снятие паузы возвращает туда, откуда встали: помощник
                # ставил её с планирования.
                expected = "planning" if target == RESUME else target
                assert answer.status_code == 200, (stage, move, answer.text)
                assert answer.json()["task"]["stage"] == expected, (stage, move, answer.json())
                walked.append((stage, move))
        assert len(walked) == 7 and len(refused) == 8, (walked, refused)
        # «Готово» терминально: с него не уводит ни один ход.
        assert [m for (s, m) in refused if s == "done"] == list(MOVES), refused

        # --- 2. Пауза с каждого незавершённого этапа, и возврат туда же -----
        for stage in ("planning", "execution", "validation"):
            agent_id = _at_stage(client, stage, "пауза")
            paused = client.patch(_task_url(agent_id), json={"move": "pause"})
            assert paused.status_code == 200, (stage, paused.text)
            assert paused.json()["task"]["stage"] == "paused", paused.json()
            back = client.patch(_task_url(agent_id), json={"move": "resume"})
            assert back.status_code == 200, (stage, back.text)
            assert back.json()["task"]["stage"] == stage, (stage, back.json())

        # --- 3. Ожидаемое действие: умолчание, своё, снова умолчание --------
        #
        # Смена этапа заданное сбрасывает: оставленное, оно называло бы
        # действие прошлого этапа.
        agent_id = _at_stage(client, "execution", "ожидание")
        default = _task_of(client, agent_id)["expects"]
        assert default == STAGE_EXPECTS["execution"], default
        mine = client.patch(_task_url(agent_id), json={"expects": "показать черновик"})
        assert mine.status_code == 200, mine.text
        assert mine.json()["task"]["expects"] == "показать черновик", mine.json()
        # Шаг правится тем же телом и в одиночку: оси разные.
        stepped = client.patch(_task_url(agent_id), json={"step": "пишу проверку"})
        assert stepped.json()["task"] == {
            **mine.json()["task"], "step": "пишу проверку",
        }, stepped.json()
        moved = client.patch(_task_url(agent_id), json={"move": "next"})
        assert moved.json()["task"]["expects"] == STAGE_EXPECTS["validation"], moved.json()
        assert moved.json()["task"]["step"] == "пишу проверку", "смена этапа съела шаг"

        # --- 4. Границы ручек ------------------------------------------------
        url = _task_url(agent_id)
        for body in ({}, {"move": "назад"}, {"move": None}, {"stage": "done"},
                     {"step": "   "}, {"expects": ""}, {"move": "next", "step": 5}):
            assert client.patch(url, json=body).status_code == 400, body
        assert client.post(url, json={"description": "  "}).status_code == 400
        assert client.post(url, json={"description": "х", "stage": "done"}).status_code == 400
        # Кривой текст рядом с законным ходом состояние не двигает: тело
        # разбирается целиком до первой правки.
        assert _task_of(client, agent_id)["stage"] == "validation", _task_of(client, agent_id)

        # --- 5. Ход называет этап, из которого его выбирали ------------------
        #
        # Таблица стережёт, какие рёбра есть, но не то, из какой вершины
        # человек на самом деле выходил, — а он видел на экране именно её.
        # Две вкладки на одном чате иначе делают два хода подряд: этап
        # перепрыгивается, и обе получают 200.
        picked = _at_stage(client, "planning", "сверка")
        ahead = client.patch(_task_url(picked), json={"move": "next", "from": "planning"})
        assert ahead.status_code == 200, ahead.text
        assert ahead.json()["task"]["stage"] == "execution", ahead.json()
        # Вторая вкладка всё ещё думает, что чат на планировании.
        stale = client.patch(_task_url(picked), json={"move": "next", "from": "planning"})
        assert stale.status_code == 409, stale.text
        assert "планирование" in stale.json()["detail"], stale.text
        assert "выполнение" in stale.json()["detail"], stale.text
        assert _task_of(client, picked)["stage"] == "execution", "отказали, а этап сдвинулся"
        # Не названный `from` по-прежнему проходит: он необязателен, — но
        # ворота на пустом этапе стоят и без него.
        _talk(client, picked, 1, "работа этапа")
        assert client.patch(_task_url(picked), json={"move": "next"}).status_code == 200
        assert _task_of(client, picked)["stage"] == "validation", _task_of(client, picked)
        # Незнакомый этап отсекается на границе, а не совпадением: подпись
        # вместо ключа — 400, и ход не состоялся.
        assert client.patch(
            _task_url(picked), json={"move": "next", "from": "проверка"}
        ).status_code == 400
        # Один `from` не просит ничего: он сверяет, а не двигает.
        assert client.patch(_task_url(picked), json={"from": "validation"}).status_code == 400
        assert _task_of(client, picked)["stage"] == "validation", _task_of(client, picked)

        # --- 6. Режим выключается, и тогда двигать нечего ---------------------
        assert client.delete(url).json() == {"task": None}, "режим не выключился"
        assert _task_of(client, agent_id) is None, _task_of(client, agent_id)
        assert client.patch(url, json={"move": "next"}).status_code == 409, "двинули выключенный"
        assert client.patch(url, json={"step": "х"}).status_code == 409, "вписали в выключенный"

        # --- 7. Состояние наружу едет одним местом — полем чата ---------------
        #
        # Отдельной ручки чтения нет: второе место «состояние наружу» обязано
        # было бы совпадать с первым. И поля конфига о режиме нет — «идёт ли
        # задача» говорит сама строка состояния, а второе поле давало бы вход
        # и выход мимо команд.
        chat = client.get(f"/api/agents/{picked}").json()
        assert "task" in chat and "workflow" not in chat, sorted(chat)
        assert client.get(_task_url(picked)).status_code == 405, "ручка чтения вернулась"
        # Включить режим ручке конфига нечем...
        bare = new_agent(client, label="мимо команды")
        assert client.patch(f"/api/agents/{bare}", json={"workflow": "plan"}).status_code == 400
        assert _task_of(client, bare) is None, "режим включили мимо команды"
        # ...и спрятать живую задачу тоже: тогда она воскресла бы перезапуском.
        assert client.patch(f"/api/agents/{picked}", json={"workflow": "off"}).status_code == 400
        assert _task_of(client, picked)["stage"] == "validation", "конфиг спрятал задачу"
        # Одно место — одно значение, и у **выгруженного** чата тоже: так
        # выглядит и вытеснение по потолку, и перезапуск сервера. Список
        # слева приезжает другой ветвью кода — у живого чата задачу отдаёт
        # он сам, у выгруженного строка из базы, — и назвать они обязаны одно.
        mine = _task_of(client, picked)
        assert REGISTRY._unload(picked) is True, "чат не был живым"
        assert REGISTRY._unload(bare) is True, "чат не был живым"
        cold = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
        assert cold[picked]["task"] == mine, (mine, cold[picked]["task"])
        assert cold[bare]["task"] is None, cold[bare]["task"]

        # --- 8. К модели за состоянием не ходят ни разу -----------------------
        #
        # Ни вызова, определяющего этап, ни разбора ответа: состояние двигает
        # человек, и чат от режима задачи не дорожает ни на токен.
        before_calls = len(_stub.CALLS)
        _talk(client, agent_id, 2)
        assert len(_stub.CALLS) == before_calls + 2, len(_stub.CALLS)
        assert not _service_calls(), "за состоянием задачи сходили к модели"

    return (
        f"{len(walked)} законных перехода прошли, {len(refused)} незаконных "
        "отказаны и состояние цело; «готово» терминально; пауза берётся с трёх "
        "этапов и возвращает туда же; умолчание этапа перебивается заданным, "
        "а смена этапа возвращает к умолчанию; выгруженный чат называет "
        "в списке ту же задачу, что отдаёт сам"
    )


@check("отказ базы не двигает этап и не прячет задачу")
def check_task_write_is_atomic():
    """Состояние задачи меняется в памяти **после** базы, а не до.

    Отказ базы — путь штатный (503 на занятом файле), и человек по нему
    получает отказ. Сдвинутый под отказом этап расходится с тем, что человек
    видит: шапка прежняя, а следующий обмен уезжает с правилом нового этапа,
    и перезапуск потом молча откатывает назад.

    Выход из режима той же монетой: снятие обязано быть целым. Флаг,
    прятавший недоудалённую строку, был не отказоустойчивостью, а
    расхождением с отложенным сроком.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("task-atomic")
    store = Store(path).init()

    def boom(*args, **kwargs):
        raise StoreBusyError("база занята другим процессом, повторите")

    try:
        chat = agent_module.Agent(AgentSpec(label="с задачей", model="stub/model"), store=store)
        chat.start_task("собрать ТЗ")
        chat.write_task({"step": "пишу проверку"})
        before = chat.task_items()
        assert before["stage"] == "planning" and before["step"] == "пишу проверку", before

        # --- 1. Ход: отказ оставляет этап прежним и в памяти, и в базе -------
        refused = False
        with patch.object(store, "save_task", boom):
            try:
                chat.move_task("next")
            except StoreBusyError:
                refused = True
        assert refused, "база отказала, а ход прошёл"
        assert chat.task_items() == before, (before, chat.task_items())
        assert store.load_task(chat.id)["stage"] == "planning", store.load_task(chat.id)
        # И промпт следующего обмена — с правилом прежнего этапа: сдвинутый
        # в памяти этап виден только здесь, шапка о нём не знает.
        head = chat.build_prompt("ещё")[0]["content"]
        assert STAGE_RULES["planning"] in head, head
        assert STAGE_RULES["execution"] not in head, head

        # --- 2. Выход из режима: снято целиком или не тронуто ----------------
        refused = False
        with patch.object(store, "clear_task", boom):
            try:
                chat.stop_task()
            except StoreBusyError:
                refused = True
        assert refused, "база отказала, а режим выключился"
        assert chat.task_items() == before, "задачу спрятали, а строка в базе цела"
        assert store.load_task(chat.id) is not None, "строку сняли, а человеку отдали отказ"
        assert STAGE_RULES["planning"] in chat.build_prompt("ещё")[0]["content"]

        # --- 3. Без отказа снимается целиком: и в памяти, и в базе -----------
        chat.stop_task()
        assert chat.task_items() == {}, chat.task_items()
        assert store.load_task(chat.id) is None, store.load_task(chat.id)
    finally:
        store.close()
    return (
        "отказ базы на ходе оставил этап прежним в памяти, в базе и в промпте; "
        "отказ на выходе не спрятал живую задачу; без отказа она снялась целиком"
    )


@check("этап едет системным сообщением, состояние задачи — врезкой")
def check_task_in_prompt():
    """Что из состояния задачи видит модель.

    Правило этапа — **системным** сообщением: это распоряжение, которому
    модель следует. Само состояние — врезкой ролью `user` с подписью
    `[задача]`: это сведения, на которые она опирается. Разделение то же,
    что у профиля и памяти.

    Пять правил и пять «ожидается» собираются из пяти настоящих запросов,
    а не читаются из таблиц в коде: одно правило на все этапы так и покраснеет.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Пять этапов — пять разных правил и пять разных «ожидается» ---
        rules, expects = {}, {}
        for stage in STAGES:
            agent_id = _at_stage(client, stage, "промпт")
            start = _frame(_frames(client, agent_id, "вопрос"), "start")
            prompt = start["resolved_messages"]
            head = prompt[0]
            assert head["role"] == "system", (stage, head)
            # Сравнивается само правило, а не сообщение целиком: подпись
            # этапа в заголовке различает блоки и без разных правил — и одно
            # правило на все пять прошло бы незамеченным.
            block = next(
                part for part in head["content"].split("\n\n")
                if part.startswith("[этап задачи: ")
            )
            title, _, rule = block.partition("\n")
            assert title == f"[этап задачи: {STAGE_LABELS[stage]}]", (stage, title)
            rules[stage] = rule
            # Номер врезки сверяется до обращения по нему: пустой или
            # посчитанный «суммой предыдущих» слот обязан краснеть
            # утверждением, а не падением по индексу.
            at = start["task_at"]
            assert isinstance(at, int) and at < len(prompt), (stage, at, len(prompt))
            insert = prompt[at]
            assert insert["role"] == "user", (stage, insert)
            assert insert["content"].startswith("[задача]"), (stage, insert)
            expects[stage] = next(
                line for line in insert["content"].splitlines()
                if line.startswith("ожидается: ")
            )
        assert len(set(rules.values())) == len(STAGES), rules
        assert all(text.strip() for text in rules.values()), rules
        assert len(set(expects.values())) == len(STAGES), expects
        assert "не продолжай" in rules["paused"], rules["paused"]

        # --- 2. Врезка едет при включённом режиме, даже с пустым шагом -------
        agent_id = _at_stage(client, "execution", "врезка")
        start = _frame(_frames(client, agent_id, "вопрос"), "start")
        empty = start["resolved_messages"][start["task_at"]]["content"]
        assert "шаг:" not in empty, empty
        assert "описание: врезка — собрать ТЗ" in empty, empty
        client.patch(_task_url(agent_id), json={"step": "пишу проверку"})
        start = _frame(_frames(client, agent_id, "вопрос"), "start")
        filled = start["resolved_messages"][start["task_at"]]["content"]
        assert "шаг: пишу проверку" in filled, filled

        # --- 3. Выключенный режим неотличим от невключавшегося ---------------
        #
        # Ни врезки, ни правила, ни системного сообщения: иначе всякая
        # последовательность ролей сдвинулась бы на сообщение.
        bare = new_agent(client, label="без задачи")
        start = _frame(_frames(client, bare, "вопрос"), "start")
        assert start["resolved_messages"] == [{"role": "user", "content": "вопрос"}], start
        assert start["task_at"] is None, start
        client.delete(_task_url(agent_id))
        after = _frame(_frames(client, agent_id, "вопрос"), "start")
        assert after["task_at"] is None, after
        assert not any("[задача]" in m["content"] for m in after["resolved_messages"]), after
        assert after["resolved_messages"][0]["role"] == "user", after["resolved_messages"][0]

        # --- 4. Все четыре врезки разом: номера сходятся с промптом ----------
        #
        # Номера берутся из длины собранного начала промпта, а не считаются
        # суммой: сумма, переписанная вторым местом, расходится молча.
        full = new_agent(client, label="все врезки", system="СИС",
                         strategy="summary", keep_last=2, compress_every=2)
        client.post(_task_url(full), json={"description": "собрать ТЗ"})
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{full}/working", json={"kind": "goal", "content": "ТЗ"})
        _talk(client, full, 3)
        start = _frame(_frames(client, full, "вопрос"), "start")
        prompt = start["resolved_messages"]
        assert [start[name] for name in agent_module.PROMPT_SLOTS] == [1, 2, 3, 4], start
        assert prompt[0]["content"].startswith("СИС\n\n[этап задачи"), prompt[0]
        assert prompt[1]["content"].startswith("[долговременная память]"), prompt[1]
        assert prompt[2]["content"].startswith("[факты о разговоре]"), prompt[2]
        assert prompt[3]["content"].startswith("[задача]"), prompt[3]
        assert prompt[4]["content"].startswith("[пересказ начала разговора"), prompt[4]
        assert len([m for m in prompt if m["role"] == "system"]) == 1, prompt

        # Сжатию состояние задачи не досталось: пересказ пересказывает
        # разговор, а не то, чем человек занят.
        compress = _service_calls("summary")[-1]["messages"]
        assert not any("[задача]" in m["content"] for m in compress), compress

        # --- 5. Ветка уносит состояние и живёт своей копией -------------------
        parent = _task_of(client, full)
        branch = client.post(f"/api/agents/{full}/fork", json={"at": 2}).json()["agents"][0]
        assert branch["task"] == parent, (parent, branch["task"])
        client.patch(_task_url(branch["id"]), json={"move": "next"})
        assert _task_of(client, branch["id"])["stage"] == "execution"
        assert _task_of(client, full) == parent, "ход в ветке двинул родителя"

    # --- 6. Перезапуск: продолжение без повторных объяснений ----------------
    _stub.reset()
    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("task-restart")
    store = Store(path).init()
    chat = agent_module.Agent(AgentSpec(label="с задачей", model="stub/model"), store=store)
    chat.start_task("собрать ТЗ")
    chat.move_task("next")
    chat.write_task({"step": "пишу проверку"})
    _ask(chat, 4)
    before = chat.task_items()
    chat_id = chat.id
    assert before["stage"] == "execution" and before["step"] == "пишу проверку", before

    with _restarted(store, chat_id) as (again, revived):
        assert revived.task_items() == before, (before, revived.task_items())
        prompt = revived.build_prompt("ещё")
        assert prompt[0]["role"] == "system" and "выполнение" in prompt[0]["content"], prompt[0]
        assert prompt[1]["content"].startswith("[задача]"), prompt[1]
        # Состояние лежит в своей таблице, а не строкой в ленте: иначе его
        # стирал бы каждый обмен — `save_history` начинается с `DELETE`.
        assert not any("[задача]" in r[2] for r in again.message_rows(chat_id)), "врезка в ленте"
    return (
        "пять этапов — пять разных правил в системном сообщении и пять разных "
        "«ожидается» во врезке; врезка едет и с пустым шагом; выключенный режим "
        f"уходит без единого сообщения; {len(agent_module.PROMPT_SLOTS)} врезки "
        "встали номерами 1–4; ветка унесла состояние копией, перезапуск его сохранил"
    )


def _lifecycle(client, agent_id: str, text: str = "вопрос") -> tuple[dict, str]:
    """Кадр `start` обмена и блок жизненного цикла из его системного
    сообщения — пустая строка, если блока в промпте нет вовсе."""
    start = _frame(_frames(client, agent_id, text), "start")
    head = start["resolved_messages"][0] if start["resolved_messages"] else {}
    parts = head.get("content", "").split("\n\n") if head.get("role") == "system" else []
    return start, next((p for p in parts if p.startswith("[жизненный цикл задачи]")), "")


@check("модель знает автомат: блок собран из таблицы, чужой работы не делает")
def check_task_lifecycle():
    """Что модель знает о машине состояний, и что из этого доезжает до ответа.

    Блок `[жизненный цикл задачи]` собран **из `TRANSITIONS`**, а не выписан
    руками: таблица остаётся единственным источником правды, и добавленное
    ребро доезжает до модели само. Ходы в блоке — те же, что законны у ручки:
    считает их одна и та же таблица.

    Едет блок **системным** сообщением, рядом с правилом этапа: это
    распоряжение, а не сведения. Своей врезки он не заводит — номера врезок
    памяти от него не сдвигаются, и выключенный режим по-прежнему
    неотличим от невключавшегося.

    Последнее утверждение — про **ответ**: заглушка читает блок там же, где
    прочитала бы его модель, и на просьбу о работе чужого этапа отвечает
    отказом, называя этап и ход. Словами живой модели заглушка не
    притворяется: обещать она вправе только то, что знание машины доезжает
    до ответа, а не теряется по дороге.
    """
    _stub.install(reply=_stage_aware)
    with TestClient(main.app) as client:
        # --- 1. Ходы в блоке — те же, что законны у ручки -------------------
        lines, blocks = {}, {}
        for stage in STAGES:
            agent_id = _at_stage(client, stage, "цикл")
            _, blocks[stage] = _lifecycle(client, agent_id)
            # Отметка «сейчас здесь» берётся с запасным пустым значением:
            # её отсутствие обязано краснеть утверждением, а не падением.
            lines[stage] = next(
                (line for line in blocks[stage].splitlines() if " — сейчас здесь: " in line),
                "",
            )
            assert lines[stage].startswith(f"{STAGE_LABELS[stage]} — сейчас здесь: "), (
                stage, lines[stage]
            )
            # Законные ходы сверяются с теми, что называет отказ ручки:
            # обоих считает одна таблица, и разойтись им негде.
            allowed = [move for move in main._allowed_from(stage).split(", ") if move]
            named = re.findall(r"\((/[a-z-]+)\)", lines[stage])
            assert named == [MOVE_COMMANDS[move] for move in allowed], (stage, lines[stage])
            for move in MOVES:
                if move not in allowed:
                    assert MOVE_COMMANDS[move] not in lines[stage], (stage, move, lines[stage])
            # Все пять этапов названы: чтобы назвать чужой этап, модель
            # обязана его знать, а не догадаться по соседям.
            assert all(STAGE_LABELS[s] in blocks[stage] for s in STAGES), (stage, blocks[stage])
        assert len(set(blocks.values())) == len(STAGES), blocks
        assert "ходов отсюда нет" in lines["done"], lines["done"]
        assert not any("ходов отсюда нет" in lines[s] for s in STAGES if s != "done"), lines

        # --- 2. Новое ребро в таблице доезжает до модели само ----------------
        #
        # Выпиши блок руками — и здесь он назвал бы прежнее: модель звала бы
        # ход, которого нет, или молчала бы о том, который есть.
        closed = _at_stage(client, "done", "ребро")
        with patch.dict(TRANSITIONS, {("done", "next"): "planning"}):
            _, block = _lifecycle(client, closed)
            assert "готово — сейчас здесь: дальше (/task-next) → планирование" in block, block
        _, block = _lifecycle(client, closed)
        assert "ходов отсюда нет" in block, block

        # --- 3. Выключенный режим уходит без блока и без системного ---------
        #
        # Проверяется до врезок памяти: непустой слой встаёт в промпт и
        # голому чату — и «уходит ни с чем» держалось бы уже не блоком.
        bare = new_agent(client, label="без задачи")
        start, block = _lifecycle(client, bare)
        assert block == "", block
        assert start["resolved_messages"] == [{"role": "user", "content": "вопрос"}], start

        # --- 4. Системным, одним сообщением, и врезок не прибавилось --------
        full = new_agent(client, label="все врезки", system="СИС",
                         strategy="summary", keep_last=2, compress_every=2)
        client.post(_task_url(full), json={"description": "собрать ТЗ"})
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{full}/working", json={"kind": "goal", "content": "ТЗ"})
        _talk(client, full, 3)
        start, block = _lifecycle(client, full)
        prompt = start["resolved_messages"]
        assert block and prompt[0]["role"] == "system", prompt[0]
        assert prompt[0]["content"].startswith("СИС\n\n[этап задачи"), prompt[0]
        assert len([m for m in prompt if m["role"] == "system"]) == 1, prompt
        assert not any(
            m["role"] != "system" and "[жизненный цикл" in m["content"] for m in prompt
        ), prompt
        assert agent_module.PROMPT_SLOTS == (
            "memory_at", "working_at", "task_at", "summary_at"
        ), agent_module.PROMPT_SLOTS
        assert [start[name] for name in agent_module.PROMPT_SLOTS] == [1, 2, 3, 4], start

        # Сжатию блок не достаётся: служебный вызов пересказывает разговор,
        # а не отвечает собеседнику.
        compress = _service_calls("summary")[-1]["messages"]
        assert not any("[жизненный цикл" in m["content"] for m in compress), compress

        # --- 5. Главное: ассистент не делает работу чужого этапа -------------
        #
        # Утверждение об **ответе**, а не о составе запроса: просьбу о коде
        # на планировании заглушка отклоняет, называя этап и ход, — и оба
        # слова взяты ею из самого блока.
        before = len(_stub.CALLS)
        # Пустой этап: с планирования здесь никуда не ходят, а лишний обмен
        # развёл бы два промпта номером ответа заглушки.
        asking = _at_stage(client, "planning", "отказ", worked=False)
        refused = _frame(_frames(client, asking, "напиши код"), "done")["text"]
        knowing = _stub.CALLS[-1]["messages"]
        # Тот же ярлык — тот же промпт: два чата различает только блок.
        with patch.object(agent_module, "lifecycle_block", lambda stage: ""):
            blind = _at_stage(client, "planning", "отказ", worked=False)
            obeyed = _frame(_frames(client, blind, "напиши код"), "done")["text"]
        ignorant = _stub.CALLS[-1]["messages"]
        assert "«выполнение»" in refused and "/task-next" in refused, refused
        assert "def main" not in refused, refused
        assert "def main" in obeyed and "выполнение" not in obeyed, obeyed
        assert refused != obeyed, refused

        # И разошлись они **от блока**: всё остальное в двух запросах
        # совпадает слово в слово, а обращений к модели поровну.
        stripped = [dict(m) for m in knowing]
        stripped[0]["content"] = "\n\n".join(
            part for part in stripped[0]["content"].split("\n\n")
            if not part.startswith("[жизненный цикл задачи]")
        )
        assert stripped == ignorant, (stripped, ignorant)
        assert len(_stub.CALLS) == before + 2, (before, len(_stub.CALLS))

    return (
        f"пять этапов — пять разных блоков, ходы в каждом те же, что у ручки; "
        f"новое ребро в таблице блок назвал; врезок по-прежнему "
        f"{len(agent_module.PROMPT_SLOTS)}, системное сообщение одно; "
        f"на планировании ответ — {refused!r}, без блока — {obeyed!r}"
    )


@check("ворота: «дальше» не выпускает с этапа, на котором не было обменов")
def check_task_gate():
    """Задание дня просит не пускать в реализацию до утверждённого плана.
    Таблица переходов о работе не знает ничего — знают ворота: ход `next`
    отказывает, пока на этапе не записалось ни одного ответа.

    Ловят они **пустоту, а не спешку**: обмен, открывающий ворота, шлёт сам
    переход, и три `/task-next` подряд пройдут. Человек нажал трижды — значит
    решил трижды.

    Пауза и снятие паузы не ограничены никогда, и утверждение об этом стоит
    рядом с утверждением про `next`: остановиться человек вправе всегда.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        store = main.REGISTRY.store

        # --- 1. Пустой этап не выпускает, обмен выпускает -------------------
        shut = []
        for stage in ("planning", "execution", "validation"):
            agent_id = _at_stage(client, stage, "ворота", worked=False)
            before = store.load_task(agent_id)
            answer = client.patch(_task_url(agent_id), json={"move": "next"})
            assert answer.status_code == 409, (stage, answer.text)
            detail = answer.json()["detail"]
            assert "не было ни одного обмена" in detail, detail
            # Текст говорит и что делать: ожидаемое действие этапа.
            assert STAGE_EXPECTS[stage] in detail, detail
            # Состояние прежнее **целиком**, вместе с отметкой входа: наружу
            # она не едет, и сверить её можно только со строкой в базе.
            assert store.load_task(agent_id) == before, (stage, store.load_task(agent_id))
            _talk(client, agent_id, 1, "поработали")
            went = client.patch(_task_url(agent_id), json={"move": "next"})
            assert went.status_code == 200, (stage, went.text)
            assert went.json()["task"]["stage"] == TRANSITIONS[(stage, "next")], went.json()
            shut.append(stage)

        # --- 2. Пауза и снятие паузы воротами не ограничены -----------------
        #
        # Утверждение об отсутствии — там, где присутствие достижимо: этапы
        # те же самые, и на них `next` только что отказал.
        for stage in shut:
            agent_id = _at_stage(client, stage, "пауза без работы", worked=False)
            off = client.patch(_task_url(agent_id), json={"move": "pause"})
            assert off.status_code == 200, (stage, off.text)
            assert off.json()["task"]["stage"] == "paused", off.json()
            back = client.patch(_task_url(agent_id), json={"move": "resume"})
            assert back.status_code == 200, (stage, back.text)
            assert back.json()["task"]["stage"] == stage, (stage, back.json())
            # И ворота на месте: постояв на паузе, работой этого не сделаешь.
            again = client.patch(_task_url(agent_id), json={"move": "next"})
            assert again.status_code == 409, (stage, again.text)

        # --- 3. Отметку входа двигает только переход -------------------------
        #
        # Двигай её всякая запись — и набранный шаг закрывал бы ворота,
        # которые открыл обмен.
        agent_id = _at_stage(client, "execution", "отметка", worked=False)
        mark = store.load_task(agent_id)["stage_at"]
        _talk(client, agent_id, 1, "поработали")
        for body in ({"step": "пишу проверку"}, {"expects": "показать черновик"}):
            assert client.patch(_task_url(agent_id), json=body).status_code == 200, body
            assert store.load_task(agent_id)["stage_at"] == mark, (body, store.load_task(agent_id))
        went = client.patch(_task_url(agent_id), json={"move": "next"})
        assert went.status_code == 200, went.text
        assert store.load_task(agent_id)["stage_at"] > mark, store.load_task(agent_id)

        # --- 4. Обмен, который не записался, ворота не открывает -------------
        #
        # Вопрос без ответа работой не был: в истории его нет вовсе.
        agent_id = _at_stage(client, "planning", "упавший", worked=False)
        _stub.install(fail=True)
        _talk(client, agent_id, 1, "вопрос без ответа")
        assert client.get(f"/api/agents/{agent_id}").json()["history_len"] == 0, "обмен записался"
        fell = client.patch(_task_url(agent_id), json={"move": "next"})
        assert fell.status_code == 409, fell.text
        _stub.install(reply=lambda m, i: f"ответ {i}")
        _talk(client, agent_id, 1, "теперь с ответом")
        assert client.patch(_task_url(agent_id), json={"move": "next"}).status_code == 200

        # --- 5. Ветка уносит отметку вместе с состоянием ---------------------
        #
        # Ветвимся **до** работы нынешнего этапа: этап у ветки тот же, а в её
        # ленте работы на нём нет — значит и ворота у неё закрыты. Не унеси
        # ветка отметку, реплики прошлого этапа сошли бы у неё за работу.
        parent = new_agent(client, label="ветка от этапа")
        client.post(_task_url(parent), json={"description": "собрать ТЗ"})
        _talk(client, parent, 1, "планирую")
        assert client.patch(_task_url(parent), json={"move": "next"}).status_code == 200
        _talk(client, parent, 1, "выполняю")
        forked = client.post(f"/api/agents/{parent}/fork", json={"at": 2}).json()["agents"][0]
        assert forked["task"]["stage"] == "execution", forked["task"]
        assert store.load_task(forked["id"]) == store.load_task(parent), forked["task"]
        blocked = client.patch(_task_url(forked["id"]), json={"move": "next"})
        assert blocked.status_code == 409, blocked.text
        # А родитель выпускает: работа на этом этапе у него есть.
        assert client.patch(_task_url(parent), json={"move": "next"}).status_code == 200

    # --- 6. Отметка переживает перезапуск процесса ---------------------------
    _stub.reset()
    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("task-gate")
    store = Store(path).init()
    chat = agent_module.Agent(AgentSpec(label="ворота", model="stub/model"), store=store)
    chat.start_task("собрать ТЗ")
    _ask(chat, 1)
    assert chat.gate_shut("next") is False, "обмен ворота не открыл"
    with _restarted(store, chat.id) as (fresh, again):
        assert again.gate_shut("next") is False, "отметка входа не пережила перезапуск"
        assert again.move_task("next")["stage"] == "execution", again.task
        assert again.gate_shut("next") is True, "новый этап выпустил без работы"
        with _restarted(fresh, chat.id) as (last, third):
            assert third.gate_shut("next") is True, "после перезапуска пустой этап стал рабочим"
            _ask(third, 1)
            assert third.gate_shut("next") is False, "обмен ворота не открыл"
            last.close()

    return (
        f"на трёх этапах «дальше» отказало на пустом и прошло после обмена; "
        f"пауза и возврат прошли на всех трёх; шаг и ожидание отметку "
        f"не двинули, а переход двинул; упавший обмен ворота не открыл; "
        f"ветка от точки до работы унесла отметку и закрытые ворота; "
        f"отметка пережила два перезапуска"
    )


@check("токены служебного вызова попадают в итог по чату")
def check_service_tokens_counted():
    """Вызов на сжатие тоже уехал в модель и тоже оплачен. Экономия,
    не вычитающая его стоимость, — враньё, поэтому он входит в `usage_total`
    отдельным слагаемым: в истории его нет, и сам собой он туда не попадёт.

    Числа служебного вызова здесь заведомо больше всех остальных вместе
    взятых: потеряйся они, сумма разошлась бы на порядок.

    Служебных вызовов было два, пока рабочую память вёл агент. Второе
    слагаемое ушло вместе с ним — и ушло целиком: цену, которой больше
    не платят, считать нечего.
    """
    folding_calls: set = set()

    def reply(messages, index):
        if _service_kind(messages) == "summary":
            folding_calls.add(index)
            return f"СВОДКА {index}"
        return f"ответ {index}"

    def usage(index):
        if index in folding_calls:
            return _usage(50000, 400, 50400, 0.05)
        return _usage(1, 1, 2, 0.000001)

    _stub.install(reply=reply, usage=usage)
    with TestClient(main.app) as client:
        # Память выключена: этот раздел про цену **сжатия**, и вызов
        # на ведение сдвинул бы нумерацию, по которой заглушка раздаёт числа.
        agent_id = new_agent(
            client, strategy="summary", keep_last=KEEP, compress_every=EVERY, memory="off"
        )
        _talk(client, agent_id, 9)
        body = client.get(f"/api/agents/{agent_id}").json()

    assert len(folding_calls) == 1, folding_calls
    total = body["usage_total"]
    assert total["prompt_tokens"] == 9 * 1 + 50000, total
    assert total["completion_tokens"] == 9 * 1 + 400, total
    assert total["total_tokens"] == 9 * 2 + 50400, total
    assert round(total["cost_usd"], 8) == round(9 * 0.000001 + 0.05, 8), total
    # Сжатие не растит счётчик сообщений: сводка живёт вне истории, и ни
    # в счётчике, ни карточкой в ленте её нет.
    assert body["history_len"] == 18, body["history_len"]
    assert len([t for t in body["transcript"] if t["role"] == "assistant"]) == 9, "сводка попала в ленту"

    # И тот же счёт из файла базы: метрики сжатия лежат в `summaries`.
    agent = REGISTRY.require(agent_id)
    assert agent.summaries[0]["metrics"]["total_tokens"] == 50400, agent.summaries[0]["metrics"]

    return (
        f"итог {total['total_tokens']} токенов = {9 * 2} за девять обменов "
        f"плюс 50400 за сжатие; сообщений по-прежнему {body['history_len']}"
    )


# --- Панель и тело запроса ----------------------------------------------------


# Каждое поле панели вместе с тем, во что оно должно превратиться в теле
# запроса. `system` проверяется отдельно: он едет не в теле, а сообщением.
PANEL_FIELDS = {
    "model": "новая/модель",
    "temperature": 0.9,
    "max_tokens": 555,
    "top_p": 0.11,
    "top_k": 7,
    "min_p": 0.02,
    "repetition_penalty": 1.3,
    "presence_penalty": 0.4,
    "frequency_penalty": 0.6,
    "stop": ["СТОП"],
    "response_format": {"type": "json_object"},
}


@check("сообщение уходит с тем конфигом, что показан в панели")
def check_panel_reaches_request():
    """Смотрим не ответ ручки, а то, что реально ушло в модель: и промпт,
    и каждое поле панели. Три состояния поля: не задано — в теле его нет
    вовсе; задано — уехало ровно им; снято — исчезло снова. Клиентская
    половина — в `checks/browser_check.js`, исполнением `app.js`."""
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, model="старая/модель", system="СТАРЫЙ ПРОМПТ")
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"})
        first = _stub.CALLS[-1]
        assert first["messages"][0]["content"] == "СТАРЫЙ ПРОМПТ", first["messages"][0]
        for name in PANEL_FIELDS:
            if name != "model":
                assert name not in first["payload"], f"{name} уехал в тело, хотя задан не был"

        patched = client.patch(
            f"/api/agents/{agent_id}",
            json={"system": "НОВЫЙ ПРОМПТ", **PANEL_FIELDS},
        )
        assert patched.status_code == 200, patched.text

        _stub.reset()
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "второй"})
        call = _stub.CALLS[-1]

        # Снятое поле исчезает из тела целиком, а не уезжает пустым.
        client.patch(
            f"/api/agents/{agent_id}",
            json={"system": None, "top_k": None, "stop": None},
        )
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "третий"})
        bare = _stub.CALLS[-1]

        # Кривой тип — 400 с текстом, а не 500 и не молчаливая отправка.
        assert client.patch(f"/api/agents/{agent_id}", json={"top_k": 0.5}).status_code == 400
        assert client.patch(f"/api/agents/{agent_id}", json={"stop": "СТОП"}).status_code == 400
        # true — не число: в Python True это int, и без отдельной проверки
        # «temperature: true» уехало бы к провайдеру единицей.
        assert client.patch(f"/api/agents/{agent_id}", json={"temperature": True}).status_code == 400

    sent, payload = call["messages"], call["payload"]
    assert sent[0] == {"role": "system", "content": "НОВЫЙ ПРОМПТ"}, sent[0]
    assert call["model"] == "новая/модель", call["model"]
    for name, value in PANEL_FIELDS.items():
        if name == "model":
            continue
        assert payload.get(name) == value, (name, payload.get(name), value)
    # Правка панели меняет конфиг, а не переписку: прошлый обмен на месте.
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"], sent
    assert sent[1]["content"] == "первый", sent[1]

    assert not any(m["role"] == "system" for m in bare["messages"]), bare["messages"]
    assert "top_k" not in bare["payload"] and "stop" not in bare["payload"], bare["payload"]
    assert bare["payload"]["top_p"] == 0.11, bare["payload"]
    return "промпт, модель и все параметры уехали новыми; снятые исчезли, история цела"


@check("тело каждого вызова: require_parameters, сжатие выключено, usage запрошен")
def check_call_body_invariants():
    """Три правила, без которых день не состоится:

    - `provider.require_parameters` — без него OpenRouter вправе увести запрос
      к провайдеру, который молча проигнорирует temperature или stop;
    - выключенное `context-compression` — иначе на окнах 8k и меньше провайдер
      сам обрезает промпт посередине: ошибки нет, а середина разговора ушла;
    - `usage: {include: true}` — без него точных чисел не пришлёт никто,
      и День 8 остался бы без единого токена.

    По телу запроса на всех путях: сообщение, перегенерация, чат
    с параметрами и чат со своим `extra_body`."""
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        # Память выключена у всех трёх: проверка перебирает пути **обмена**,
        # и служебные вызовы в этот перебор не входят — за тело вызова
        # на ведение памяти отвечает своя проверка.
        bare = new_agent(client)
        client.post(f"/api/agents/{bare}/messages", json={"text": "раз"})
        client.post(f"/api/agents/{bare}/regenerate")

        loaded = new_agent(client, temperature=0.7, stop=["СТОП"])
        client.post(f"/api/agents/{loaded}/messages", json={"text": "два"})

        # Свой поставщик и свой плагин дописываются рядом, а не вместо наших:
        # иначе выключенное сжатие исчезало бы молча, от соседства ключей.
        mine = new_agent(
            client,
            extra_body={"provider": {"order": ["openai"]}, "plugins": [{"id": "web"}]},
        )
        client.post(f"/api/agents/{mine}/messages", json={"text": "три"})

    assert len(_stub.CALLS) == 4, len(_stub.CALLS)
    for call in _stub.CALLS:
        broken = _body_rules(call["payload"])
        assert not broken, f"вызов через ручку: {broken}"
    last = _stub.CALLS[-1]["payload"]
    assert last["provider"]["order"] == ["openai"], last["provider"]
    assert last["plugins"][-1] == {"id": "web"}, last["plugins"]

    # И то же самое напрямую, без веб-слоя: правила живут в build_payload,
    # а не в ручке, поэтому CLI и любой другой вызывающий получают их тоже.
    from app.llm import build_payload

    payload = build_payload(AgentSpec(label="без веба", model="stub/m"))
    assert not _body_rules(payload), f"вызов напрямую: {_body_rules(payload)}"
    # И плагин тут **единственный**: у чата без своих плагинов наш встаёт
    # один, а не дописывается к чужому списку из ниоткуда.
    assert payload["plugins"] == [{"id": "context-compression", "enabled": False}], payload
    return "4 вызова через ручки и один напрямую — все три правила на каждом"


# --- День 8: подсчёт токенов --------------------------------------------------


class _FakeResponse:
    """Ответ OpenRouter, разобранный до строк SSE. Пауза между рассуждением
    и ответом настоящая: на ней и видно, что первый токен наступил раньше."""

    def __init__(self, lines, gap_after=None, status_code=200):
        self.status_code = status_code
        self._lines = lines
        self._gap_after = gap_after

    async def aiter_lines(self):
        for i, line in enumerate(self._lines):
            await asyncio.sleep(0)
            yield line
            if self._gap_after is not None and i == self._gap_after:
                await asyncio.sleep(0.02)

    async def aread(self):
        return b""


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def stream(self, *args, **kwargs):
        return _FakeStream(self._response)


def _sse_chunks(*payloads) -> list[str]:
    return [f"data: {json.dumps(p, ensure_ascii=False)}" for p in payloads] + ["data: [DONE]"]


def _parsed_metrics(lines, **kwargs) -> dict:
    """Гоняет настоящий `stream_completion` на подменённом транспорте: метрики
    заглушки говорили бы только о самой заглушке."""
    import app.llm as llm

    gap = kwargs.pop("gap", None)
    with patch.object(llm, "shared_client", lambda: _FakeClient(_FakeResponse(lines, gap_after=gap))), \
            patch.object(llm, "api_key", lambda: "sk-or-проверочный"):
        spec = AgentSpec(label="разбор", model="stub/thinking", reasoning_enabled=True)
        events = asyncio.run(drain(llm.stream_completion(spec, prompt_override=[], **kwargs)))
    return next(e for e in events if e["type"] == "done")["metrics"]


def _streamed(lines, *, spec=None, **kwargs) -> tuple[list[dict], dict]:
    """Гоняет настоящий `stream_completion` на подменённом транспорте и отдаёт
    **все** события вместе с телом запроса, которое ушло бы в OpenRouter.

    У `_parsed_metrics` ответом одни метрики, и для чисел этого хватает. Про
    вызовы инструментов спрашивается другое: сколько событий и в каком порядке
    они пришли, — значит нужен весь список. Утверждений здесь нет ни одного:
    помощник ставит сцену, а меряет её проверка.
    """
    import app.llm as llm

    sent: list[dict] = []

    class _Recorder(_FakeClient):
        """Тот же транспорт, но запоминает тело запроса: «объявлены ли `tools`»
        читается из настоящего `json=`, а не из пересказа."""

        def stream(self, *args, **call_kwargs):
            sent.append(call_kwargs.get("json"))
            return super().stream(*args, **call_kwargs)

    with patch.object(llm, "shared_client", lambda: _Recorder(_FakeResponse(lines))), \
            patch.object(llm, "api_key", lambda: "sk-or-проверочный"):
        target = spec if spec is not None else AgentSpec(label="вызовы", model="stub/tools")
        events = asyncio.run(drain(llm.stream_completion(target, prompt_override=[], **kwargs)))
    return events, sent[-1]


@check("входные, выходные и всего — числа провайдера, разобранные сервером")
def check_usage_parsed_by_server():
    """Главные числа дня приезжают последним чанком OpenRouter, и разбирает
    их `app/llm.py`. Заодно честное время до первого токена: `ttft_ms` стоит
    на первом токене **ответа**, `first_token_ms` — на первом вообще, и на
    думающей модели это разные моменты."""
    metrics = _parsed_metrics(
        _sse_chunks(
            {"provider": "стенд", "choices": [{"delta": {"reasoning": "думаю"}}]},
            {"choices": [{"delta": {"content": "ответ"}, "finish_reason": "stop"}]},
            {
                "usage": {
                    "prompt_tokens": 140,
                    "completion_tokens": 60,
                    "total_tokens": 200,
                    "completion_tokens_details": {"reasoning_tokens": 40},
                    "cost": 0.000123456789,
                }
            },
        ),
        gap=0,
        context_length=1000,
    )
    assert metrics["prompt_tokens"] == 140, metrics
    assert metrics["completion_tokens"] == 60, metrics
    assert metrics["total_tokens"] == 200, metrics
    # Рассуждение провайдер кладёт ВНУТРЬ выхода и называет отдельно.
    assert metrics["reasoning_tokens"] == 40, metrics
    assert metrics["cost_usd"] == 0.00012346, metrics
    assert metrics["provider"] == "стенд" and metrics["finish_reason"] == "stop", metrics
    # Рассуждение пришло раньше ответа — значит и первый токен раньше TTFT.
    # Равенство здесь так же плохо, как отсутствие: оно значит, что
    # размышление записали в молчание.
    assert metrics["first_token_ms"] is not None, metrics
    assert metrics["first_token_ms"] < metrics["ttft_ms"], metrics

    # Сумму провайдер вправе не называть. Тогда её досчитывает сервер — один
    # раз, до записи в историю: добор в браузере дал бы на одном экране два
    # разных «всего» — сумму под ответом и другую сумму в плитке.
    quiet = _parsed_metrics(
        _sse_chunks(
            {"choices": [{"delta": {"content": "ответ"}}]},
            {"usage": {"prompt_tokens": 80, "completion_tokens": 40}},
        )
    )
    assert quiet["total_tokens"] == 120, quiet

    # А из одной половины сумма не выдумывается: «всего» остаётся неизвестным.
    import app.llm as llm

    half = Metrics()
    llm._apply_usage(half, {"prompt_tokens": 80})
    assert half.total_tokens is None, half
    return (
        f"вход 140, выход 60 (из них 40 рассуждение), всего 200; "
        f"первый токен {metrics['first_token_ms']:.1f} мс раньше ttft {metrics['ttft_ms']:.1f} мс"
    )


def _usage(prompt, completion, total, cost):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost_usd": cost,
    }


@check("сводка по чату — сумма слагаемых, а не выдуманное число")
def check_usage_summary_sums():
    """У каждого ответа свой usage: с одинаковыми числами сумма сошлась бы
    и при сложении, и при `100 * len(history)`, и при возврате последней
    метрики трижды."""
    plan = [
        _usage(11, 5, 16, 0.000011),
        _usage(120, 40, 160, 0.000120),
        _usage(1300, 7, 1307, 0.001300),
    ]
    _stub.install(usage=lambda i: plan[i])
    with TestClient(main.app) as client:
        # Память выключена: у каждого обмена здесь **свой** usage по номеру
        # вызова, и вклинившийся служебный вызов сдвинул бы нумерацию.
        agent_id = new_agent(client)
        _talk(client, agent_id, 3)
        body = client.get(f"/api/agents/{agent_id}").json()

    total = body["usage_total"]
    assert total is not None, "сводки нет вовсе"
    assert total["prompt_tokens"] == 11 + 120 + 1300, total
    assert total["completion_tokens"] == 5 + 40 + 7, total
    assert total["total_tokens"] == 16 + 160 + 1307, total
    assert round(total["cost_usd"], 8) == round(0.000011 + 0.000120 + 0.001300, 8), total
    assert body["history_len"] == 6, body["history_len"]

    # Сумма — не пересказ последнего обмена: слагаемые лежат в стенограмме,
    # и клиент рисует по ним строку под каждым ответом.
    answers = [t for t in body["transcript"] if t["role"] == "assistant"]
    assert [a["metrics"]["total_tokens"] for a in answers] == [16, 160, 1307], answers

    # Перегенерация обмен заменяет, а не добавляет: сумма не удваивается.
    _stub.install(reply="снова", usage=lambda i: _usage(1000, 100, 1100, 0.001))
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{agent_id}/regenerate")
        after = client.get(f"/api/agents/{agent_id}").json()
    assert after["history_len"] == 6, after["history_len"]
    assert after["usage_total"]["total_tokens"] == 16 + 160 + 1100, after["usage_total"]
    return (
        f"вход {total['prompt_tokens']}, выход {total['completion_tokens']}, "
        f"всего {total['total_tokens']} за {len(answers)} обмена — сумма сошлась"
    )


@check("реплики без чисел пропускаются, а не считаются нулём")
def check_usage_summary_skips_unknown():
    """Ноль и «неизвестно» — разные вещи, и на экране выглядят по-разному.
    Провайдер вправе смолчать о цене (или о токенах вовсе): такая реплика
    в сумму не входит ни слагаемым, ни нулём, а чат, где чисел не принёс
    никто, даёт `None` — прочерк, а не «0 токенов»."""
    from app.agent import Agent

    spec = AgentSpec(label="тихий", model="stub/model")
    quiet = Agent(spec)
    quiet.remember("user", "вопрос")
    quiet.remember("assistant", "ответ", metrics=None)
    quiet.remember("user", "ещё")
    quiet.remember("assistant", "ответ", metrics={"provider": "stub", "cost_usd": None})
    assert quiet.usage_summary() is None, quiet.usage_summary()
    assert quiet.as_dict()["usage_total"] is None, quiet.as_dict()["usage_total"]
    # Сумм нет, а разговор был: плитка «Сообщений» считает сообщения, а не
    # слагаемые сумм.
    assert quiet.as_dict()["history_len"] == 4, quiet.as_dict()["history_len"]

    mixed = Agent(spec)
    mixed.remember("user", "вопрос")
    mixed.remember("assistant", "ответ", metrics=_usage(100, 10, 110, None))
    mixed.remember("user", "ещё")
    mixed.remember("assistant", "ответ", metrics=None)
    mixed.remember("user", "и ещё")
    mixed.remember("assistant", "ответ", metrics=_usage(200, 20, 220, 0.0002))
    total = mixed.usage_summary()
    assert total["prompt_tokens"] == 300, total
    assert total["total_tokens"] == 330, total
    # Цену назвал один ответ из трёх — сумма ровно его, а не «0 + 0 + цена».
    assert total["cost_usd"] == 0.0002, total
    assert mixed.as_dict()["history_len"] == 6, mixed.as_dict()["history_len"]

    # Вопросы пользователя в сумму не идут, даже если метрики к ним прицепили.
    sneaky = Agent(spec)
    sneaky.remember("user", "вопрос", metrics=_usage(999, 999, 999, 9.0))
    assert sneaky.usage_summary() is None, sneaky.usage_summary()
    return "ответ без чисел пропущен, чат без чисел даёт None, вопросы не считаются"


# --- День 17: транспорт вызовов инструментов -----------------------------------


@check("вызовы инструментов: куски склеены по index, событие одно и раньше done")
def check_tool_calls_transport():
    """День 17 начинается с транспорта: агент будет вызывать инструменты MCP
    из приложения и пользоваться результатом. Значит первое, что обязано быть
    настоящим, — разбор потока с вызовами, и проверка гоняет настоящий
    `stream_completion` на кусках той формы, что описана в машинной схеме
    OpenRouter, а не в их прозе.

    Пять свойств этой формы, каждое из которых ломает наивный разбор:

    - `finish_reason` лежит на `choices[0]`, а не в `delta` (потоковый пример
      из документации OpenRouter проверяет `delta.finish_reason` и не
      срабатывает никогда);
    - у фрагмента вызова обязателен только `index`: `id`, `type` и
      `function.name` вправе отсутствовать в любом отдельном куске,
      а `arguments` приезжают обрывками и склеиваются строкой;
    - `finish_reason: "tool_calls"` приходит дважды — на последнем
      содержательном куске и ещё раз на куске с `usage`;
    - текст и вызов приезжают вместе, иногда в одном куске;
    - `arguments` вправе оказаться битым JSON, и ошибки провайдер не пришлёт.

    Отсюда и страховка: модель без поддержки инструментов OpenRouter подменяет
    шаблоном и отдаёт обычный текст, поэтому событие держится на наличии
    накопленных вызовов, а не на слове про причину.
    """
    def piece(index, **fields):
        """Кусок потока с одним фрагментом вызова — форма из схемы."""
        return {"choices": [{"delta": {"tool_calls": [{"index": index, **fields}]}}]}

    # --- два вызова вперемешку, обрывками, и причина дважды -------------------
    events, _ = _streamed(
        _sse_chunks(
            # Второй вызов начинается раньше, чем кончился первый: порядок
            # в событии задаёт `index`, а не порядок приезда.
            piece(1, id="call_b", type="function",
                  function={"name": "save_file", "arguments": '{"ok":'}),
            piece(0, id="call_a", type="function",
                  function={"name": "git_log", "arguments": '{"steps":'}),
            # Поздний кусок с чужими `id` и именем: в потоке это переповтор
            # уже присланных полей, а не поправка. Первое появление выигрывает,
            # затирать собранное нельзя.
            piece(0, id="call_x", function={"name": "forget_log"}),
            piece(1, function={"arguments": " true"}),
            # Кусок, несущий **только** `index`: ни `id`, ни имени, ни
            # аргументов. Вызов обязан собраться целиком и от него не пострадать.
            piece(0),
            piece(0, function={"arguments": ' ["раз",'}),
            # Кусок без номера и кусок с номером не-числом: `index` —
            # единственное обязательное поле фрагмента, и провайдер, нарушивший
            # свою же схему, не вправе ни уронить стрим, ни завести третий
            # вызов, ни дописать мусор в чужие аргументы.
            {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "мусор"}}]}}]},
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": "0", "function": {"arguments": "мусор"}}]}}
                ]
            },
            piece(1, function={"arguments": "}"}),
            piece(0, function={"arguments": ' "два"]}'}),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            # Причина приезжает вторым разом — на куске с usage.
            {
                "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
            },
        )
    )
    kinds = [e["type"] for e in events]
    assert kinds.count("tool_calls") == 1, kinds
    assert kinds.index("tool_calls") < kinds.index("done"), kinds
    announced = events[kinds.index("tool_calls")]
    assert announced["calls"] == [
        {"id": "call_a", "name": "git_log", "arguments": '{"steps": ["раз", "два"]}'},
        {"id": "call_b", "name": "save_file", "arguments": '{"ok": true}'},
    ], announced["calls"]
    assert announced["metrics"]["finish_reason"] == "tool_calls", announced["metrics"]

    # --- текст и вызов в одном ответе ----------------------------------------
    mixed, _ = _streamed(
        _sse_chunks(
            {"choices": [{"delta": {"content": "Сейчас "}}]},
            # Слова и вызов в одном куске: `if/else` потерял бы одно из двух.
            {
                "choices": [
                    {
                        "delta": {
                            "content": "посмотрю журнал.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_m",
                                    "function": {
                                        "name": "git_log",
                                        "arguments": '{"steps": []}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
    )
    mixed_kinds = [e["type"] for e in mixed]
    assert mixed_kinds.count("delta") == 2, mixed_kinds
    assert mixed_kinds.count("tool_calls") == 1, mixed_kinds
    assert mixed[-1]["type"] == "done", mixed_kinds
    assert mixed[-1]["text"] == "Сейчас посмотрю журнал.", mixed[-1]
    mixed_calls = mixed[mixed_kinds.index("tool_calls")]["calls"]
    assert mixed_calls == [
        {"id": "call_m", "name": "git_log", "arguments": '{"steps": []}'}
    ], mixed_calls

    # --- страховка: вызовы есть, а причина приехала «stop» -------------------
    saved, _ = _streamed(
        _sse_chunks(
            piece(0, id="call_s", function={"name": "save_file", "arguments": "{}"}),
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
    )
    saved_kinds = [e["type"] for e in saved]
    assert saved_kinds.count("tool_calls") == 1, saved_kinds
    assert saved_kinds.index("tool_calls") < saved_kinds.index("done"), saved_kinds
    # Причину транспорт не переписывает: сказали «stop» — значит stop. Событие
    # держится на накопленных вызовах, а не на слове про причину.
    assert saved[saved_kinds.index("tool_calls")]["metrics"]["finish_reason"] == "stop", saved

    # --- id провайдер вправе не прислать -------------------------------------
    nameless, _ = _streamed(
        _sse_chunks(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                # Один без `id` вовсе, второй с пустым: в ответном
                                # сообщении `tool_call_id` обязателен, и пустая
                                # строка тут ничем не лучше отсутствия.
                                {
                                    "index": 2,
                                    "function": {"name": "save_file", "arguments": "{}"},
                                },
                                {
                                    "index": 0,
                                    "id": "",
                                    "function": {"name": "git_log", "arguments": "{}"},
                                },
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
    )
    ids = [c["id"] for c in next(e for e in nameless if e["type"] == "tool_calls")["calls"]]
    # Номер в подставленном id — тот самый `index`, а не порядковый счётчик:
    # у второго вызова индекс 2, и id обязан сказать 2.
    assert ids == ["call_0", "call_2"], ids
    assert all(ids), ids

    # --- битый JSON в аргументах ---------------------------------------------
    torn = '{"steps": ["раз"'
    raw, _ = _streamed(
        _sse_chunks(
            piece(0, id="call_j", function={"name": "git_log", "arguments": torn}),
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        )
    )
    torn_call = next(e for e in raw if e["type"] == "tool_calls")["calls"][0]
    assert torn_call["arguments"] == torn, torn_call
    # И строка эта правда неразбираемая: разбери её транспорт — упал бы он,
    # а разбирать её будет следующий слой и под `try`.
    broke = False
    try:
        json.loads(torn_call["arguments"])
    except ValueError:
        broke = True
    assert broke, torn_call["arguments"]

    # --- объявление инструментов в теле запроса ------------------------------
    log_tool = {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "показать журнал коммитов репозитория",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
    }
    plain = _sse_chunks({"choices": [{"delta": {"content": "ок"}, "finish_reason": "stop"}]})

    _, declared = _streamed(plain, tools=[log_tool])
    assert declared["tools"] == [log_tool], declared.get("tools")
    # `tool_choice` не отправляется вовсе: звать инструмент или ответить
    # словами — решение модели.
    assert "tool_choice" not in declared, declared

    _, silent = _streamed(plain)
    assert "tools" not in silent, silent
    # Пустой список — это тоже «не объявлять ничего»: `tools: []` у части
    # провайдеров значит другое, чем отсутствие ключа.
    _, empty = _streamed(plain, tools=[])
    assert "tools" not in empty, empty
    # Помимо инструментов в теле не поменялось ничего: три правила на месте
    # у обоих запросов.
    assert not _body_rules(declared) and not _body_rules(silent), (declared, silent)

    # Своё тело чата перебивает инструменты — как перебивает всё остальное.
    mine = {"type": "function", "function": {"name": "своё"}}
    _, overridden = _streamed(
        plain,
        spec=AgentSpec(label="своё тело", model="stub/tools", extra_body={"tools": [mine]}),
        tools=[log_tool],
    )
    assert overridden["tools"] == [mine], overridden["tools"]

    # --- незнакомый тип события обмен переживает ------------------------------
    #
    # Реестр пуст (пустая конфигурация) — значит инструменты не объявлены,
    # и событие `tool_calls` агент исполнять не вправе: он переживает его
    # молча, обмен идёт одним обращением и записывается. Заглушка отдаёт
    # событие той же формы, что настоящий транспорт.
    _stub.install(
        reply="ок",
        tool_calls=[{"id": "call_a", "name": "git_log", "arguments": "{}"}],
    )
    with TestClient(main.app) as client:
        chat = new_agent(client)
        answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
        assert answer.status_code == 200, answer.text
        frames = sse(answer.text)
        history = client.get(f"/api/agents/{chat}").json()["transcript"]
    frame_kinds = [f["event"] for f in frames]
    # Наружу событие не уехало: `ask` перечисляет известные имена и чужое
    # не пересылает. И исполнения не было: реестр пуст, вызывать нечего.
    assert "tool_calls" not in frame_kinds and "tool_call" not in frame_kinds, frame_kinds
    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    assert frame_kinds[-1] == "done" and frames[-1]["committed"] is True, frames[-1]
    assert [m["role"] for m in history] == ["user", "assistant"], history
    assert history[-1]["content"] == "ок", history[-1]

    return (
        "два вызова собраны из обрывков вперемешку, событие одно на две причины "
        "и раньше done; текст и вызов вместе; страховка на «stop»; id без "
        "провайдера — call_{index}; битый JSON отдан строкой; tools объявлены, "
        "а без них ключа в теле нет и extra_body их перебивает; обмен "
        "переживает незнакомый тип события и записывается"
    )


# --- День 17: цикл вызовов инструментов --------------------------------------
#
# Модель просит вызов — агент исполняет его на настоящем сервере (git из
# `app/mcp_servers/git.py`, процессом через тот же McpManager) и повторяет
# запрос уже с результатом. Заглушка отвечает на первый вызов `tool_calls`,
# а на второй читает tool-сообщение **из запроса** — там же, где прочла бы
# его модель. Поэтому утверждение «ответ использует результат» не зависит
# от заглушки: она отвечает тем, что лежит в промпте.

GIT = {"module": "app.mcp_servers.git", "timeout_s": 15}


def _git_head_line() -> str:
    """Первая строка настоящего `git log -1` — тем же вызовом, что у сервера."""
    run = subprocess.run(
        ["git", "log", "-1", "--format=%h %ad %s", "--date=short"],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def _tool_reply(messages, index):
    """Первый вызов — молчание (модель позвала инструмент без предисловий),
    дальше — тем, что лежит в tool-сообщении, или честным «сам по себе»,
    если его нет. Утверждение о втором запросе стоит на различении."""
    if index == 0:
        return ""
    tool = next((m for m in messages if m.get("role") == "tool"), None)
    return "вижу результат: " + tool["content"] if tool else "сам по себе"


def _first_call_only(name, arguments):
    """`tool_calls` на первый вызов и дальше словами — заглушка в функцию,
    как `reply`."""
    return lambda messages, index: (
        None if index else [{"id": "call_1", "name": name, "arguments": arguments}]
    )


@check("вызов инструмента: исполнен на настоящем сервере, второй запрос несёт результат")
def check_tool_call_loop():
    """Сквозной сценарий дня. Заглушка на первый вызов отвечает `tool_calls`
    с просьбой о `git_log`; агент обязан исполнить его на настоящем
    git-сервере и повторить запрос с assistant-`tool_calls` и `role: "tool"`,
    где лежит настоящий последний коммит репозитория. Финальный ответ —
    заглушкин пересказ этого сообщения: «ответ использует результат»
    проверяется там же, где его увидела бы модель."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(
            reply=_tool_reply,
            tool_calls=_first_call_only("git_log", '{"n": 1}'),
        )
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(
                    f"/api/agents/{chat}/messages", json={"text": "покажи журнал"}
                )
                assert answer.status_code == 200, answer.text
                frames = sse(answer.text)
                body = client.get(f"/api/agents/{chat}").json()

    # Два обращения к модели: с просьбой о вызове и с его результатом.
    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    head = _git_head_line()
    first, second = _stub.CALLS
    # Инструменты объявлены — все три, с описанием и схемой от самого сервера.
    declared = {t["function"]["name"]: t["function"] for t in first["payload"]["tools"]}
    assert sorted(declared) == ["git_diff_stat", "git_log", "git_status"], sorted(declared)
    assert declared["git_log"]["description"], declared["git_log"]
    assert "n" in declared["git_log"]["parameters"]["properties"], declared["git_log"]

    # Второй запрос: за промптом — assistant с вызовами и tool с результатом.
    sent = second["payload"]["messages"]
    roles = [m["role"] for m in sent]
    assert roles[-2:] == ["assistant", "tool"], roles
    asked = sent[-2]
    assert asked["tool_calls"][0]["id"] == "call_1", asked
    assert asked["tool_calls"][0]["function"]["arguments"] == '{"n": 1}', asked
    tool = sent[-1]
    assert tool["tool_call_id"] == "call_1", tool
    assert tool["content"] == head, (tool["content"], head)

    # SSE: кадр на вызов — до done, с именем, сервером, миллисекундами и ok.
    kinds = [f["event"] for f in frames]
    assert "tool_calls" not in kinds, kinds  # внутреннее событие наружу не уехало
    badge = _frame(frames, "tool_call")
    assert kinds.index("tool_call") < kinds.index("done"), kinds
    assert badge["name"] == "git_log" and badge["server"] == "git", badge
    assert badge["arguments"] == {"n": 1} and badge["result"] == head, badge
    assert badge["ok"] is True and badge["ms"] >= 0, badge

    # Финальный ответ результат использует, история попарная, а в метриках —
    # запись вызова с именем, сервером и миллисекундами.
    answer_turn = body["transcript"][-1]
    assert [t["role"] for t in body["transcript"]] == ["user", "assistant"], body["transcript"]
    assert answer_turn["content"] == "вижу результат: " + head, answer_turn["content"]
    runs = answer_turn["metrics"]["tool_calls"]
    assert len(runs) == 1, runs
    assert runs[0]["name"] == "git_log" and runs[0]["server"] == "git", runs
    assert runs[0]["ok"] is True and runs[0]["ms"] >= 0, runs
    return (
        f"git_log исполнен процессом, второй запрос несёт «{head[:40]}…», "
        "ответ его пересказывает, кадр и метрика на месте"
    )


@check("tools объявляются только при непустом реестре: пустой — неотличим от дня 15")
def check_tools_only_with_registry():
    """Реестр пуст (пустая конфигурация): тело запроса ключа `tools` не имеет
    вовсе — ни с пустым списком, ни с чужим. Слать всегда значило бы назвать
    модели инструменты, которых у нас нет."""
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        chat = new_agent(client)
        answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
        assert answer.status_code == 200, answer.text
    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    assert "tools" not in _stub.CALLS[0]["payload"], _stub.CALLS[0]["payload"]
    return "пустой реестр — тело без ключа tools; с реестром — см. сквозной сценарий"


@check("битый JSON аргументов — tool-сообщение с ошибкой, а не падение обмена")
def check_tool_call_broken_arguments():
    """Модель прислала неразбираемые аргументы. Падать нельзя: её вызов
    обязан кончиться tool-сообщением с понятной ошибкой — иначе она позовёт
    его снова с теми же аргументами. Сервер при этом не дёргается вовсе:
    вызывать нечем."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(
            reply=_tool_reply,
            tool_calls=_first_call_only("git_log", '{"n":'),
        )
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
                assert answer.status_code == 200, answer.text
                frames = sse(answer.text)
                body = client.get(f"/api/agents/{chat}").json()

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    tool = _stub.CALLS[1]["payload"]["messages"][-1]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_1", tool
    assert "не разобрать как JSON" in tool["content"], tool["content"]
    badge = _frame(frames, "tool_call")
    assert badge["ok"] is False, badge
    # Обмен завершён штатно: финальный ответ прочитал ошибку и записался.
    assert frames[-1]["event"] == "done" and frames[-1]["committed"] is True, frames[-1]
    assert body["transcript"][-1]["content"].startswith("вижу результат: аргументы"), (
        body["transcript"][-1]["content"]
    )
    assert body["transcript"][-1]["metrics"]["tool_calls"][0]["ok"] is False, (
        body["transcript"][-1]["metrics"]
    )
    return "tool-сообщение с ошибкой вместо падения, кадр с ok=false, обмен записан"


@check("сервер вышел после initialize: ошибка вызова возвращается модели, обмен записан")
def check_tool_server_exits_before_call():
    import tempfile

    from app.mcp import MANAGER

    with tempfile.TemporaryDirectory(prefix="check-tools-down-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(reply=_tool_reply, tool_calls=_first_call_only("git_status", "{}"))
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                process = MANAGER.servers[0].process
                assert process is not None and process.returncode is None
                process.terminate()
                client.portal.call(process.wait)
                assert process.returncode is not None
                chat = new_agent(client)
                response = client.post(f"/api/agents/{chat}/messages", json={"text": "статус"})
                assert response.status_code == 200, response.text
                frames = sse(response.text)
                saved = client.get(f"/api/agents/{chat}").json()["transcript"]

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    tool = _stub.CALLS[1]["payload"]["messages"][-1]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call_1", tool
    badge = _frame(frames, "tool_call")
    assert badge["ok"] is False and badge["server"] == "git", badge
    assert badge["result"] == tool["content"] and tool["content"], tool
    assert frames[-1]["committed"] is True, frames[-1]
    assert saved[-1]["content"] == "вижу результат: " + tool["content"], saved
    assert saved[-1]["metrics"]["tool_calls"][0]["ok"] is False, saved[-1]
    return "настоящий subprocess завершён; ошибка в tool-сообщении, failed-бейдже и сохранённых метриках"


@check("модель зовёт инструменты вечно — стоп на MAX_TOOL_ITER, накопленное отдано")
def check_tool_call_limit():
    """Заглушка отвечает `tool_calls` на каждый вызов. Цикл обязан
    остановиться: исполнено ровно MAX_TOOL_ITER вызовов, обращений к модели
    на одно больше (последнее — уже без исполнения), а накопленный текст
    отдан. Без лимита это было бы зависание на оплаченных вызовах."""
    import tempfile

    from app.agent import MAX_TOOL_ITER

    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(
            reply="ответ",
            tool_calls=[{"id": "call_x", "name": "git_status", "arguments": "{}"}],
        )
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
                assert answer.status_code == 200, answer.text
                frames = sse(answer.text)
                body = client.get(f"/api/agents/{chat}").json()

    # Оборотов на один больше исполнений: последний запрос модели уже не
    # исполняется — лимит стопит цикл ДО вызова, а не после.
    assert len(_stub.CALLS) == MAX_TOOL_ITER + 1, len(_stub.CALLS)
    last_roles = [m["role"] for m in _stub.CALLS[-1]["payload"]["messages"]]
    assert last_roles.count("tool") == MAX_TOOL_ITER, last_roles
    kinds = [f["event"] for f in frames]
    assert kinds.count("tool_call") == MAX_TOOL_ITER, kinds
    metrics = body["transcript"][-1]["metrics"]
    assert metrics["tool_iterations"] == MAX_TOOL_ITER, metrics
    assert len(metrics["tool_calls"]) == MAX_TOOL_ITER, metrics
    # Накопленное отдано: текст всех итераций в ответе, обмен записан.
    assert body["transcript"][-1]["content"] == "ответ" * (MAX_TOOL_ITER + 1), (
        body["transcript"][-1]["content"]
    )
    assert frames[-1]["committed"] is True, frames[-1]
    return f"{MAX_TOOL_ITER} исполнений из {MAX_TOOL_ITER + 1} обращений, лимит назван в метриках"


@check("usage по итерациям складывается: вызовы модели после первого оплачены")
def check_tool_usage_sums_iterations():
    """Каждая итерация цикла — отдельный оплаченный вызов. Сумма, берущая
    только последний, врала бы ровно на стоимость первого — прецедент тот же,
    что у метрик сводок вторым проходом."""
    import tempfile

    plan = [_usage(100, 10, 110, 0.001), _usage(200, 20, 220, 0.002)]
    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(
            reply="итог",
            usage=lambda i: plan[i],
            tool_calls=_first_call_only("git_status", "{}"),
        )
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
                assert answer.status_code == 200, answer.text
                body = client.get(f"/api/agents/{chat}").json()

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    metrics = body["transcript"][-1]["metrics"]
    assert metrics["prompt_tokens"] == 300, metrics
    assert metrics["completion_tokens"] == 30, metrics
    assert metrics["total_tokens"] == 330, metrics
    assert metrics["cost_usd"] == 0.003, metrics
    # И итог по чату согласен с ней: он считается из тех же записей.
    assert body["usage_total"]["total_tokens"] == 330, body["usage_total"]
    return "110 + 220 = 330 токенов и $0.003 — сумма по обеим итерациям"


@check("история в базе попарная: ни tool-сообщений, ни дыр в seq")
def check_tool_history_stays_pairwise():
    """Цикл вызовов живёт в рабочем списке сообщений и умирает с обменом:
    в `messages` ложатся только вопрос и финальный ответ. Иначе перегенерация
    (`take_last_exchange` ждёт хвост user/assistant), свёртка и нумерация
    поехали бы разом. Смотрим в сам файл базы, а не в стенограмму."""
    import sqlite3
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(
            reply="готово",
            tool_calls=_first_call_only("git_log", '{"n": 1}'),
        )
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                first = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
                assert first.status_code == 200, first.text
                # Следующий обмен видит только финальный ответ: результат
                # вызова он уже впитал, и второго экземпляра ему не надо.
                second = client.post(f"/api/agents/{chat}/messages", json={"text": "два"})
                assert second.status_code == 200, second.text

    conn = sqlite3.connect(REGISTRY.store.path)
    rows = conn.execute(
        "SELECT seq, role FROM messages WHERE session_id = ? ORDER BY seq", (chat,)
    ).fetchall()
    conn.close()
    assert rows == [(0, "user"), (1, "assistant"), (2, "user"), (3, "assistant")], rows
    followup = _stub.CALLS[-1]["payload"]["messages"]
    assert "tool" not in [m["role"] for m in followup], [m["role"] for m in followup]
    assert not any("tool_calls" in m for m in followup), followup
    return "в базе две пары user/assistant, seq без дыр, следующий промпт без tool-ролей"


@check("сжатию инструменты не объявляются: оно пересказывает разговор, а не работает")
def check_compress_declares_no_tools():
    """Вызов на сжатие — служебный: ему пересказывать, и вызовы инструментов
    ему ни к чему. При непустом реестре обычные обращения несут `tools`,
    а сжатие — нет: иначе модель на свёртке позвала бы инструмент, и цикл
    вызовов начал бы работать внутри пересказа."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})
        _stub.install(reply="слово")
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(
                    client, strategy="summary", keep_last=KEEP, compress_every=EVERY
                )
                _talk(client, chat, 9)

    kinds = [_service_kind(c["messages"]) for c in _stub.CALLS]
    assert "summary" in kinds, kinds  # сворачивание правда случилось
    folding = _stub.CALLS[kinds.index("summary")]
    assert "tools" not in folding["payload"], folding["payload"]
    # А у обычных обращений того же чата инструменты объявлены.
    talking = next(c for c, k in zip(_stub.CALLS, kinds) if k is None)
    assert talking["payload"]["tools"], talking["payload"]
    return "у сжатия ключа tools нет, у обменов того же чата — есть"


@check("git-сервер: три инструмента, настоящий журнал и честная ругань git")
def check_git_server():
    """По живому процессу: список с описаниями и схемами, `git_log` отвечает
    настоящим последним коммитом, а на несуществующий ref едет текст самого
    git — сервер его не прячет и не переводит."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-tools-") as tmp:
        cfg = _mcp_config(tmp, {"git": GIT})

        async def scenario():
            with _mcp_env(cfg):
                manager = McpManager()
                await manager.start()
                try:
                    assert len(manager.servers) == 1, manager.servers
                    server = manager.servers[0]
                    assert server.status == "ok", server.error
                    tools = {t["name"]: t for t in server.view}
                    assert sorted(tools) == ["git_diff_stat", "git_log", "git_status"], tools
                    for name, tool in tools.items():
                        assert tool["description"], f"{name}: нет описания"
                        assert tool["schema"].get("properties") is not None, f"{name}: нет схемы"
                    assert "n" in tools["git_log"]["schema"]["properties"], tools["git_log"]
                    assert "ref" in tools["git_diff_stat"]["schema"]["properties"], (
                        tools["git_diff_stat"]
                    )
                    log = await manager.call("git_log", {"n": 1})
                    assert log.content[0].text == _git_head_line(), log.content[0].text
                    bad = await manager.call("git_diff_stat", {"ref": "несуществующий-ref"})
                    return bad.content[0].text
                finally:
                    await manager.stop()

        error = asyncio.run(scenario())
    # Ругань — собственными словами git: имя ref в ней названо.
    assert "несуществующий-ref" in error, error
    assert "fatal" in error, error
    return "журнал настоящий, на плохой ref — текст самого git, не прятки"


# --- День 7: память переживает перезапуск, чаты изолированы -------------------


@check("сводка по чату переживает перезапуск: числа те же из файла базы")
def check_usage_summary_survives_restart():
    """Сводка выводится из `messages.metrics`, а не хранится колонкой. Значит
    доказательство — настоящее переоткрытие файла: жила бы сумма в памяти
    процесса, после перезапуска она обнулилась бы."""
    from app.agent import Agent

    path = _temp_db("usage-restart")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="память", model="stub/model"), store=store)
    agent.remember("user", "вопрос")
    agent.remember("assistant", "ответ", metrics=_usage(70, 30, 100, 0.00007))
    agent.remember("user", "ещё")
    agent.remember("assistant", "ответ", metrics=_usage(230, 90, 320, 0.00023))
    before = agent.usage_summary()
    agent_id = agent.id

    with _restarted(store, agent_id) as (again, revived):
        after = revived.usage_summary()

    assert before == after, (before, after)
    assert after["total_tokens"] == 420, after
    assert revived.as_dict()["history_len"] == 4, revived.as_dict()["history_len"]
    return f"после переоткрытия файла всего {after['total_tokens']} — как и до него"


@check("диалог продолжается в новом процессе программы")
def check_restart_process():
    return _script("restart.py", "ОК: диалог продолжился", 2)


@check("два процесса на одной базе: id не пересекаются, чужой диалог цел")
def check_two_processes():
    return _script("two_processes.py", "ОК: два процесса", -3)


@check("любое поле конфига и вся история переживают переоткрытие файла")
def check_config_survives_by_construction():
    """Список полей **выводится** из `AgentSpec`, а не перечисляется:
    перечисленный отстал бы от датакласса ровно тогда, когда поле добавили
    и забыли — в единственном случае, ради которого проверка нужна. История
    длиннее прежних отсечек: вернись любая при записи или при подъёме —
    станет красно."""
    from dataclasses import fields as spec_fields

    from app.agent import Agent

    # Значение, отличное от умолчания, для каждого поля конфига.
    probes: dict = {}
    for f in spec_fields(AgentSpec):
        kind = str(f.type)
        if f.name == "model":
            probes[f.name] = "проверка/модель"
        elif kind == "bool":
            probes[f.name] = True
        elif "list[str]" in kind:
            probes[f.name] = [f"СТОП-{f.name}"]
        elif "str" in kind:
            probes[f.name] = f"текст-{f.name}"
        elif "int" in kind:
            probes[f.name] = 7
        elif "float" in kind:
            probes[f.name] = 0.25
        elif "dict" in kind:
            probes[f.name] = {"метка": f.name}
        else:
            raise AssertionError(f"поле {f.name}: {f.type} — тип неизвестен, подберите значение")
    assert len(probes) == len(spec_fields(AgentSpec)), probes

    messages = 500
    path = _temp_db("schema-roundtrip")
    store = Store(path).init()
    spec = AgentSpec(**probes)
    agent = Agent(spec, store=store)
    _fill(agent, messages)
    agent.remember("assistant", "ответ", metrics={"provider": "stub"})
    agent.persist()
    agent_id = agent.id

    with _restarted(store, agent_id) as (again, revived):
        lost = {
            f.name: (probes[f.name], getattr(revived.spec, f.name))
            for f in spec_fields(AgentSpec)
            if getattr(revived.spec, f.name) != probes[f.name]
        }
        assert not lost, f"поля конфига не пережили переоткрытие файла: {lost}"

        assert len(revived.history) == messages + 1, f"из базы поднялось {len(revived.history)}"
        assert revived.history[0].content == "реплика 0", "у поднятого чата отъели начало"
        # Число сообщений у чата, которого нет в памяти, считает SQL — и это
        # то же самое число, которое показывает плитка: реплика к реплике,
        # и вопросы, и ответы. Разойдись счёт, список слева и консоль назвали
        # бы одну длину, а открытый чат — другую.
        listed = {row["id"]: row["history_len"] for row in again.list_sessions()}
        assert listed[agent_id] == len(revived.history), (listed[agent_id], len(revived.history))
        assert revived.history[-1].metrics == {"provider": "stub"}, revived.history[-1].metrics
        # И это же целиком уезжает в модель: восстановленная история — обычная.
        prompt = revived.build_prompt("новый вопрос")
        assert len(prompt) == 1 + messages + 1 + 1, len(prompt)
        assert prompt[1]["content"] == "реплика 0", prompt[1]

        # Колонки схемы и колонки, которые читает код, — один набор.
        # Лишняя колонка так же плоха, как потерянная: она либо мёртвая,
        # либо её кто-то пишет мимо `_session_row`.
        in_db = {r["name"] for r in again.conn.execute("PRAGMA table_info(sessions)")}
        row = again.load_session(agent_id)
        expected = set(row) | {"updated_at"}
        assert in_db == expected, f"схема и код разошлись: в базе {in_db}, код читает {expected}"
    return (
        f"{len(probes)} полей конфига и история из {messages + 1} реплик пережили "
        "переоткрытие файла, схема сходится с кодом"
    )


@check("изоляция чатов: у сообщений есть session_id, ленты не сливаются")
def check_session_isolation():
    _stub.install(reply=lambda m, i: f"ответ{i}")
    with TestClient(main.app) as client:
        # Память выключена обоим: ответ заглушки пронумерован вызовами,
        # а проверка сверяет ленты дословно.
        first = new_agent(client, label="чат 1")
        second = new_agent(client, label="чат 2")
        client.post(f"/api/agents/{first}/messages", json={"text": "меня зовут Нина"})
        _stub.reset()
        client.post(f"/api/agents/{second}/messages", json={"text": "как меня зовут?"})

    asked = " ".join(m["content"] for m in _stub.CALLS[0]["messages"])
    assert "Нина" not in asked, f"вторая сессия видит чужую историю: {asked}"

    store = REGISTRY.store
    assert [r[2] for r in store.message_rows(first)] == ["меня зовут Нина", "ответ0"]
    assert [r[2] for r in store.message_rows(second)] == ["как меня зовут?", "ответ0"]

    # И тот же путь, которым лента поднимается после перезапуска: выборка
    # обязана быть по `session_id`. Условие, которое его не сужает, даёт
    # каждому чату все строки базы — ленты сливаются в одну, и заметно это
    # только после перезапуска.
    assert [m["content"] for m in store.load_messages(first)] == ["меня зовут Нина", "ответ0"], (
        store.load_messages(first)
    )
    assert [m["content"] for m in store.load_messages(second)] == ["как меня зовут?", "ответ0"], (
        store.load_messages(second)
    )

    # Схема не даёт записать реплику без сессии: ключ составной, и это
    # единственная защита от «все чаты в одной ленте» после перезапуска.
    keys = [row[1] for row in store.conn.execute("PRAGMA table_info(messages)") if row[5]]
    assert keys == ["session_id", "seq"], keys
    indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(messages)")}
    assert "messages_by_session" in indexes, indexes
    return "две сессии — две ленты; PK (session_id, seq), индекс по session_id есть"


@check("seq без дыр: откат и укорачивание пересчитывают номера от нуля")
def check_seq_renumbered():
    """Номера строк — не отделка хранения: по ним история поднимается в том же
    порядке (`ORDER BY seq`), и дыра или сдвиг значат, что `save_history`
    дописывает хвост вместо того, чтобы переписать историю целиком. А хвост
    после отката оставил бы в базе ответ, которого в истории уже нет.

    Вторая половина — про то, что записывать нечего: несостоявшийся обмен
    не оставляет вопроса без ответа. Мутациями проверено, что без этой
    проверки молча проходят все три поломки: `seq = i * 2`,
    `enumerate(turns, start=5)` и запись вопроса при пустом ответе.
    """
    from app.agent import Agent, Turn

    path = _temp_db("seq")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="seq", model="stub/m"), store=store)

    # 1) Ответа не случилось — в базе не появилось ничего, даже вопроса.
    _stub.install(fail=True)
    asyncio.run(drain(agent.ask("вопрос, на который не ответили")))
    assert store.message_rows(agent.id) == [], store.message_rows(agent.id)

    # 2) Обычные обмены: номера идут подряд и от нуля.
    _stub.install(reply="ок")
    _ask(agent, 3)
    seqs = [row[0] for row in store.message_rows(agent.id)]
    assert seqs == list(range(6)), f"номера пошли с дырами или со сдвигом: {seqs}"

    # 3) Укорачивание истории — так выглядит откат обмена и перегенерация.
    agent.history = agent.history[-2:]
    agent.persist()
    rows = store.message_rows(agent.id)
    assert [r[0] for r in rows] == [0, 1], f"после укорачивания номера не от нуля: {rows}"
    assert [r[2] for r in rows] == ["вопрос 2", "ок"], rows

    # 4) Длинный чат пишется целиком: ни отсечки, ни дыр в нумерации.
    long_chat = 500
    agent.history = [Turn(role="user", content=f"т{i}") for i in range(long_chat)]
    agent.persist()
    rows = store.message_rows(agent.id)
    assert len(rows) == long_chat, f"историю подрезали при записи: {len(rows)}"
    assert [r[0] for r in rows] == list(range(long_chat)), "номера пошли с дырами"
    assert rows[0][2] == "т0", rows[0]
    store.close()
    return f"нет ответа — нет записи; seq = 0..{long_chat - 1} подряд после укорачивания"


@check("транзакция берёт блокировку сразу; занятая база — внятный 503")
def check_tx_locks_immediately():
    """`BEGIN IMMEDIATE`, а не голый `BEGIN` — и это тот самый блокирующий баг:
    второй `uvicorn` на той же базе молча уничтожал чужой чат.

    Отложенная транзакция берёт блокировку на первой записи. Начавшись
    с чтения, при повышении до записи она получает SQLITE_BUSY **мимо**
    `busy_timeout`: ретрая нет, и второй процесс получает ошибку вместо
    очереди. `two_processes.py` этого не ловит — там все пути записи
    начинаются с записи.

    Проверяется наблюдаемым: пока транзакция открыта и не сделала ни одного
    запроса, второй писатель обязан её видеть. Соединение берём с нулевым
    таймаутом — ждать нечего, нужен сам факт блокировки.

    И то, ради чего блокировка нужна: дождавшийся своей очереди ждёт молча,
    а не дождавшийся получает внятный 503 с объяснением, а не голый 500.
    """
    import sqlite3

    from app.store import StoreBusyError, _busy

    path = _temp_db("immediate")
    store = Store(path).init()
    entered = None
    try:
        with store.tx():
            # Ни одного запроса внутри транзакции ещё не было.
            rival = sqlite3.connect(path, timeout=0)
            try:
                rival.execute("BEGIN IMMEDIATE")
                entered = True
            except sqlite3.OperationalError as exc:
                entered = False
                reason = str(exc).lower()
                assert "locked" in reason or "busy" in reason, exc
            finally:
                rival.close()
        assert entered is False, (
            "второй писатель вошёл в базу, пока транзакция открыта: значит она "
            "отложенная. Отложенная берёт блокировку только на первой записи, "
            "и повышение из чтения в запись даёт SQLITE_BUSY мимо busy_timeout"
        )
    finally:
        store.close()

    # «database is locked» — ситуация штатная и переводится в текст; «no such
    # table» — баг схемы, и подменять его успокаивающим текстом нельзя.
    translated = _busy(sqlite3.OperationalError("database is locked"), path)
    assert isinstance(translated, StoreBusyError), type(translated)
    assert "занята другим процессом" in str(translated), translated
    assert "повторите" in str(translated), translated
    assert _busy(sqlite3.OperationalError("no such table: sessions"), path) is None

    # И то же самое наружу: 503 с объяснением, а запись, которая не прошла,
    # не оставила после себя половины чата.
    busy_path = _temp_db("busy")
    waiting = Store(busy_path).init()
    blocker = sqlite3.connect(busy_path, isolation_level=None)
    blocker.execute("PRAGMA busy_timeout=0")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO sessions (id, created_at, updated_at) VALUES ('ag_99999', 1, 1)")
    waiting.conn.execute("PRAGMA busy_timeout=50")
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            with patch.object(main.REGISTRY, "store", waiting):
                response = client.post("/api/agents", json={"agent": {"model": "stub/m"}})
        assert response.status_code == 503, (response.status_code, response.text)
        assert "занята другим процессом" in response.json()["detail"], response.text
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        waiting.close()

    with _reopened(busy_path) as after_busy:
        assert after_busy.list_sessions() == [], after_busy.list_sessions()
    return "открытая транзакция видна второму писателю сразу; занятая база даёт 503"


# --- Сквозное: ключ, сеть, клиент ---------------------------------------------


@check("ключ не попадает в отдаваемые наружу данные")
def check_no_key_leak():
    # Подставляем заведомо ненастоящую строку в форме ключа и смотрим,
    # не вылезет ли она в ответах ручек. Настоящий ключ проверке не нужен.
    import app.config as config

    lure = lambda: "sk-or-v1-ЭТО-НЕ-КЛЮЧ-А-ПРИМАНКА-ДЛЯ-ПРОВЕРКИ"  # noqa: E731
    with patch.object(config, "api_key", lure), TestClient(main.app) as client:
        agent_id = client.post("/api/agents", json={}).json()["agents"][0]["id"]
        bodies = [
            client.get("/api/agents").text,
            client.get("/api/health").text,
            client.get(f"/api/agents/{agent_id}").text,
        ]
    for body in bodies:
        assert "sk-or-v1" not in body, "ключ утёк в ответ ручки"
    return "ручки не отдают подставленный фиктивный ключ"


@check("ключа OpenRouter в базе нет ни в одной колонке — включая ещё не придуманные")
def check_no_key_in_db():
    """Ключ подставляется во все текстовые пути, а не только туда, где есть redact()."""
    from app.agent import Agent

    key = "sk-or-v1-ТЕСТОВЫЙ-КЛЮЧ-КОТОРЫЙ-НЕ-ДОЛЖЕН-УТЕЧЬ"
    path = _temp_db("secret")
    store = Store(path).init()
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": key}):
        agent = Agent(
            AgentSpec(
                label=f"утечка {key}",
                model="stub/m",
                system=key,
                extra_body={"headers": {"Authorization": f"Bearer {key}"}},
                stop=[key],
            ),
            store=store,
        )
        agent.remember("user", f"вот мой ключ {key}")
        agent.remember("assistant", "не надо мне его слать", error=f"HTTP 401: {key}")
        # Сводку пишет модель, и ключ из реплики попадёт в пересказ так же
        # легко, как в саму реплику: таблица новая, а обещание то же.
        store.save_summaries(
            agent.id,
            [{"upto": 2, "content": f"пересказ, в котором засветился {key}",
              "metrics": {"заметка": key}}],
        )
        # И в родство — тем же явным путём. Своих текстов у него нет, но
        # `parent_id` это строка из запроса, и обещание про любой строковый
        # параметр любого запроса касается её наравне с репликой.
        store.save_branch(agent.id, parent_id=f"ag_{key}", forked_at=2)
        # И в рабочую память — тем же явным путём: записи ведёт модель, и ключ
        # из реплики попадёт в них так же легко, как в пересказ: человек
        # вписывает запись руками, и ключ в неё попадает ровно так же, как
        # в реплику, — перепутав окно.
        store.add_working(agent.id, "goal", f"цель с ключом {key}")
        # Правка — по **второй** записи, а не по первой: перепиши она
        # засветившуюся, утечка добавления затёрлась бы чистой правкой,
        # и проверка стерегла бы один путь вместо двух.
        poked = store.add_working(agent.id, "question", "запись под правку")
        store.update_working(
            agent.id, poked["seq"], kind="limit", content=f"правка с ключом {key}"
        )

        # И в долговременную память — тем же явным путём. Слой глобальный
        # и удаление чата его не чистит: утёкший сюда ключ пережил бы и сам
        # чат. Правка — по **второй** записи, как и в рабочей памяти: перепиши
        # она засветившуюся, утечка добавления затёрлась бы чистой правкой.
        store.add_memory("knowledge", f"ключ от панели: {key}")
        marked = store.add_memory("knowledge", "запись под правку")
        store.update_memory(
            marked["seq"], kind="profile", content=f"правка с ключом {key}"
        )

        # И в профиль — тем же явным путём. Он глобальный, как и память,
        # и живёт дольше любого чата: утёкший сюда ключ уехал бы системным
        # сообщением в каждый следующий запрос.
        store.save_profile({"style": f"отвечай как {key}"})

        # А это — про колонки, которых ещё нет: любая запись идёт через
        # транзакцию, и параметр чистится независимо от того, вспомнил ли
        # автор про redact() в этом конкретном методе.
        with store.tx() as conn:
            conn.execute("UPDATE sessions SET label = ? WHERE id = ?", (key, agent.id))
            conn.execute("INSERT INTO meta (key, value) VALUES ('ловушка', ?)", (key,))

        leaked = _columns_holding(store.conn, key)
        assert not leaked, f"ключ лежит в колонках: {leaked}"

        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        store.close()

        folder = os.path.dirname(path)
        files = sorted(os.listdir(folder))
        blob = b""
        for name in files:
            with open(os.path.join(folder, name), "rb") as handle:
                blob += handle.read()
        assert key.encode() not in blob, f"ключ утёк в файлы базы: {files}"
        assert "***".encode() in blob, "ключ должен быть заменён, а не потерян молча"
    return f"ключ не найден ни в одной колонке и ни в одном файле базы ({', '.join(files)})"


@check("мимо redact() записать нельзя: пути записи выводятся из класса")
def check_every_write_path_redacts():
    """Несущий слой чистки — не явные `redact()` в отдельных методах, а `_Writer`:
    транзакция отдаёт обёртку, и чистится любой строковый параметр любого
    запроса. Но само это свойство надо стеречь отдельно: `check_no_key_in_db`
    ходит теми путями записи, которые знает, а новый путь мимо транзакции
    прошёл бы зелёным — мутацией проверено.

    Поэтому список путей здесь **выводится** из класса `Store`: всякий метод,
    в теле которого есть INSERT/UPDATE/DELETE/REPLACE, обязан идти через
    `tx()`, а не через голое соединение. Перечисленный список пропустил бы
    ровно тот метод, который забыли в него внести.
    """
    import inspect
    import sqlite3

    import app.store as store_module

    # Слова целиком, а не подстроки: `updated_at` — это не UPDATE, и по
    # подстроке в список путей записи попадал весь `list_sessions`.
    mutating = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|CREATE)\b")
    checked = []
    for name, fn in inspect.getmembers(Store, inspect.isfunction):
        body = inspect.getsource(fn)
        if not mutating.search(body.upper()):
            continue
        checked.append(name)
        if name == "init":
            continue  # схему заводит executescript, параметров у него нет
        for bypass in ("self.conn.execute", "self._conn.execute", "self.reading("):
            assert bypass not in body, (
                f"Store.{name} пишет через {bypass} — мимо `_Writer`, а значит "
                "мимо redact(). Писать можно только внутри tx()"
            )
        # И то же самое с другой стороны: путь записи обязан **открывать**
        # транзакцию, а не только не трогать соединение по известным именам.
        # Список запрещённых форм ловит те обходы, которые уже видели;
        # способов достать голое соединение больше, чем их в списке, и
        # мутация «миграция идёт мимо tx()» прошла мимо него зелёной.
        assert "self.tx()" in body, (
            f"Store.{name} меняет базу, не открывая tx() — значит мимо `_Writer` "
            "и мимо redact()"
        )
    assert len(checked) >= 5, f"путей записи нашлось всего {checked} — обход сузился"

    # Транзакция отдаёт обёртку, а не соединение: иначе обещание держалось бы
    # на внимательности автора каждого метода.
    key = "sk-or-v1-" + "e" * 64
    with contextlib.closing(Store(store_module.MEMORY).init()) as store, \
            patch.dict(os.environ, {"OPENROUTER_API_KEY": key}):
        with store.tx() as conn:
            assert not isinstance(conn, sqlite3.Connection), (
                "tx() отдаёт голое соединение — redact() перестал быть по построению"
            )
            # Все три формы параметров, какие принимает обёртка.
            conn.execute(
                "INSERT INTO sessions (id, label, config, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0)",
                (f"ag_{key}", key, key),
            )
            conn.execute("INSERT INTO meta (key, value) VALUES (:k, :v)", {"k": "т", "v": key})
            conn.executemany(
                "INSERT INTO messages (session_id, seq, role, content, at) VALUES (?, ?, ?, ?, 0)",
                [("s", 0, "user", key), ("s", 1, "assistant", key)],
            )
            conn.executemany(
                "INSERT INTO summaries (session_id, seq, upto, content, at) "
                "VALUES (?, ?, ?, ?, 0)",
                [("s", 0, 2, key)],
            )
            conn.executemany(
                "INSERT INTO working_memory (session_id, kind, content, at) "
                "VALUES (?, 'goal', ?, 0)",
                [("s", key)],
            )
            conn.executemany(
                "INSERT INTO branches (session_id, parent_id, forked_at, at) "
                "VALUES (?, ?, 2, 0)",
                [("s", key)],
            )
            conn.execute(
                "INSERT INTO memory (kind, content, at) VALUES ('knowledge', ?, 0)",
                (key,),
            )
            conn.execute(
                "INSERT INTO profile (field, content, at) VALUES ('style', ?, 0)",
                (key,),
            )
            conn.execute(
                "INSERT INTO invariants (kind, content, banned, at) "
                "VALUES ('stack', ?, ?, 0)",
                (key, key),
            )
        leaked = _columns_holding(store.conn, key)
        assert not leaked, f"ключ уехал в базу через tx(): {leaked}"
    return f"{len(checked)} путей записи выведено из класса, все идут через tx()"




# --- день 16: MCP — соединение и список инструментов -------------------------
#
# Проверки поднимают настоящий subprocess через тот же McpManager, что носит
# приложение, с явно внедрённым фикстурным конфигом. Заглушки вместо сервера здесь не
# бывает: соединение проверяется соединением.


@contextlib.contextmanager
def _mcp_env(config_path: str, **extra):
    """Explicit fixture config, with optional runtime auth keys."""
    with patch.object(mcp_module, "DEFAULT_CONFIG_PATH", Path(config_path)), patch.dict(os.environ, extra):
        yield


def _mcp_config(tmp: str, servers: dict) -> str:
    path = os.path.join(tmp, "mcp.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"servers": servers}, fh)
    return path


ECHO = {"module": "app.mcp_servers.echo", "timeout_s": 10}
PROBE = {"module": "checks._mcp_probe"}


@check("MCP: соединение устанавливается, список инструментов доезжает целиком")
def check_mcp_connects():
    """Настоящий процесс, настоящее рукопожатие: echo поднимается, `ping`
    приезжает с описанием и схемой и отвечает на вызов. Захардкоженный
    список здесь не прожил бы: описание и схему отдаёт живой сервер,
    и утверждения бьют по ним."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        cfg = _mcp_config(tmp, {"echo": ECHO})

        async def scenario():
            with _mcp_env(cfg):
                manager = McpManager()
                await manager.start()
                try:
                    assert len(manager.servers) == 1, manager.servers
                    server = manager.servers[0]
                    assert server.status == "ok", server.error
                    tools = {tool["name"]: tool for tool in server.view}
                    assert "ping" in tools, f"ping нет в списке: {list(tools)}"
                    assert tools["ping"]["description"], "у ping нет описания"
                    props = tools["ping"]["schema"].get("properties", {})
                    assert "text" in props, f"в схеме ping нет text: {props}"
                    result = await manager.call("ping", {"text": "связь"})
                    assert result.content[0].text == "pong связь", result.content
                finally:
                    await manager.stop()

        asyncio.run(scenario())
        # И через ручку: приложение с таким конфигом отдаёт тот же список.
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                answer = client.get("/api/mcp")
                assert answer.status_code == 200, answer.text
                servers = answer.json()["servers"]
                assert [s["name"] for s in servers] == ["echo"], servers
                assert servers[0]["status"] == "ok", servers
                names = [t["name"] for t in servers[0]["tools"]]
                assert names == ["ping"], names
        return "echo поднялся процессом, ping приехал с описанием и схемой и ответил"


@check("MCP: коллизия имён двух серверов превращает оба в «сервер__инструмент»")
def check_mcp_name_collision():
    """Два сервера несут один инструмент `ping`: голого имени в реестре
    не остаётся, оба получают префикс — иначе второй молча затёр бы
    первого, и вызов ушёл бы не туда."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        cfg = _mcp_config(tmp, {"one": ECHO, "two": ECHO})

        async def scenario():
            with _mcp_env(cfg):
                manager = McpManager()
                await manager.start()
                try:
                    assert sorted(manager.tools) == ["one__ping", "two__ping"], (
                        f"реестр: {sorted(manager.tools)}"
                    )
                    for server in manager.servers:
                        assert server.status == "ok", (server.name, server.error)
                        assert [t["name"] for t in server.view] == [f"{server.name}__ping"], (
                            server.view
                        )
                    result = await manager.call("two__ping", {"text": "второй"})
                    assert result.content[0].text == "pong второй", result.content
                finally:
                    await manager.stop()

        asyncio.run(scenario())
        return "голого ping нет, оба с префиксом, вызов по префиксу работает"


@check("MCP: stop() гасит процессы — terminate, и только непослушным kill")
def check_mcp_stop_kills_processes():
    """После stop() exitcode не None. И это не kill: terminate снимает
    процесс сигналом, kill остался бы -9 — так мутация «не terminate»
    не спрятана за запасным kill."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        cfg = _mcp_config(tmp, {"echo": ECHO})

        async def scenario():
            with _mcp_env(cfg):
                manager = McpManager()
                await manager.start()
                process = manager.servers[0].process
                assert process is not None and process.returncode is None
                await manager.stop()
                assert process.returncode is not None, "процесс пережил stop()"
                assert process.returncode != -9, (
                    f"процесс убит kill'ом ({process.returncode}): terminate не сработал"
                )

        asyncio.run(scenario())
        return "после stop() exitcode не None, и это SIGTERM, а не SIGKILL"


@check("MCP: сервер, упавший на initialize, — down, приложение стартует дальше")
def check_mcp_down_server_starts_anyway():
    """Зонд отвечает на initialize ошибкой протокола. Менеджер обязан
    пометить его down и доехать до конца старта: соседний сервер при
    этом ok, а в реестре его инструменты."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        cfg = _mcp_config(tmp, {"probe": PROBE, "echo": ECHO})

        async def scenario():
            with _mcp_env(cfg):
                manager = McpManager()
                await manager.start()  # упасть целиком — значит не пройти
                try:
                    by_name = {s.name: s for s in manager.servers}
                    assert by_name["probe"].status == "down", by_name["probe"].status
                    assert by_name["probe"].error, "у down-сервера названа причина"
                    assert by_name["echo"].status == "ok", by_name["echo"].error
                    assert list(manager.tools) == ["ping"], sorted(manager.tools)
                finally:
                    await manager.stop()

        asyncio.run(scenario())
        return "зонд down с причиной, echo ok, старт доехал до конца"


@check("MCP: env дочернего процесса — белый список, ключа OpenRouter в нём нет")
def check_mcp_child_env_whitelist():
    """Зонд отвечает на initialize ошибкой со списком своего окружения.
    В родителе выставлен фиктивный OPENROUTER_API_KEY: наследуй менеджер
    os.environ целиком — ключ оказался бы в списке."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        cfg = _mcp_config(tmp, {"probe": PROBE})

        async def scenario():
            with _mcp_env(cfg, OPENROUTER_API_KEY="sk-фиктивный-ключ-проверки"):
                manager = McpManager()
                await manager.start()
                try:
                    server = manager.servers[0]
                    assert server.status == "down", server.status
                    return server.error
                finally:
                    await manager.stop()

        error = asyncio.run(scenario())
    assert "env: " in error, f"зонд не назвал своё окружение: {error}"
    keys = json.loads(error.split("env: ", 1)[1])
    assert "OPENROUTER_API_KEY" not in keys, f"ключ уехал в subprocess: {keys}"
    assert "PATH" in keys, f"даже PATH не доехал — процесс не поднялся бы: {keys}"
    return f"в окружении subprocess'а только белый список: {keys}"


@check("MCP: два сервера — остановка приложения чистая: стеки закрываются LIFO")
def check_mcp_stop_two_servers_lifo():
    """У каждого сервера свой AsyncExitStack, а входили они в скоупы anyio
    по очереди и в одну задачу. Закрытие в прямом порядке ломало стек
    скоупов молча (под suppress в stop) — и падал уже портал TestClient на
    своём выходе, далеко от причины. Проверка — сам выход из TestClient:
    он и есть утверждение, и держится оно на двух живых серверах."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        cfg = _mcp_config(tmp, {"echo": ECHO, "git": GIT})
        with _mcp_env(cfg):
            with TestClient(main.app) as client:  # выход без исключения — проверяемое
                answer = client.get("/api/mcp")
                assert answer.status_code == 200, answer.text
                servers = {s["name"]: s for s in answer.json()["servers"]}
                assert servers["echo"]["status"] == "ok", servers
                assert servers["git"]["status"] == "ok", servers
    return "два живых сервера, выход из приложения без исключения"


@check("MCP: без конфига менеджер пуст, и ручка отдаёт пустой список")
def check_mcp_empty_without_config():
    """Нет файла — ни процессов, ни инструментов, ни ошибки: приложение
    работает в точности как в день 15. Явное отключение fixture менеджера также безопасно."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-mcp-") as tmp:
        missing = os.path.join(tmp, "нет-такого-файла.json")

        async def scenario():
            with _mcp_env(missing):
                manager = McpManager()
                await manager.start()
                assert manager.servers == [] and manager.tools == {}
                await manager.stop()
            with _mcp_env(missing):
                manager = McpManager(disabled=True)
                await manager.start()
                assert manager.servers == [] and manager.tools == {}

        asyncio.run(scenario())
        with _mcp_env(missing):
            with TestClient(main.app) as client:
                answer = client.get("/api/mcp")
                assert answer.status_code == 200, answer.text
                assert answer.json()["servers"] == [], answer.json()
        return "ни процессов, ни инструментов, ручка отдаёт пустой список"


# --- День 18: напоминания — инструмент с отложенным выполнением --------------
#
# `due_at` только разрешает claim; fired увеличивается после фактического
# выполнения. Хранение — своя временная база сервера, не стор приложения.
# Отдельная сквозная проверка запускает reminders и Git по HTTP.

def _remind_fixture_config(tmp: str, extra: dict | None = None) -> str:
    """Test-only cwd/module adapter; production manager still filters child env.

    The actual server receives its temporary database inside the child before
    initialize, without passing arbitrary parent environment variables.
    """
    directory = Path(tmp)
    for package in ("app", "checks", "services"):
        (directory / package).symlink_to(Path(ROOT) / package, target_is_directory=True)
    (directory / "_isolated_remind.py").write_text(
        "from pathlib import Path\nfrom services.reminders import server as srv\n"
        + "srv.DATABASE = Path(" + repr(str(directory / "reminders.db")) + ")\n"
        + "srv.server.run()\n",
        encoding="utf-8",
    )
    return _mcp_config(tmp, {"remind": {"module": "_isolated_remind", "timeout_s": 10}, **(extra or {})})


@check("напоминание: срок разрешает claim, fired только после реального finish")
def check_remind_one_shot_fires():
    import tempfile
    from services.reminders import server as srv
    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        path = Path(tmp) / "r.db"
        rid = srv.add_reminder("позвонить", 10, path=path, now=100, context_id="host/chat")
        assert not srv.claim_reminder(rid, "a", "host/chat", path=path, now=109.999)
        assert srv.list_reminders(path=path, now=110)["items"][0]["fired"] == 0
        assert srv.claim_reminder(rid, "a", "host/chat", path=path, now=110)
        assert not srv.claim_reminder(rid, "b", "host/chat", path=path, now=110)
        assert not srv.finish_reminder(rid, "b", path=path, now=111)
        assert srv.finish_reminder(rid, "a", path=path, now=111)
        item = srv.list_reminders(path=path, now=300)["items"][0]
        assert item["fired"] == 1 and item["state"] == "сработало", item
        with patch.object(srv, "DATABASE", path):
            assert srv.remind("   ", 5).startswith("пустой текст")
            assert "прошлом" in srv.remind("текст", -1)
            assert "больше нуля" in srv.remind("текст", 5, every=0)
            assert "больше нуля" in srv.remind("текст", 5, every=float("nan"))
    return "до срока claim запрещён, один владелец, чужой finish не принят; fired после результата"


@check("повтор: фактические исполнения и фиксированный срок с пропуском опозданий")
def check_remind_recurring():
    import tempfile
    from services.reminders import server as srv
    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        path = Path(tmp) / "r.db"
        rid = srv.add_reminder("встать", 10, every=3, path=path, now=100, context_id="host/chat")
        assert srv.claim_reminder(rid, "one", "host/chat", path=path, now=110)
        assert srv.finish_reminder(rid, "one", path=path, now=119.25)
        item = srv.list_reminders(path=path, now=119.25)["items"][0]
        assert item["fired"] == 1 and item["due_at"] == 122 and item["status"] == "pending", item
        assert not srv.claim_reminder(rid, "two", "host/chat", path=path, now=121.999)
        assert srv.claim_reminder(rid, "two", "host/chat", path=path, now=122)
        assert srv.finish_reminder(rid, "two", path=path, now=122.5)
        item = srv.list_reminders(path=path, now=200)["items"][0]
        assert item["fired"] == 2 and item["due_at"] == 125, item
    return "две реальные итерации; просроченные слоты пропущены, чтение не увеличивает fired"


@check("cancel: существующее снимается, чужой номер — честный отказ текстом")
def check_remind_cancel():
    """Сняли — записи нет, а её номер не выдаётся заново (AUTOINCREMENT,
    довод тот же, что у `memory`). По номеру, которого нет, — текстовый
    отказ, а не молчание и не «снято»."""
    import tempfile

    from services.reminders import server as srv

    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        with patch.object(srv, "DATABASE", Path(tmp) / "r.db"):
            assert srv.db_path() == Path(tmp) / "r.db"
            srv.remind("раз", 3600)
            srv.remind("два", 3600)
            gone = srv.cancel(1)
            missing = srv.cancel(999)
            snap = json.loads(srv.reminders())
            third = srv.remind("три", 3600)
    assert "снято" in gone, gone
    assert [i["text"] for i in snap["items"]] == ["два"], snap
    assert "№999 нет" in missing, missing
    assert json.loads(third)["id"] == 3, third
    return "снял одну из двух, чужой номер — «нет», следующий номер — 3"


@check("агрегат reminders(): счётчики по состояниям сходятся со списком")
def check_remind_aggregate():
    """Три записи в трёх положениях: ждёт, сработало одноразовое, сработало
    повторяющееся. Счётчики обязаны сойтись с пересчётом по списку — агрегат,
    который не считает, здесь красный."""
    import tempfile

    from services.reminders import server as srv

    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        with patch.object(srv, "DATABASE", Path(tmp) / "r.db"):
            assert srv.db_path() == Path(tmp) / "r.db"
            srv.remind("долгое", 3600)
            srv.remind("уже", 0)
            srv.remind("период", 0, every=3600)
            snap = json.loads(srv.reminders())
    states = [i["state"] for i in snap["items"]]
    assert states == ["не привязано к чату"] * 3, states
    assert snap["total"] == 3, snap
    assert snap["waiting"] == 0 and snap["unbound"] == 3, snap
    assert snap["fired"] == 0, snap
    recurring = snap["items"][2]
    assert recurring["fired"] == 0 and recurring["every"] == 3600, recurring
    return "три старых непривязанных записи сохранены; время не выдаётся за исполнение"


@check("напоминания переживают перезапуск процесса: хранение — SQLite самого сервера")
def check_remind_survives_restart():
    """Заводим напоминание через настоящий процесс сервера, гасим менеджер
    и поднимаем новый: список тот же. Хранили бы в памяти процесса — второй
    запуск принёс бы пусто. База при этом временная серверная SQLite,
    а не стор приложения: SCHEMA и таблица очистки не тронуты."""
    import sqlite3
    import tempfile
    import time

    marker = f"перезапуск-{time.time_ns()}"
    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        cfg = _remind_fixture_config(tmp)

        async def scenario():
            with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
                first = McpManager()
                await first.start()
                try:
                    made = await first.call("remind", {"text": marker, "in_seconds": 3600})
                    assert json.loads(made.content[0].text)["scheduled"], made.content
                finally:
                    await first.stop()
                database = Path(tmp) / "reminders.db"
                assert database.is_file(), database
                with sqlite3.connect(database) as conn:
                    rows = conn.execute("SELECT id, text FROM reminders").fetchall()
                assert rows == [(1, marker)], rows
                second = McpManager()
                await second.start()
                try:
                    raw = (await second.call("reminders", {})).content[0].text
                    listing = json.loads(raw)
                    # За собой чистим тем же инструментом, живым процессом.
                    for item in listing["items"]:
                        if item["text"] == marker:
                            await second.call("cancel", {"id": item["id"]})
                finally:
                    await second.stop()
                return listing

        listing = asyncio.run(scenario())
    ours = [i for i in listing["items"] if i["text"] == marker]
    assert len(ours) == 1, listing
    assert ours[0]["state"] == "не привязано к чату" and ours[0]["fired"] == 0, ours
    return "второй процесс сервера видит строку, заведённую первым"


@check("ручка /api/mcp: у remind-сервера поле reminders, у лежачего его нет — и ручка жива")
def check_mcp_reminders_field():
    """Менеджер дёргает инструмент `reminders` только у живого сервера,
    у которого он есть: лежачий — без поля и без вызова, живой echo без
    такого инструмента — тоже без поля. Дёргай он и лежачего — ручка
    висела бы на таймауте чужого процесса."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        cfg = _remind_fixture_config(tmp, {"echo": ECHO, "probe": PROBE})
        with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
            with TestClient(main.app) as client:
                answer = client.get("/api/mcp")
                assert answer.status_code == 200, answer.text
                servers = {s["name"]: s for s in answer.json()["servers"]}
                with patch.object(main.mcp.MANAGER, "reminder_protocol", side_effect=RuntimeError("aggregate fixture failed")) as calls:
                    failed = client.get("/api/mcp")
                assert failed.status_code == 200, failed.text
                assert calls.call_count == 1, calls.call_count
                assert all("reminders" not in row for row in failed.json()["servers"]), failed.json()
    assert servers["remind"]["status"] == "ok", servers["remind"]
    data = servers["remind"]["reminders"]
    assert data["total"] == len(data["items"]), data
    assert data["waiting"] + data["fired"] == data["total"], data
    assert servers["echo"]["status"] == "ok", servers["echo"]
    assert "reminders" not in servers["echo"], servers["echo"]
    assert servers["probe"]["status"] == "down", servers["probe"]
    assert "reminders" not in servers["probe"], servers["probe"]
    return "поле есть только у живого remind, лежачий не дёргается, ручка отвечает"


@check("сквозной: заглушка зовёт remind — вызов доезжает до настоящего сервера")
def check_remind_end_to_end():
    """Настоящий stdio-сервис сохраняет задачу, агент отвечает подтверждением
    без второго вызова модели; срок далеко, отложенного результата ещё нет."""
    import sqlite3
    import tempfile
    import time

    marker = f"сквозное-{time.time_ns()}"
    args = json.dumps({"text": marker, "in_seconds": 3600})
    with tempfile.TemporaryDirectory(prefix="check-remind-") as tmp:
        cfg = _remind_fixture_config(tmp)
        _stub.install(reply=_tool_reply, tool_calls=_first_call_only("remind", args))
        with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(
                    f"/api/agents/{chat}/messages", json={"text": "напомни сейчас"}
                )
                assert answer.status_code == 200, answer.text
                frames = sse(answer.text)
                body = client.get(f"/api/agents/{chat}").json()
                servers = client.get("/api/mcp").json()["servers"]
                listing = servers[0]["reminders"]

        # За собой чистим тем же инструментом, живым процессом.
        async def cleanup():
            with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
                manager = McpManager()
                await manager.start()
                try:
                    for item in listing["items"]:
                        if item["text"] == marker:
                            await manager.call("cancel", {"id": item["id"]})
                finally:
                    await manager.stop()

        asyncio.run(cleanup())

    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    tool = json.loads(_frame(frames, "tool_call")["result"])
    assert tool["scheduled"] and marker in tool["message"], tool
    badge = _frame(frames, "tool_call")
    assert badge["name"] == "remind" and badge["server"] == "remind", badge
    assert badge["ok"] is True, badge
    assert body["transcript"][-1]["metrics"]["tool_calls"][0]["name"] == "remind", body
    assert [f["event"] for f in frames].index("tool_call") < [f["event"] for f in frames].index("done"), frames
    assert body["transcript"][-1]["content"] == tool["message"], (
        body["transcript"][-1]["content"]
    )
    # Заведённое видно через ручку, а история осталась попарной.
    ours = [i for i in listing["items"] if i["text"] == marker]
    assert len(ours) == 1 and ours[0]["state"] == "ждёт" and ours[0]["fired"] == 0, ours
    conn = sqlite3.connect(REGISTRY.store.path)
    roles = conn.execute(
        "SELECT role FROM messages WHERE session_id = ? ORDER BY seq", (chat,)
    ).fetchall()
    conn.close()
    assert roles == [("user",), ("assistant",)], roles
    return "вызов исполнен процессом, напоминание заведено и видно в ручке, история попарная"


# --- День 19: пайплайн из трёх инструментов ----------------------------------
#
# search → summarize → save_file. Цепочку ведёт модель циклом вызовов дня 17:
# автоматики, передающей результат одного инструмента в другой мимо модели,
# нет. Механика проверяется in-process (декоратор FastMCP возвращает функцию
# как есть), связь и сквозная цепочка — настоящим процессом через тот же
# McpManager.

def _pipeline_fixture(tmp: str, extra: dict | None = None) -> tuple[Path, str]:
    """Prepare known Git inputs and a child-local adapter, without assertions."""
    directory = Path(tmp)
    repo = directory / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("/ignored.txt\n", encoding="utf-8")
    (repo / "a.txt").write_text("\nneedle alpha2\n" + "\n" * 7 + "Needle alpha10\n", encoding="utf-8")
    (repo / "b.txt").write_text("needle beta1\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "a.txt", "b.txt"], cwd=repo, check=True)
    (repo / "z.txt").write_text("NEEDLE untracked\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("needle ignored\n", encoding="utf-8")
    for package in ("app", "checks", "services"):
        (directory / package).symlink_to(Path(ROOT) / package, target_is_directory=True)
    (directory / "_isolated_pipeline.py").write_text(
        "from pathlib import Path\nfrom app.mcp_servers import pipeline as srv\n"
        + "srv.OUTPUT = Path(" + repr(str(directory / "output")) + ")\n"
        + "srv.ROOT = Path(" + repr(str(repo)) + ")\n"
        + "srv.server.run()\n",
        encoding="utf-8",
    )
    cfg = _mcp_config(tmp, {"pipeline": {"module": "_isolated_pipeline", "timeout_s": 15}, **(extra or {})})
    return repo, cfg


@check("search: выдача детерминирована — сортировка по (файл, строка), потом top-N")
def check_pipeline_search_deterministic():
    import tempfile
    from app.mcp_servers import pipeline as srv

    expected = "a.txt:2:needle alpha2\na.txt:10:Needle alpha10\nb.txt:1:needle beta1\nz.txt:1:NEEDLE untracked"
    with tempfile.TemporaryDirectory(prefix="check-pipeline-") as tmp:
        repo, _ = _pipeline_fixture(tmp)
        with patch.object(srv, "ROOT", repo):
            first = srv.search("needle", 10)
            second = srv.search("needle", 10)
            top = srv.search("needle", 3)
    assert first == second == expected, (first, second)
    assert top == "a.txt:2:needle alpha2\na.txt:10:Needle alpha10\nb.txt:1:needle beta1", top

    shuffled = ["b.py:5: x", "a.py:9: x", "a.py:2: x", "b.py:1: x"]
    with patch.object(srv, "_grep", lambda query: shuffled):
        arranged = srv.search("x", 3)
    assert arranged.splitlines() == ["a.py:2: x", "a.py:9: x", "b.py:1: x"], arranged
    return "temp Git: tracked/untracked, ignored исключён, numeric 2<10, repeat/top-N/shuffled"


@check("search: пустая выдача — честное «ничего не найдено», а не выдумка")
def check_pipeline_search_empty():
    import tempfile
    from app.mcp_servers import pipeline as srv

    with tempfile.TemporaryDirectory(prefix="check-pipeline-") as tmp:
        repo, _ = _pipeline_fixture(tmp)
        with patch.object(srv, "ROOT", repo):
            answer = srv.search("absent-marker")
        with patch.object(srv, "ROOT", Path(tmp)):
            failed = srv.search("needle")
        with patch.object(srv.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 15)):
            timeout = srv.search("needle")
    assert answer == "по запросу «absent-marker» ничего не найдено", answer
    assert srv.search("   ") == "пустой запрос: искать нечего"
    assert "больше нуля" in srv.search("раз", 0)
    assert failed.startswith("ошибка:") and "not a git repository" in failed, failed
    assert timeout == "ошибка: git grep не ответил за 15 с", timeout
    return "temp Git empty/invalid, настоящий non-Git отказ, timeout назван"


@check("summarize: фиксированный вход → эталонный выход (дедуп, группы, лимит)")
def check_pipeline_summarize_reference():
    """Механический конденсат, без LLM: дубли схлопнуты, строки сгруппированы
    по файлам в порядке первого появления, лишние группы отброшены — и это
    названо числом. Вход фиксирован, выход обязан совпасть побайтово."""
    from app.mcp_servers import pipeline as srv

    raw = "\n".join(
        [
            "b.py:5: два",
            "a.py:2: раз",
            "a.py:9: полтора",
            "b.py:5: два",  # дубль: схлопывается
            "строка без номера",
            "c.py:1: три",
        ]
    )
    out = srv.summarize(raw, max_items=2)
    assert out == (
        "b.py — 1 строка:\n"
        "  b.py:5: два\n"
        "\n"
        "a.py — 2 строки:\n"
        "  a.py:2: раз\n"
        "  a.py:9: полтора\n"
        "\n"
        "…ещё 2 файла не показано"
    ), out
    assert srv.summarize(raw, 4) == (
        "b.py — 1 строка:\n  b.py:5: два\n\n"
        "a.py — 2 строки:\n  a.py:2: раз\n  a.py:9: полтора\n\n"
        "(без файла) — 1 строка:\n  строка без номера\n\n"
        "c.py — 1 строка:\n  c.py:1: три"
    )
    assert srv.summarize("") == "пустой вход: конденсировать нечего"
    assert "больше нуля" in srv.summarize(raw, 0)
    return "независимый эталон: dedup/order/4 группы/unparsed/лимит 2/отказы"


@check("save_file: пишет в каталог явного OUTPUT, содержимое побайтово")
def check_pipeline_save_file_writes():
    """Каталог приходит из env (проверка уводит его во временный), ответ
    называет путь записанного, а байты на диске — в точности те, что просили."""
    import tempfile
    from pathlib import Path

    from app.mcp_servers import pipeline as srv

    content = "раз\r\nдва ⚙\n"
    with tempfile.TemporaryDirectory(prefix="check-pipeline-") as tmp:
        with patch.object(srv, "OUTPUT", Path(tmp)):
            answer = srv.save_file("итог.txt", content)
        written = (Path(tmp) / "итог.txt").read_bytes()
    assert written == content.encode("utf-8"), written
    assert answer == f"записано: {tmp}/итог.txt ({len(content.encode('utf-8'))} байт)", answer
    return "файл лежит в заданном каталоге, байты совпали, путь назван"


@check("save_file: имя с «../» или «/» — отказ текстом, и файла не появляется")
def check_pipeline_save_file_name():
    """Refusals happen before creating the output directory or any file."""
    import tempfile
    from pathlib import Path

    from app.mcp_servers import pipeline as srv

    with tempfile.TemporaryDirectory(prefix="check-pipeline-") as tmp:
        with patch.object(srv, "OUTPUT", Path(tmp) / "output"):
            for bad in ("../escape.txt", "sub/dir.txt", "a\\b.txt", "..", "  ", "", " name", "name ", "a..b"):
                answer = srv.save_file(bad, "x")
                assert answer.startswith("отказано:"), (bad, answer)
        assert list(Path(tmp).iterdir()) == [], list(Path(tmp).iterdir())
    return "9 отказов: traversal/разделители/пустое/краевые пробелы/embedded .., ни записи"


@check("pipeline через MANAGER: настоящий процесс, list/search по временному Git")
def check_pipeline_end_to_end():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-pipeline-") as tmp:
        _, cfg = _pipeline_fixture(tmp)

        async def scenario():
            with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
                manager = McpManager()
                await manager.start()
                try:
                    server = manager.servers[0]
                    assert server.status == "ok", server.error
                    tools = {t["name"]: t for t in server.view}
                    assert sorted(tools) == ["save_file", "search", "summarize"], sorted(tools)
                    for tool in tools.values():
                        assert tool["description"], tool
                        assert tool["schema"].get("properties"), tool
                    result = await manager.call(
                        "search", {"query": "needle", "limit": 5}
                    )
                    return result.content[0].text
                finally:
                    await manager.stop()

        found = asyncio.run(scenario())
    assert found == "a.txt:2:needle alpha2\na.txt:10:Needle alpha10\nb.txt:1:needle beta1\nz.txt:1:NEEDLE untracked", found
    return "child-local ROOT: initialize/list/3 schema+description, реальные 4 Git строки"


@check("сквозная: цепочка search → summarize → save_file, данные текут по tool-сообщениям")
def check_pipeline_chain():
    """Сквозной сценарий дня. Заглушка играет модель, ведущую цепочку: зовёт
    search, затем summarize с его результатом — прочитанным из tool-сообщения,
    как прочитала бы его модель, — затем save_file с выводом summarize.
    Передача данных проверяется там же, где её увидела бы модель: в
    CALLS[n].payload["messages"] аргумент summarize содержит результат search,
    аргумент save_file — вывод summarize. В продукте автоматики нет: каждый
    инструмент видит только то, что положила модель."""
    import tempfile
    import sqlite3
    query, name = "needle", "цепочка.txt"
    expected_search = "a.txt:2:needle alpha2\na.txt:10:Needle alpha10\nb.txt:1:needle beta1\nz.txt:1:NEEDLE untracked"
    expected_summary = (
        "a.txt — 2 строки:\n  a.txt:2:needle alpha2\n  a.txt:10:Needle alpha10\n\n"
        "b.txt — 1 строка:\n  b.txt:1:needle beta1\n\n"
        "z.txt — 1 строка:\n  z.txt:1:NEEDLE untracked"
    )

    def last_tool(messages):
        tool = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        return tool["content"] if tool else ""

    plan = ["search", "summarize", "save_file"]

    def chain_calls(messages, index):
        """Модель ведёт цепочку: аргумент следующего вызова — результат
        предыдущего, взятый из последнего tool-сообщения."""
        if index >= len(plan):
            return None
        if index == 0:
            args = {"query": query, "limit": 5}
        elif index == 1:
            args = {"text": last_tool(messages), "max_items": 3}
        else:
            args = {"name": name, "content": last_tool(messages)}
        return [
            {
                "id": f"call_{index + 1}",
                "name": plan[index],
                "arguments": json.dumps(args, ensure_ascii=False),
            }
        ]

    def chain_reply(messages, index):
        """Пока цепочка идёт — молчание; в конце — пересказ подтверждения
        из последнего tool-сообщения."""
        if index < len(plan):
            return ""
        return "сохранил: " + last_tool(messages)

    with tempfile.TemporaryDirectory(prefix="check-pipeline-") as tmp:
        _, cfg = _pipeline_fixture(tmp)
        target = Path(tmp) / "output" / name
        _stub.install(reply=chain_reply, tool_calls=chain_calls)
        with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(
                    f"/api/agents/{chat}/messages",
                    json={"text": "найди и сохрани"},
                )
                assert answer.status_code == 200, answer.text
                frames = sse(answer.text)
                body = client.get(f"/api/agents/{chat}").json()
                written = target.read_bytes() if target.exists() else None
                assert [p.name for p in target.parent.iterdir()] == [name]

    # Четыре обращения к модели: три с просьбой о вызове и финальное словами.
    assert len(_stub.CALLS) == 4, len(_stub.CALLS)
    first, second, third, fourth = _stub.CALLS
    # Counts after the whole chain detect mutation of any earlier request.
    snapshots = [c["payload"]["messages"] for c in _stub.CALLS]
    assert [sum(m["role"] == "tool" for m in ms) for ms in snapshots] == [0, 1, 2, 3], snapshots
    assert [sum(bool(m.get("tool_calls")) for m in ms) for ms in snapshots] == [0, 1, 2, 3], snapshots
    assert [[m["tool_call_id"] for m in ms if m["role"] == "tool"] for ms in snapshots] == [
        [], ["call_1"], ["call_1", "call_2"], ["call_1", "call_2", "call_3"]
    ]
    declared = {t["function"]["name"] for t in first["payload"]["tools"]}
    assert declared == {"search", "summarize", "save_file"}, declared

    # search исполнен: его настоящий результат лежит в tool-сообщении.
    got_search = second["payload"]["messages"][-1]
    assert got_search["role"] == "tool" and got_search["tool_call_id"] == "call_1", got_search
    assert got_search["content"] == expected_search, got_search["content"]

    # summarize позван с результатом search — читаем аргумент из запроса.
    asked2 = third["payload"]["messages"][-2]
    arg2 = json.loads(asked2["tool_calls"][0]["function"]["arguments"])
    assert arg2 == {"text": expected_search, "max_items": 3}, arg2
    got_summary = third["payload"]["messages"][-1]
    assert got_summary["role"] == "tool" and got_summary["tool_call_id"] == "call_2", got_summary
    assert got_summary["content"] == expected_summary, got_summary["content"]

    # save_file позван с выводом summarize, и файл лежал с ним побайтово.
    asked3 = fourth["payload"]["messages"][-2]
    arg3 = json.loads(asked3["tool_calls"][0]["function"]["arguments"])
    assert arg3 == {"name": name, "content": expected_summary}, arg3
    got_saved = fourth["payload"]["messages"][-1]
    assert got_saved == {"role": "tool", "tool_call_id": "call_3",
                         "content": f"записано: {target} ({len(expected_summary.encode('utf-8'))} байт)"}, got_saved
    assert written == expected_summary.encode("utf-8"), written

    # SSE: три бейджа подряд, до done, каждый со своим результатом.
    kinds = [f["event"] for f in frames]
    badges = [f for f in frames if f["event"] == "tool_call"]
    assert [b["name"] for b in badges] == plan, badges
    assert badges[0]["result"] == expected_search, badges[0]
    assert badges[1]["result"] == expected_summary, badges[1]
    assert badges[2]["result"] == got_saved["content"], badges[2]
    assert all(b["ok"] and b["server"] == "pipeline" for b in badges), badges
    assert max(i for i, kind in enumerate(kinds) if kind == "tool_call") < kinds.index("done"), kinds

    # Финальный ответ пересказывает подтверждение, история попарная, метрики
    # несут три записи вызовов в порядке цепочки.
    answer_turn = body["transcript"][-1]
    assert answer_turn["content"] == "сохранил: " + got_saved["content"], answer_turn["content"]
    assert [t["role"] for t in body["transcript"]] == ["user", "assistant"], body["transcript"]
    runs = answer_turn["metrics"]["tool_calls"]
    assert [r["name"] for r in runs] == plan, runs
    assert all(r["ok"] and r["server"] == "pipeline" for r in runs), runs
    conn = sqlite3.connect(REGISTRY.store.path)
    roles = conn.execute("SELECT role FROM messages WHERE session_id=? ORDER BY seq", (chat,)).fetchall()
    conn.close()
    assert roles == [("user",), ("assistant",)], roles
    return (
        "три вызова доехали процессом, аргумент summarize — результат search, "
        "аргумент save_file — вывод summarize, temp bytes, SQL pair, 4 неизменных payload"
    )


# --- День 20: оркестрация — несколько серверов, длинный флоу -----------------
#
# Независимые сервисы подключаются по URL; эти сценарии сохраняют явные stdio
# фикстуры. Реестр разруливает имена и коллизии, цикл дня 17 исполняет вызовы.
# День проверяет маршрутизацию (каждый вызов уезжает в свой сервер, порядок
# сохранён), полноту объявленных инструментов и изоляцию упавшего.


@check("оркестрация: один обмен — вызовы двух разных серверов, каждый в свой, порядок сохранён")
def check_orch_two_servers_routing():
    """Заглушка просит два вызова одним кадром — git_log (git) и ping (echo),
    в порядке, обратном порядку серверов в конфиге. Каждый обязан уехать в свой
    сервер: иначе tool-сообщение лежало бы с чужим результатом или с ошибкой.
    Порядок обязан сохраниться: модель читает tool-сообщения позиционно, и
    перестановка молча разменяла бы результаты. Метрики обмена называют сервер
    каждого вызова."""
    import tempfile
    import time
    import sqlite3

    marker = f"маршрут-{time.time_ns()}"

    def route_calls(messages, index):
        if index:
            return None
        return [
            {"id": "call_1", "name": "git_log", "arguments": '{"n": 1}'},
            {"id": "call_2", "name": "ping", "arguments": json.dumps({"text": marker})},
        ]

    def route_reply(messages, index):
        if index == 0:
            return ""
        tools = [m["content"] for m in messages if m.get("role") == "tool"]
        return "итог: " + " | ".join(tools)

    with tempfile.TemporaryDirectory(prefix="check-orch-") as tmp:
        cfg = _mcp_config(tmp, {"echo": ECHO, "git": GIT})
        _stub.install(reply=route_reply, tool_calls=route_calls)
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(
                    f"/api/agents/{chat}/messages", json={"text": "журнал и связь"}
                )
                assert answer.status_code == 200, answer.text
                frames = sse(answer.text)
                body = client.get(f"/api/agents/{chat}").json()

    # Два обращения к модели: с просьбой о вызовах и с их результатами.
    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    head = _git_head_line()
    sent = _stub.CALLS[1]["payload"]["messages"]
    roles = [m["role"] for m in sent]
    assert roles[-3:] == ["assistant", "tool", "tool"], roles
    asked = sent[-3]
    assert [c["function"]["name"] for c in asked["tool_calls"]] == ["git_log", "ping"], asked
    assert [c["id"] for c in asked["tool_calls"]] == ["call_1", "call_2"], asked

    # Каждый вызов уехал в свой сервер: результаты не перепутать — одно
    # сообщение несёт настоящий коммит, второе отвечает эхом.
    first, second = sent[-2], sent[-1]
    assert first["tool_call_id"] == "call_1" and first["content"] == head, first
    assert second["tool_call_id"] == "call_2" and second["content"] == f"pong {marker}", second

    # SSE: два бейджа до done, в порядке просьбы, каждый со своим сервером.
    kinds = [f["event"] for f in frames]
    badges = [f for f in frames if f["event"] == "tool_call"]
    assert [b["name"] for b in badges] == ["git_log", "ping"], badges
    assert [b["server"] for b in badges] == ["git", "echo"], badges
    assert badges[0]["result"] == head and badges[1]["result"] == f"pong {marker}", badges
    assert all(b["ok"] for b in badges), badges
    assert max(i for i, event in enumerate(kinds) if event == "tool_call") < kinds.index("done"), kinds

    # Финальный ответ собран из обоих результатов, история попарная, метрики
    # называют сервер каждого вызова в порядке исполнения.
    answer_turn = body["transcript"][-1]
    assert answer_turn["content"] == f"итог: {head} | pong {marker}", answer_turn["content"]
    assert [t["role"] for t in body["transcript"]] == ["user", "assistant"], body["transcript"]
    runs = answer_turn["metrics"]["tool_calls"]
    assert [(r["name"], r["server"]) for r in runs] == [("git_log", "git"), ("ping", "echo")], runs
    assert all(r["ok"] for r in runs), runs
    conn = sqlite3.connect(REGISTRY.store.path)
    persisted = conn.execute("SELECT role FROM messages WHERE session_id=? ORDER BY seq", (chat,)).fetchall()
    conn.close()
    assert persisted == [("user",), ("assistant",)], persisted
    return "два сервера, два вызова одним кадром: каждый в свой, порядок сохранён"


@check("оркестрация: в payload объявлены инструменты всех живых серверов, полный список")
def check_orch_full_tool_list():
    """Три сервера (echo, git, pipeline) — модель выбирает из полного списка,
    и обрезанный список сделал бы часть инструментов для неё невидимыми.
    Эталон не записан здесь: список берётся из /api/mcp — того, что сами
    серверы отдали, — и сверяется с телом запроса из CALLS, описание и схема
    у каждого на месте."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-orch-") as tmp:
        _, cfg = _pipeline_fixture(tmp, {"echo": ECHO, "git": GIT})
        _stub.install(reply="ок")
        with patch.object(mcp_module, "ROOT", Path(tmp)), _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
                assert answer.status_code == 200, answer.text
                assert _frame(sse(answer.text), "done")["committed"] is True, answer.text
                servers = client.get("/api/mcp").json()["servers"]

    assert len(servers) == 3 and all(s["status"] == "ok" for s in servers), servers
    listed = {t["name"]: t for s in servers for t in s["tools"]}
    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    declared = {t["function"]["name"]: t["function"] for t in _stub.CALLS[0]["payload"]["tools"]}
    assert sorted(declared) == sorted(listed), (sorted(declared), sorted(listed))
    for name, tool in declared.items():
        assert tool["description"], f"{name}: нет описания"
        assert "properties" in tool["parameters"], f"{name}: нет схемы"
        assert tool["description"] == listed[name]["description"], (name, tool, listed[name])
        assert tool["parameters"] == listed[name]["schema"], (name, tool, listed[name])
    return f"все {len(listed)} инструментов трёх серверов в теле запроса, с описанием и схемой"


@check("оркестрация: упавший на старте сервер изолирован — обмен идёт, его инструменты не объявлены")
def check_orch_down_server_isolated():
    """Зонд падает на initialize, echo и git живы. Обмен обязан пройти, в
    объявленных tools — только инструменты живых серверов, а в ручке /api/mcp
    зонд виден со статусом down: иначе человек не узнал бы, что часть
    инструментов молча пропала. Уровень менеджера стережёт своя проверка
    (день 16), здесь — сквозная: до тела запроса и до ручки."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="check-orch-") as tmp:
        cfg = _mcp_config(tmp, {"probe": PROBE, "echo": ECHO, "git": GIT})
        _stub.install(reply="ок")
        with _mcp_env(cfg):
            with TestClient(main.app) as client:
                chat = new_agent(client)
                answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
                assert answer.status_code == 200, answer.text
                assert _frame(sse(answer.text), "done")["committed"] is True, answer.text
                assert client.get(f"/api/agents/{chat}").json()["transcript"][-1]["content"] == "ок"
                servers = {s["name"]: s for s in client.get("/api/mcp").json()["servers"]}

    assert servers["probe"]["status"] == "down", servers["probe"]
    assert servers["probe"]["tools"] == [], servers["probe"]
    assert servers["echo"]["status"] == "ok", servers["echo"]
    assert servers["git"]["status"] == "ok", servers["git"]
    live = sorted(t["name"] for s in servers.values() if s["status"] == "ok" for t in s["tools"])
    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    declared = sorted(t["function"]["name"] for t in _stub.CALLS[0]["payload"]["tools"])
    assert declared == live, (declared, live)
    return "зонд down и виден в ручке, обмен прошёл, объявлены только инструменты живых"


@check("scheduler: standalone HTTP, сроки, повтор, отмена, рестарт и изоляция чата")
def check_reminder_scheduler():
    result = subprocess.run([sys.executable, os.path.join(ROOT, "checks", "reminder_scheduler.py")],
                            capture_output=True, text=True, cwd=ROOT, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()
@check("MCP URL: independent service, persisted config and explicit reconnect")
def check_url_api():
    from checks.mcp_url import check_url_api
    return check_url_api()


@check("MCP URL: foreground call/config and multi-round session races")
def check_url_race():
    from checks.mcp_url import check_url_race
    return check_url_race()


@check("request JSON: final outbound overrides survive config edits and restart")
def check_request_capture():
    from checks.mcp_url import check_request_capture
    return check_request_capture()


@check("Git HTTP: real tool rounds, exact request logs and whole-exchange lease")
def check_git_exchange():
    from checks.tool_url import check_git_exchange
    return check_git_exchange()


@check("Pipeline HTTP: operator paths, exact four-round JSON and persistent client info")
def check_pipeline_http():
    from checks.pipeline_url import check_pipeline_http
    return check_pipeline_http()


@check("RAG chat: pinned retrieval, immutable answer snapshot and terminal lifecycle")
def check_rag_chat():
    from checks.rag_chat_check import check_rag_chat
    return check_rag_chat()


@check("RAG refinement: rewrite/filter lifecycle")
def check_rag_refinement():
    from checks.rag_refinement_check import check_rag_refinement
    return check_rag_refinement()


@check("RAG citations: provenance, exact quotes and weak-context refusal")
def check_rag_citations():
    from checks.rag_citations_check import check_rag_citations
    return check_rag_citations()


@check("RAG: actual semantic HTTP/CLI and save boundary")
def check_rag_workflow_http():
    from checks.workflow_http_check import check_workflow_http
    return check_workflow_http()


@check("RAG: generation model catalogue HTTP/auth and safe failures")
def check_rag_model_catalogue():
    from checks.rag_models_check import check_rag_models
    return check_rag_models()


@check("security: bounded chat errors and trusted-origin RAG credential actions")
def check_security_boundaries():
    from checks.security_check import check_security
    return check_security()


@check("runtime: fixed defaults, explicit isolation and two-key dotenv")
def check_runtime_configuration():
    from checks.runtime_check import check_runtime
    return check_runtime()


@check("RAG: full raw HTML preparation, cache, atomic failures and API/CLI")
def check_rag_preparation():
    from checks.preparation_check import check_preparation
    return check_preparation()


@check("RAG: semantic exact source boundaries and cache validation")
def check_rag_semantic():
    from checks.semantic_check import check_semantic
    return check_semantic()


@check("RAG: durable workflow/deletion/async API")
def check_rag_workflow():
    from checks.workflow_check import check_workflow
    return check_workflow()


@check("RAG: HTML/HTTP/CLI/SQLite/cache/atomic index and bounded inspector")
def check_rag_index():
    from checks.rag_check import check_rag
    return check_rag()


@check("клиент: экранирование, разбор markdown и панель проверены настоящими вызовами")
def check_browser():
    """Клиентский код исполняется под node: payload на входе, утверждения
    про выход. Греп по исходнику прошёл бы и если экранирование переедет
    **после** разбора — то есть самая опасная поверхность демо была бы
    прикрыта пустышкой."""
    node = shutil.which("node")
    assert node, (
        "нужен node, чтобы исполнить клиентский код: разбор markdown "
        "проверяется настоящими вызовами, а не чтением исходника"
    )
    result = subprocess.run(
        [node, os.path.join(ROOT, "checks", "browser_check.js")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@check("Independent reasoning preferences and actual provider controls")
def check_reasoning():
    from checks.reasoning_check import check_reasoning
    return check_reasoning()


@check("Shared model providers and full-permutation RAG rerank")
def shared_model_providers():
    from checks.models_check import check_models
    return check_models()


def main_() -> int:
    for fn in CHECKS:
        fn()

    width = max(len(name) for name, _, _ in RESULTS)
    failed = 0
    print()
    for name, ok, detail in RESULTS:
        print(f"{'OK  ' if ok else 'FAIL'}  {name.ljust(width)}  {detail}")
        failed += 0 if ok else 1
    print()
    print(f"{len(RESULTS) - failed} из {len(RESULTS)} проверок пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_())
