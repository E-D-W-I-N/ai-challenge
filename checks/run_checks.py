"""Ядро проверок — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Каждая проверка стережёт одно обещание продукта. Отдельные скрипты
(`spawn_100.py`, `restart.py`, `two_processes.py`) запускаются отсюда же.
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
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from checks import _stub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_stub.install_offline()

import app.agent as agent_module  # noqa: E402
import app.main as main  # noqa: E402
from app.llm import Metrics  # noqa: E402
from app.registry import REGISTRY  # noqa: E402
from app.schema import AgentSpec  # noqa: E402
from app.store import Store  # noqa: E402

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


def read(path: str) -> str:
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


KEEP, EVERY = 6, 10
"""Окно и порог, на которых проверяется сжатие. Порог выше самого длинного
чужого сценария: опусти его — поплывут счётчики вызовов в чужих проверках."""


def _service_kind(messages) -> str | None:
    """Чем был этот вызов: `summary` — сжатие, `None` — обычный обмен; у
    служебного свой системный промпт. Вопрос именно «какой», а не «сжатие
    ли»: второй ответ пришлось бы заводить при первом же новом вызове."""
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
    """Ответ, зависящий от **содержимого запроса**, а не от его номера: врезку
    заглушка читает там же, где прочитала бы её модель. Обещать можно только
    то, что ответ **разойдётся**. Читаются обе врезки."""
    knows = any(
        "пишу на Kotlin" in m.get("content", "")
        or "цель: пример на Kotlin" in m.get("content", "")
        for m in messages
    )
    return "Держи пример на Kotlin." if knows else "На каком языке показать пример?"


def _profile_aware(messages, index):
    """Ответ, зависящий от **профиля**, а не от номера запроса: заглушка
    читает его в блоке `[как отвечать]`. Довод тот же, что у `_memory_aware`."""
    head = messages[0].get("content", "") if messages else ""
    if "стиль: кратко, на ты" in head:
        return "Смотри: начни с макета."
    if "стиль: подробно, с примерами" in head:
        return "Давайте разберём по шагам, с примерами: начните с макета."
    return "С чего вам удобнее начать?"


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
# Помощники ниже **ничего не утверждают**: утверждение, уехавшее сюда, стало
# бы самопроверкой стенда. И сюда же не уезжает то, что проверка **измеряет**:
# число обменов, счётчики вызовов и сами сравнения остаются на виду в ней.
# Помощника на «подменить и вернуть» здесь нет намеренно — это делают
# `patch.object` и `patch.dict` из стандартной библиотеки.


def _frames(client, agent_id: str, text: str) -> list[dict]:
    """Обмен через веб-слой, разобранный в список кадров SSE."""
    return sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": text}).text)


def _frame(frames, event: str) -> dict:
    """Первый кадр названного события."""
    return next(e for e in frames if e["event"] == event)


def _talk(client, agent_id: str, turns, text: str = "вопрос {i}") -> list:
    """Обмены подряд через веб-слой; отдаёт ответы ручки. Сколько их —
    остаётся у проверки: это её счёт. Число значит «столько с нуля», готовый
    `range` — «как просили»: номер уезжает в текст вопроса."""
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
    Закрывается и при падении: незакрытое соединение держит WAL."""
    store = Store(path).init()
    try:
        yield store
    finally:
        store.close()


@contextlib.contextmanager
def _restarted(store, agent_id: str):
    """Перезапуск целиком: закрыть файл, открыть заново, поднять из него чат.
    Конфиг у поднятого пустой — всё, что он о себе знает, приехало из базы."""
    path = store.path
    store.close()
    with _reopened(path) as fresh:
        yield fresh, agent_module.Agent(
            AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=fresh
        )


ALL_TABLES = (
    "sessions", "messages", "meta", "summaries", "branches", "memory",
    "working_memory", "task_state", "task_steps", "profile",
)
"""Все таблицы схемы: перебор идёт по ним целиком и по всем колонкам каждой,
чтобы ключ искался и в колонках, которых ещё не придумали."""


def _columns_holding(conn, needle: str, tables=ALL_TABLES) -> list[str]:
    """Колонки, в которых лежит `needle`, — списком `таблица.колонка`."""
    found = set()
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table}"):
            for name in row.keys():
                if isinstance(row[name], str) and needle in row[name]:
                    found.add(f"{table}.{name}")
    return sorted(found)


def _body_rules(payload) -> list[str]:
    """Три правила тела запроса — списком нарушенных, словами: дословные копии
    расходятся молча, и третий путь уехал бы без правила. Утверждение остаётся
    в проверках, здесь только его текст."""
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
}
"""Чего после какого пути очистки не остаётся. Каждый `True` — место, где
`DELETE` обязан стоять руками: каскада нет, FK не объявлены.

`False` — не пробелы, а вторая половина таблицы, такая же обязательная:
родство переживает `forget()`, а долговременная память и профиль — ещё
и удаление чата, и стирает их один только `clear()`. Проверка,
потребовавшая бы чистки везде, сломала бы это так же, как забытый `DELETE`.
"""


def _filled_chat(store, label: str):
    """Чат, у которого непусты **все шесть** слоёв сразу; обменов три — порог
    сжатия набирается на третьем. План кладётся вызовом и **утверждается**:
    «после очистки плана нет» на чате без плана держалось бы само собой."""
    chat = agent_module.Agent(
        AgentSpec(label=label, model="stub/model", strategy="summary",
                  keep_last=2, compress_every=2, workflow="plan"),
        store=store,
    )
    _ask(chat, 3, label + " {i}")
    chat.add_working_record("goal", f"цель чата {label}")
    chat.run_tool("update_plan", json.dumps({"steps": [
        {"title": f"шаг чата {label}", "status": "in_progress"},
        {"title": "второй шаг", "status": "pending"},
    ]}, ensure_ascii=False))
    chat.approve_plan()
    store.save_branch(chat.id, parent_id="ag_00001", forked_at=2)
    store.add_memory("knowledge", f"запись рядом с чатом {label}")
    store.save_profile({"style": f"кратко, рядом с чатом {label}"})
    return chat


def _leftovers(store, session_id: str) -> dict:
    """Что осталось от чата в каждом слое; пустое значение — слой унесён.
    Состояние задачи читается **двумя** запросами: `load_plan` отдаёт `None`,
    едва исчезла строка `task_state`, и клетка стерегла бы половину слоя."""
    with store.reading() as conn:
        steps = conn.execute(
            "SELECT * FROM task_steps WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
    plan = store.load_plan(session_id)
    return {
        "summaries": store.load_summaries(session_id),
        "working": store.list_working(session_id),
        "task": ([plan] if plan else []) + [tuple(row) for row in steps],
        "branches": store.load_branch(session_id),
        "long_term": store.list_memory(),
        "profile": store.load_profile(),
    }


# --- День 6: агент как отдельная сущность, сто агентов в одном процессе -------


@check("спавн ста агентов с разными конфигами в одном процессе")
def check_spawn_100():
    return _script("spawn_100.py", "ОК: сто агентов", 1)


@check("конфиг агента копируется вглубь: сто агентов не делят один extra_body")
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

    # Консоль — вторая видимая поверхность, и единица на ней та же, что в
    # панели: сообщения, а не пары. Печатается в тот же `out`, что и ответ.
    cli._print_sessions(out=out)
    cli._print_agents(out=out)
    printed = out.getvalue()
    named = [line for line in printed.splitlines() if agent.id in line]
    assert len(named) == 2, f"агент назван не в двух списках, а в {len(named)}: {named}"
    for line in named:
        # Вопрос и ответ — два сообщения: «сообщений 1» значило бы, что
        # консоль считает пары, а «реплик 2» — что она зовёт ту же величину
        # другим словом, чем экран.
        assert "сообщений 2" in line, line
        assert "реплик" not in line, line
    return (
        "ответ напечатан, история записана, агент в реестре; "
        "консоль называет 2 сообщения в обоих списках"
    )


# --- История: помнится и уезжает в модель целиком ------------------------------


@check("обрезка бывает только выбранная и всегда названная: full, window, summary")
def check_cut_only_where_chosen():
    """Инвариант дня: обрезка бывает только выбранная и всегда названа —
    под ответом написано, сколько сообщений не уехало.

    По стратегии на раздел: **что уезжает** и **как названо то, что
    не уехало** (`summarized` у сводки, `dropped` у окна; слова клиента
    проверяет `browser_check.js`). Сама история при любой стратегии полная,
    иначе сломалась бы перегенерация.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")

    # --- full: не срезается ничего, и это умолчание ---------------------------
    # 1. Живой маршрут без стратегии вовсе: 25 обменов — больше прежнего окна.
    turns = 25
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИС")
        assert client.get(f"/api/agents/{agent_id}").json()["strategy"] == "full", "умолчание не full"
        for response in _talk(client, agent_id, turns):
            assert response.status_code == 200, response.text

    sent = _stub.CALLS[-1]["messages"]
    # Системный промпт + вся переписка (по две реплики на обмен) + новый вопрос.
    assert [m["role"] for m in sent] == (
        ["system"] + ["user", "assistant"] * (turns - 1) + ["user"]
    ), [m["role"] for m in sent]
    assert sent[1]["content"] == "вопрос 0", sent[1]
    assert sent[-1]["content"] == f"вопрос {turns - 1}", sent[-1]
    assert [m["content"] for m in sent[1:-1:2]] == [f"вопрос {i}" for i in range(turns - 1)]
    assert not _service_calls(), "без стратегии сжатие не запускается вовсе"

    agent = REGISTRY.require(agent_id)
    assert len(agent.history) == 2 * turns, len(agent.history)
    assert agent.summaries == [], agent.summaries
    # Нечего называть — и не названо: приписка под каждым ответом чата, где
    # ничего не срезано, была бы шумом.
    assert not any(k in (agent.history[-1].metrics or {}) for k in ("summarized", "dropped")), (
        agent.history[-1].metrics
    )

    # 2. Рост истории: 500 реплик — больше прежнего потолка хранимого.
    long_chat = _fill(_bare("длинный", system="СИС"), 500, role="user")
    assert len(long_chat.history) == 500, "история подрезана при росте"
    assert long_chat.history[0].content == "реплика 0", "у истории отъели начало"

    prompt = long_chat.build_prompt("последний вопрос")
    assert len(prompt) == 502, len(prompt)
    assert prompt[1]["content"] == "реплика 0", prompt[1]

    # 3. Числа заданы, а стратегия осталась `full` — не срезается **всё равно
    # ничего**. Так живут чаты, поднятые из конфига без ключа `strategy`:
    # срежь их окно молча — обрезку выбрал бы не пользователь, а сервер.
    _stub.reset()
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        numbers_only = new_agent(client, keep_last=KEEP, compress_every=EVERY)
        _talk(client, numbers_only, 9)
        assert not _service_calls(), "стратегия `full`, а сжатие запустилось"
        assert len(_stub.CALLS[-1]["messages"]) == 17, len(_stub.CALLS[-1]["messages"])
        assert _stub.CALLS[-1]["messages"][0]["content"] == "вопрос 0", _stub.CALLS[-1]["messages"][0]

    # --- window: уезжает хвост, отброшенное названо числом --------------------
    _stub.reset()
    with TestClient(main.app) as client:
        win_id = new_agent(client, strategy="window", keep_last=KEEP)
        _talk(client, win_id, 9)

        win = REGISTRY.require(win_id)
        sent = _stub.CALLS[-1]["messages"]
        # Ровно последние KEEP реплик и вопрос — и ни одной врезки: окно
        # ничего не заменяет, оно отбрасывает.
        assert [m["role"] for m in sent] == ["user", "assistant"] * 3 + ["user"], sent
        assert len(sent) == KEEP + 1, len(sent)
        assert sent[0]["content"] == "вопрос 5", sent[0]
        assert sent[-1]["content"] == "вопрос 8", sent[-1]
        assert not any("пересказ начала разговора" in m["content"] for m in sent), sent

        # Отброшенное названо: число в метриках обмена, и оно ровно то, чего
        # в промпте не оказалось. Выброси его — и обрезка станет молчаливой.
        cut, insert = win.context_cut()
        assert insert is None, insert
        # В истории сейчас 18 реплик, и следующий обмен отбросит 12 из них.
        assert cut == 12, cut
        # А этот отбросил 10: его число считали до того, как он сам записался,
        # — ровно как у сводки, и потому оно сходится с тем, что уехало.
        dropped = win.history[-1].metrics["dropped"]
        assert dropped == 10, win.history[-1].metrics
        assert dropped + (len(sent) - 1) == len(win.history) - 2 == 16, (
            dropped, len(sent), len(win.history)
        )
        assert "summarized" not in win.history[-1].metrics, "окно назвалось сводкой"
        # Первый обмен отбрасывать было нечего — и приписки у него нет.
        assert win.history[1].metrics.get("dropped") is None, win.history[1].metrics

        # История цела: окно живёт в сборке промпта, а не в памяти чата.
        # На этом держится перегенерация — она ждёт хвост и сверяет длину.
        assert len(win.history) == 18, len(win.history)
        assert win.history[0].content == "вопрос 0", win.history[0]
        assert win.take_last_exchange() is not None, "перегенерация не сняла пару"

        # И к модели за это не ходят: окно бесплатное, служебного вызова у
        # него нет ни одного.
        assert not _service_calls(), "окно зачем-то сходило к модели"
        assert len(_stub.CALLS) == 9, len(_stub.CALLS)

        # Пустой хвост — резать нечем: `None` это «не делать», а не «оставить
        # ноль». Стратегия выбрана, а число нет — история уезжает целиком.
        off = client.patch(f"/api/agents/{win_id}", json={"keep_last": None})
        assert off.status_code == 200, off.text
        assert len(win.history) == 16, "перегенерация сняла не пару"
        assert win.context_cut() == (0, None), win.context_cut()
        assert len(win.build_prompt("ещё")) == 17, len(win.build_prompt("ещё"))

    # Ноль в хвосте — это ноль, а не «не задано»: уезжает только вопрос.
    bare = _fill(
        _bare("ноль", strategy="window", keep_last=0, system="СИС"), 6
    )
    assert bare.context_cut() == (6, None), bare.context_cut()
    assert [m["content"] for m in bare.build_prompt("вопрос")] == ["СИС", "вопрос"], (
        bare.build_prompt("вопрос")
    )

    # --- окно режет ровно столько, сколько просили --------------------------
    # Зажима «не дальше прочитанного» нет: записи рабочей памяти вписал
    # человек, и от длины разговора они не зависят. Зато видно главное
    # различие слоёв: окно **отбрасывает** начало разговора, а вписанная
    # руками цель уезжает в модель всё равно.
    _stub.reset()
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        narrow = new_agent(client, strategy="window", keep_last=2)
        goal = client.post(
            f"/api/agents/{narrow}/working", json={"kind": "goal", "content": "собрать ТЗ"}
        )
        assert goal.status_code == 200, goal.text
        _talk(client, narrow, 5)

        window = REGISTRY.require(narrow)
        sent = _stub.CALLS[-1]["messages"]
        # Врезка рабочей памяти, две последние реплики и вопрос. Начало
        # разговора отброшено — его в промпте нет вовсе, — а цель на месте.
        assert [m["role"] for m in sent] == ["user", "user", "assistant", "user"], sent
        assert sent[0]["content"] == (
            "[факты о разговоре]\nцель: собрать ТЗ\n[конец фактов о разговоре]"
        ), sent[0]
        assert sent[1]["content"] == "вопрос 3", sent[1]
        assert not any("вопрос 0" in m["content"] for m in sent), sent
        # В истории десять реплик, и следующий обмен отбросит восемь; этот
        # отбросил шесть — его число считали до того, как он сам записался.
        assert window.context_cut()[0] == 8, window.context_cut()
        assert window.history[-1].metrics["dropped"] == 6, window.history[-1].metrics
        # И к модели за это не ходят ни разу: обменов пять, вызовов пять.
        assert not _service_calls(), "окно или рабочая память сходили к модели"
        assert len(_stub.CALLS) == 5, len(_stub.CALLS)

    # --- summary: сводка вместо начала, механика Дня 9 нетронутой -------------
    # 4. Порог не набран — история уезжает целиком: **без сводки история
    # не режется**, и это отличает сводку от окна, которое режет сразу.
    _stub.reset()
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        folded_id = new_agent(
            client,
            strategy="summary",
            keep_last=KEEP,
            compress_every=EVERY,
            stop=["СТОП"],
            response_format={"type": "json_object"},
        )
        _talk(client, folded_id, 5)

        early = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in early] == ["user", "assistant"] * 4 + ["user"], early
        assert early[0]["content"] == "вопрос 0", early[0]
        assert not _service_calls(), "сжатие запустилось до порога"

        # 5. Дошли до порога: 9-й обмен сам уезжает уже сжатым.
        _talk(client, folded_id, range(5, 9))

        folded = REGISTRY.require(folded_id)
        covered = folded.summary_cover()
        sent = _stub.CALLS[-1]["messages"]
        tail = sent[1:-1]

        assert len(folded.summaries) == 1, folded.summaries
        assert covered == 10, covered
        # Сводка — первой репликой, с подписью: это не вопрос пользователя.
        assert sent[0]["role"] == "user" and "пересказ начала разговора" in sent[0]["content"], sent[0]
        assert "СВОДКА" in sent[0]["content"], sent[0]
        # Хвост — ровно последние N, и начинается он там, где кончилась сводка.
        assert len(tail) == KEEP, [m["content"] for m in tail]
        assert tail[0]["content"] == "вопрос 5", tail[0]
        # Равенство, отличающее сводку от окна: свёрнутое плюс хвост — вся
        # история на момент сборки. 16 реплик восьми обменов, девятый сжат.
        assert covered + len(tail) == 16 == len(folded.history) - 2, (
            covered, len(tail), len(folded.history)
        )
        # Системного сообщения в голом чате нет и со сжатием тоже.
        assert not any(m["role"] == "system" for m in sent), sent

        # История не тронута: сжатие меняет промпт, а не память чата.
        assert [t.content for t in folded.history[:2]] == ["вопрос 0", "ответ 0"], folded.history[:2]

        # 6. Свёрнутое уехало в сжатие, а не пропало: вызов на сжатие видел
        # ровно те реплики, которых больше нет в промпте.
        folding = _service_calls("summary")[-1]["messages"]
        assert folding[0]["role"] == "system", folding[0]
        assert "вопрос 0" in folding[1]["content"] and "ответ 4" in folding[1]["content"], folding[1]
        assert "вопрос 5" not in folding[1]["content"], "в сжатие уехал хвост, который остаётся как есть"

        # Формат ответа и стоп-строки на время сжатия сняты: первый вернул бы
        # объект вместо пересказа, вторая оборвала бы его. У обмена они есть.
        folding_body = _service_calls("summary")[-1]["payload"]
        assert "response_format" not in folding_body, folding_body.get("response_format")
        assert "stop" not in folding_body, folding_body.get("stop")
        assert _stub.CALLS[-1]["payload"]["response_format"] == {"type": "json_object"}
        assert _stub.CALLS[-1]["payload"]["stop"] == ["СТОП"]
        # А модель та же: вторая развалила бы счёт на две цены. Всё
        # из одного `replace` — стеречь его надо целиком.
        assert folding_body["model"] == _stub.CALLS[-1]["payload"]["model"], (
            folding_body["model"],
            _stub.CALLS[-1]["payload"]["model"],
        )

        # Уехавший сжатым обмен говорит об этом метриками. Слово у сводки
        # своё: она начало **заменила**, и его видно в промпте.
        assert folded.history[-1].metrics["summarized"] == 10, folded.history[-1].metrics
        assert "dropped" not in folded.history[-1].metrics, "сводка назвалась окном"
        assert folded.history[-3].metrics.get("summarized") is None, "пометка досталась обмену до сжатия"

        # 7. Второе сворачивание идёт инкрементально: прошлая сводка плюс
        # только новое, а не пересказ разговора с начала.
        _talk(client, folded_id, range(9, 14))
        assert len(folded.summaries) == 2, folded.summaries
        again = _service_calls("summary")[-1]["messages"][1]["content"]
        assert "СВОДКА" in again, "прошлая сводка в сжатие не попала — начало разговора потеряно"
        assert "вопрос 0" not in again, "сжатие пересказывает историю с начала заново"
        assert "вопрос 5" in again, again[:200]
        assert folded.summary_cover() == 20, folded.summary_cover()
        assert len(folded.history) == 28, len(folded.history)

        # 8. Переключатель обратим в обе стороны, и сводки это переживают:
        # у `full` история едет **целиком**, а сводка не выбрасывается.
        off = client.patch(f"/api/agents/{folded_id}", json={"strategy": "full"})
        assert off.status_code == 200, off.text
        client.post(f"/api/agents/{folded_id}/messages", json={"text": "после выключения"})
        back = _stub.CALLS[-1]["messages"]
        assert len(back) == 28 + 1, len(back)
        assert back[0]["content"] == "вопрос 0", back[0]
        assert folded.context_cut() == (0, None), folded.context_cut()
        assert not any("пересказ начала разговора" in m["content"] for m in back), back[0]
        assert len(folded.summaries) == 2, "сводки выброшены переключателем"
        assert folded.summaries[-1]["upto"] == 20, folded.summaries[-1]

        # `window` на том же чате: сводка в базе есть, но в промпт не идёт.
        # Подставься она здесь — вышла бы не та стратегия, что выбрали.
        client.patch(f"/api/agents/{folded_id}", json={"strategy": "window"})
        client.post(f"/api/agents/{folded_id}/messages", json={"text": "с окном"})
        win_back = _stub.CALLS[-1]["messages"]
        assert len(win_back) == KEEP + 1, len(win_back)
        assert not any("пересказ начала разговора" in m["content"] for m in win_back), win_back[0]
        assert folded.history[-1].metrics["dropped"] == 30 - KEEP, folded.history[-1].metrics

        # И обратно в `summary` — граница та же, пересказывать заново не нужно.
        client.patch(f"/api/agents/{folded_id}", json={"strategy": "summary"})
        assert folded.summary_cover() == 20, folded.summary_cover()
        assert len(folded.summaries) == 2, "включение обратно пересобрало сводки заново"

        # Чужая стратегия — 400 с перечислением допустимых, а не молчаливое
        # умолчание: молча подменённая стратегия резала бы не то, что выбрали.
        bad = client.patch(f"/api/agents/{folded_id}", json={"strategy": "окно"})
        assert bad.status_code == 400, bad.status_code
        assert all(name in bad.text for name in ("full", "window", "summary")), bad.text
        assert "facts" not in bad.text, "«Факты» остались стратегией"
        assert folded.spec.strategy == "summary", folded.spec.strategy

    # 9. Граница сворачивания не рвёт пару: свёрнутый вопрос без ответа
    # сделал бы хвост бессмысленным. Округляется вниз до чётного.
    odd = _fill(
        _bare("нечёт", strategy="summary", keep_last=5, compress_every=EVERY), 20
    )
    asyncio.run(odd.compress(odd.spec))
    odd_cover = odd.summary_cover()
    assert odd_cover == 14, odd_cover
    assert odd_cover % 2 == 0, f"граница разорвала пару: свёрнуто {odd_cover} реплик"
    assert odd.history[odd_cover].role == "user", odd.history[odd_cover].role

    # 10. Перегенерация снимает пару **с конца**, сводка покрывает начало,
    # и на коротком чате `upto` оказывается больше истории.
    short = _fill(
        _bare("перегенерация", strategy="summary", keep_last=0, compress_every=EVERY,
), 10
    )
    asyncio.run(short.compress(short.spec))
    assert short.summary_cover() == 10, short.summary_cover()
    assert short.take_last_exchange() is not None, "перегенерация не сняла пару"
    short_cover = short.summary_cover()
    assert short_cover == 8 == len(short.history), (short_cover, len(short.history))
    short_prompt = short.build_prompt("вопрос после перегенерации")
    assert short_cover + (len(short_prompt) - 2) == len(short.history), short_prompt

    # 11. Умолчание — «не резать»: «Новый чат» идёт мимо разбора полей,
    # и стань окно умолчанием — резали бы все новые чаты и вся консоль.
    _stub.reset()
    with TestClient(main.app) as client:
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        assert fresh["strategy"] == "full", fresh["strategy"]
        assert fresh["keep_last"] is None, fresh["keep_last"]
        assert fresh["compress_every"] is None, fresh["compress_every"]
        _talk(client, fresh["id"], 9)
    assert not _service_calls(), "чат из умолчаний ходил к модели за чем-то ещё"
    # И к модели он ходит ровно по разу на обмен: служебный вызов у чата
    # из умолчаний один — сжатие, — и тот не запускается.
    assert len(_stub.CALLS) == 9, len(_stub.CALLS)
    assert len(_stub.CALLS[-1]["messages"]) == 17, len(_stub.CALLS[-1]["messages"])
    assert _stub.CALLS[-1]["messages"][0]["content"] == "вопрос 0", _stub.CALLS[-1]["messages"][0]

    return (
        f"full — вся история из {2 * turns} реплик и 500 хранимых целиком; "
        f"window — {KEEP + 1} сообщений в промпте, отброшено 10 и названо числом; "
        "окно отбросило 6 реплик, а вписанная руками цель уехала в модель "
        "всё равно; "
        f"summary — сводка и хвост, {covered} свёрнутых плюс {KEEP} хвоста"
    )


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
    # Память в этом чате выключена: второе обращение к модели за обмен
    # сбило бы «в модель ушёл ровно один вызов».
    assert codes == [200, 409], codes
    agent = REGISTRY.require(agent_id)
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert len(_stub.CALLS) == 1, f"в модель ушло {len(_stub.CALLS)} вызовов, а должен один"
    return f"коды {codes}, в истории 2 реплики, вызов к модели один"


# --- День 9: сжатие истории ---------------------------------------------------


@check("сжатие экономит входные токены, и сводка покрывает выброшенное")
def check_compression_saves_input():
    """Сравнение расхода «до/после»: два одинаковых чата, у одного окно задано.
    `prompt_tokens` заглушка считает по длине отправленного, поэтому экономия
    видна без живых вызовов, и в итог у сжатого входит сам вызов на сжатие."""
    _stub.install(reply=_service_aware)
    question = "вопрос {i}: " + "довольно длинный текст вопроса, " * 20

    # События каждого обмена — по ним видно, что наружу сказано про сжатие:
    # оно идёт **до** ответа, и клиенту надо узнать о паузе, пока она идёт.
    events: dict[str, list[list[dict]]] = {}

    with TestClient(main.app) as client:
        # Память обоим выключена: проверка про экономию сжатия, и лишнее
        # обращение к модели попало бы в обе колонки сравнения.
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

    # Вызов на сжатие — такой же вызов, и три правила тела на нём тоже:
    # со включённым `context-compression` провайдер выбросил бы середину
    # куска, отданного на пересказ.
    folding_body = _service_calls("summary")[-1]["payload"]
    assert not _body_rules(folding_body), f"вызов на сжатие: {_body_rules(folding_body)}"

    # Про сворачивание сказано наружу и **до** вызова: карточка, узнавшая
    # о паузе после неё, показала бы строку состояния на пустом месте.
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
    # И **чем** занята пауза, называет сервер: без этого поля строка
    # состояния не знала бы, что писать.
    folding_frame = _frame(events[folded][8], "compressing")
    assert folding_frame.get("strategy") == "summary", folding_frame

    # И сводку в промпте показывает сервер полем `summary_at`, а не разбор
    # текста: вторая копия `build_prompt` в браузере разошлась бы молча.
    def start_of(agent_id, index):
        return _frame(events[agent_id][index], "start")

    assert start_of(folded, 7)["summary_at"] is None, "сводка объявилась раньше сворачивания"
    assert start_of(plain, 11)["summary_at"] is None, "в чате без сжатия нашлась сводка"
    last = start_of(folded, 11)
    assert last["summary_at"] == 0, last["summary_at"]
    assert "пересказ начала разговора" in last["resolved_messages"][last["summary_at"]]["content"], (
        f"summary_at показывает не на сводку: {last['resolved_messages'][last['summary_at']]}"
    )
    # Место врезки без имени неполно: подпись роли берётся из того же поля,
    # и промолчи сервер — сводка перестала бы называться сводкой.
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
    """Половины две, и обе про одно: у сводки своя таблица.

    **Первая** — сводка лежит не строкой в `messages`: `save_history` делает
    `DELETE FROM messages`, и лежи она там, её стирал бы каждый обмен.
    **Вторая** — плата за отдельную таблицу: каскада нет, и каждый слой
    уносится руками (`CLEANUP_TABLE`). Проходятся клетки разом и на чате,
    у которого непусты все шесть слоёв: утверждение об очистке обязано
    стоять на непустом значении.
    """
    from app.agent import Agent

    _stub.install(reply=_service_aware)
    path = _temp_db("summary-restart")
    store = Store(path).init()
    # Память и рабочий процесс выключены: их врезки стояли бы перед сводкой,
    # сдвигая всё, на что проверка смотрит по номеру.
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
        # Путь получает свой чат: `clear()` стирает базу целиком, и общий чат
        # оставил бы последним двум путям пустую сцену.
        for column, (path_name, wipe) in enumerate(CLEANUP_PATHS.items()):
            chat = _filled_chat(again, path_name)
            full = _leftovers(again, chat.id)
            # Сцена непуста во всех шести слоях: без этого «после очистки
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

        # И в памяти объекта сводка пуста, а не только в файле: оставь её,
        # и она снова стала бы действующей. Смотрим на отросшую историю.
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


@check("рабочая память: пишет её человек, и она уезжает поверх любой обрезки")
def check_working_memory():
    """Рабочая память — второй слой: состояние **этой задачи**, записями.
    Пишет в неё только человек, отсюда и главный вопрос: **к модели
    за память не ходят ни разу**. Части четыре: врезка, ручки, разница слоёв
    (окно отбросило начало разговора, а вписанная цель уехала всё равно)
    и файл."""
    from app.agent import Agent

    # --- 1. Живой маршрут: врезка при окне, хвост как есть ------------------
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # Вариант обрезки здесь — окно, самое недоверчивое к памяти: оно
        # начало **отбрасывает**, а врезка всё равно встаёт.
        agent_id = new_agent(
            client,
            strategy="window",
            keep_last=KEEP,
            system="СИС",
            stop=["СТОП"],
            response_format={"type": "json_object"},
        )
        url = f"/api/agents/{agent_id}/working"
        kept = client.post(url, json={"kind": "decision", "content": "берём Kotlin"})
        assert kept.status_code == 200, kept.text
        kept = kept.json()
        for response in _talk(client, agent_id, 9):
            assert response.status_code == 200, response.text

        agent = REGISTRY.require(agent_id)
        sent = _stub.CALLS[-1]["messages"]

        # К модели сходили ровно девять раз — по разу на обмен: служебного
        # вызова за рабочей памятью нет, и цена чата не выросла ни на токен.
        assert len(_stub.CALLS) == 9, len(_stub.CALLS)
        assert not _service_calls(), "за рабочей памятью сходили к модели"

        frames = sse(response.text)
        assert not [e for e in frames if e["event"] == "compressing"], frames
        start = _frame(frames, "start")
        assert start.get("strategy") == "window", start.get("strategy")
        # Слот у врезки рабочей памяти свой: стратегия её не заказывает,
        # и слот стратегии у окна пуст — оно ничего не вставляет.
        assert start["summary_at"] is None, start["summary_at"]
        assert start["working_at"] == 1, start["working_at"]
        assert "[факты о разговоре]" in start["resolved_messages"][start["working_at"]]["content"], (
            start["resolved_messages"][start["working_at"]]
        )

        # Промпт: системный промпт, врезка, хвост в KEEP реплик, вопрос.
        assert [m["role"] for m in sent] == (
            ["system", "user"] + ["user", "assistant"] * 3 + ["user"]
        ), [m["role"] for m in sent]
        assert sent[0]["content"] == "СИС", sent[0]
        # Врезка едет ролью `user` с подписью: второго системного сообщения
        # у чата не бывает. Тип подписан той же картой, что во вкладке,
        # а скобка нейтральная — за врезкой может встать сводка.
        assert sent[1] == {
            "role": "user",
            "content": (
                "[факты о разговоре]\n"
                "решение: берём Kotlin\n"
                "[конец фактов о разговоре]"
            ),
        }, sent[1]
        assert len([m for m in sent if m["role"] == "system"]) == 1, sent
        assert sent[2]["content"] == "вопрос 5", sent[2]
        assert sent[-1]["content"] == "вопрос 8", sent[-1]

        # Отброшенное названо числом, и врезки у окна нет вовсе: начало оно
        # отбросило, а не заменило.
        cut, insert = agent.context_cut()
        assert insert is None, insert
        assert cut == 12, cut
        slots = agent.prompt_slots()
        assert slots["working_at"] == 1, slots
        assert slots["summary_at"] is None, slots
        assert agent.history[-1].metrics["dropped"] == 10, agent.history[-1].metrics
        assert "summarized" not in agent.history[-1].metrics, "окно назвалось сводкой"
        assert "facts" not in agent.history[-1].metrics, "врезка памяти назвалась обрезкой"

        # История цела: память живёт в сборке промпта, а не в ленте чата.
        # На этом держится перегенерация — она ждёт хвост и сверяет длину.
        assert len(agent.history) == 18, len(agent.history)
        assert agent.history[0].content == "вопрос 0", agent.history[0]
        assert agent.take_last_exchange() is not None, "перегенерация не сняла пару"

        # --- 2. Ручки чата: тип обязателен, номер выдаёт база ---------------
        # Тип без умолчания, как у долговременной памяти: подставь сервер тип
        # за человека — и «явно выбирать» перестало бы быть его работой.
        listed = client.get(url).json()
        assert listed["total"] == 1, listed
        assert listed["records"][0]["seq"] == kept["seq"], listed["records"]
        # Автора у записи нет вовсе: писать в слой некому, кроме человека.
        assert set(listed["records"][0]) == {"seq", "kind", "content", "at"}, listed["records"][0]

        bad = client.post(url, json={"content": "без типа"})
        assert bad.status_code == 400 and "goal" in bad.text, bad.text
        assert client.post(url, json={"kind": "цель", "content": "подписью"}).status_code == 400
        assert client.post(url, json={"kind": "profile", "content": "чужой слой"}).status_code == 400
        assert client.post(url, json={"kind": "goal", "content": "   "}).status_code == 400
        # Номер, время и автора сервер не спрашивает: тело, которое их
        # присылает, просит не то, что ручка делает. Принять автора значило бы
        # завести в слое второе мнение о том, кто пишет.
        assert client.post(
            url, json={"kind": "goal", "content": "х", "seq": 5}
        ).status_code == 400
        assert client.post(
            url, json={"kind": "goal", "content": "х", "author": "human"}
        ).status_code == 400

        human = client.post(url, json={"kind": "limit", "content": "  бюджет 100к  "})
        assert human.status_code == 200, human.text
        human = human.json()
        assert human["content"] == "бюджет 100к", human
        assert human["seq"] > kept["seq"], (human, kept)

        edited = client.patch(f"{url}/{human['seq']}", json={"content": "бюджет 200к"})
        assert edited.status_code == 200, edited.text
        assert edited.json()["seq"] == human["seq"], edited.json()
        assert edited.json()["kind"] == "limit", edited.json()
        # Тип правится тем же телом и **в одиночку**: иначе запись не того
        # типа чинилась бы только удалением. Номер и текст при этом стоят.
        retyped = client.patch(f"{url}/{human['seq']}", json={"kind": "goal"})
        assert retyped.status_code == 200, retyped.text
        assert retyped.json()["kind"] == "goal", retyped.json()
        assert retyped.json()["content"] == "бюджет 200к", retyped.json()
        assert retyped.json()["seq"] == human["seq"], retyped.json()
        assert client.patch(f"{url}/{human['seq']}", json={"kind": "limit"}).status_code == 200
        assert client.patch(f"{url}/{human['seq']}", json={}).status_code == 400
        assert client.patch(f"{url}/{human['seq']}", json={"kind": "х"}).status_code == 400
        assert client.patch(f"{url}/9999", json={"content": "нет такой"}).status_code == 404
        assert client.delete(f"{url}/9999").status_code == 404

        # Тип — не ключ: две записи одного типа живут рядом, ключ у списка
        # это номер. Словарь по типу вытеснял бы вторую «цель» молча.
        twin = client.post(url, json={"kind": "limit", "content": "срок до мая"})
        assert twin.status_code == 200, twin.text
        twin = twin.json()
        assert twin["seq"] > human["seq"], (twin, human)
        assert [(r["kind"], r["content"]) for r in client.get(url).json()["records"]] == [
            ("decision", "берём Kotlin"), ("limit", "бюджет 200к"), ("limit", "срок до мая"),
        ], client.get(url).json()["records"]

        # Удаление плюс новая запись: номер удалённой не достаётся следующей.
        # Обычный `INTEGER PRIMARY KEY` отдал бы его новой, и правка
        # «по номеру три» попала бы не в ту запись.
        assert client.delete(f"{url}/{twin['seq']}").status_code == 200
        after = client.post(url, json={"kind": "goal", "content": "после удаления"}).json()
        assert after["seq"] > twin["seq"], (after, twin)
        assert client.delete(f"{url}/{after['seq']}").status_code == 200

        # И запись человека уезжает в промпт наравне с первой: слой один.
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос после правки"})
        block = _stub.CALLS[-1]["messages"][1]["content"]
        assert "ограничение: бюджет 200к" in block, block
        assert "решение: берём Kotlin" in block, block

    # --- 3. Разница слоёв в одном кадре -------------------------------------
    # Ради этого слой и заведён: окно отбрасывает начало разговора, а
    # вписанная руками цель уезжает в модель всё равно.
    _stub.reset()
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        narrow = new_agent(client, strategy="window", keep_last=2)
        client.post(
            f"/api/agents/{narrow}/working", json={"kind": "goal", "content": "собрать ТЗ"}
        )
        _talk(client, narrow, 10)
        sent = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in sent] == ["user", "user", "assistant", "user"], sent
        assert sent[0]["content"].startswith("[факты о разговоре]"), sent[0]
        assert "цель: собрать ТЗ" in sent[0]["content"], sent[0]
        assert not any("вопрос 0" in m["content"] for m in sent), sent
        chat = REGISTRY.require(narrow)
        assert chat.history[-1].metrics["dropped"] == 16, chat.history[-1].metrics
        assert len(chat.history) == 20, len(chat.history)
        # И за десять обменов — десять вызовов: слой ничего не стоит.
        assert len(_stub.CALLS) == 10, len(_stub.CALLS)

    # --- 4. Файл: перезапуск переживают, чистку — нет -----------------------
    _stub.reset()
    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("working-restart")
    store = Store(path).init()
    spec = AgentSpec(label="с памятью", model="stub/model", strategy="window", keep_last=KEEP)
    agent = Agent(spec, store=store)
    _ask(agent, 9)
    mine = agent.add_working_record("question", "успеем ли к маю")
    goal = agent.add_working_record("goal", "собрать ТЗ")
    before = [dict(r) for r in agent.working]
    agent_id = agent.id
    assert [(r["kind"], r["content"]) for r in before] == [
        ("question", "успеем ли к маю"), ("goal", "собрать ТЗ"),
    ], before

    with _restarted(store, agent_id) as (again, revived):
        # Номера пережили перезапуск: без них правка «по номеру» после
        # подъёма чата попала бы не в ту запись.
        assert revived.working == before, (before, revived.working)
        assert len(revived.history) == 18, len(revived.history)
        assert "[факты о разговоре]" in revived.build_prompt("ещё")[0]["content"], "врезка не встала"

        # Записей нет среди реплик: они в своей таблице. Лежи они в
        # `messages`, их стирал бы каждый обмен — там `DELETE` первой строкой.
        contents = [r[2] for r in again.message_rows(agent_id)]
        assert not any("[факты о разговоре]" in c for c in contents), contents

        asyncio.run(drain(revived.ask("вопрос после перезапуска")))
        assert again.list_working(agent_id) == before, again.list_working(agent_id)
        assert len(revived.history) == 20, len(revived.history)

        # Чистка чата уносит и рабочую память: каскада в схеме нет, а зажима
        # по длине истории у врезки нет вовсе — забытый разговор оставил бы
        # свои цели следующему.
        revived.forget()
        assert again.list_working(agent_id) == [], again.list_working(agent_id)
        assert revived.working == [], revived.working
        assert again.message_rows(agent_id) == [], again.message_rows(agent_id)
        _ask(revived, 4, "новый вопрос {i}")
        # Врезки в промпте нового разговора нет вовсе: записи унесены вместе
        # с историей, и цели забытого разговора следующему не достались.
        fresh = revived.build_prompt("ещё")
        assert not any("[факты о разговоре]" in m["content"] for m in fresh), fresh
        assert not any("успеем ли к маю" in m["content"] for m in fresh), fresh

        # Обе клетки очистки — в таблице «слой × путь». Здесь остаётся то,
        # чего в ней нет: страж чужого чата. Номера сквозные на всю базу,
        # и без `session_id` в `WHERE` правка ушла бы в соседний разговор.
        doomed = Agent(spec, store=again)
        keeper = Agent(spec, store=again)
        keeper.add_working_record("goal", "цель соседа")
        someone = again.list_working(keeper.id)[0]
        assert again.update_working(
            doomed.id, someone["seq"], kind="goal", content="взлом"
        ) is None, "правка по чужому чату прошла"
        assert again.delete_working(doomed.id, someone["seq"]) is False, "удаление по чужому чату прошло"
        assert again.list_working(keeper.id)[0] == someone, again.list_working(keeper.id)

    return (
        f"при окне врезка и хвост в {KEEP} реплик, слот врезки свой и назван "
        "в кадре `start`; за девять обменов девять вызовов к модели и ни одного "
        "служебного; тип обязателен и без умолчания, номер устойчив и заново "
        "не выдаётся, автора у записи нет; окно отбросило 16 реплик, а вписанная "
        "руками цель уехала в модель; номера пережили перезапуск, три пути "
        "очистки — нет, чужой чат не тронуть"
    )


@check("ветка уносит ровно просимое, живёт независимо и переживает перезапуск")
def check_branch_independent():
    """Ветвление: «создайте 2 ветки от одного места, продолжите диалог
    в каждой независимо, переключайтесь между ними».

    Ветка — **отдельный чат**: своя строка в `sessions`, свой id, своя
    история. Разделы: унесено ровно просимое; сводка едет только своя
    (`upto <= at`), а записи рабочей памяти все; дальше чат сам по себе.
    Раздела про «переключайтесь» нет намеренно: это открыть чат из списка,
    а что открытый отдаёт свою историю, стережёт `check_session_isolation`.
    """
    # --- две ветки от одного места, и каждая живёт своей жизнью ------------
    _stub.install(reply=lambda messages, i: "ответ на " + messages[-1]["content"])
    with TestClient(main.app) as client:
        # Память выключена: раздел про копию истории и конфига, и врезка
        # встала бы в каждый промпт, который сверяется дословно.
        parent = new_agent(
            client, label="родитель", system="СИС", temperature=0.5,
            extra_body={"provider": {"order": ["stub"]}},
        )
        _talk(client, parent, 3)
        spoken = len(_stub.CALLS)
        store = REGISTRY.store
        # Снимок ленты родителя **до** ветвления — единственное окно, где
        # видно, тронуло ли оно чужую сессию: заговори родитель снова,
        # и `persist()` перепишет `messages` из памяти, затерев повреждение.
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

        # Для родителя ветвление — операция **только на чтение**. Смотрим
        # сразу, пока никто не заговорил: одна лишняя `save_history(parent_id,
        # ...)` стёрла бы ему хвост разговора насовсем, а после первой же
        # его реплики это уже не видно.
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

        # И в базе три разные ленты: `save_history` начинается с `DELETE`
        # по `session_id`, и общая сессия стоила бы разговора.
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

        # Тот же список **холодным** путём: пометка приезжает другой ветвью
        # кода — у живого чата её отдаёт он сам, у выгруженного строка
        # из базы, — и без этого она пропадала бы после перезапуска.
        assert REGISTRY._unload(one["id"]) is True, "ветка не была живой"
        cold = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
        assert cold[one["id"]]["branch"] == {"parent_id": parent, "forked_at": 4}, cold[one["id"]]
        assert cold[one["id"]]["history_len"] == 4, cold[one["id"]]
        assert cold[parent]["branch"] is None, cold[parent]

        # Ветка от ветки: родство называет того, от кого отделились, а не
        # деда. Ветвимся от выгруженной — та годится в родители.
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

        # И чат, где не сказано ни слова, ветвится так же: унести нечего,
        # но конфиг у ветки его, а `at = 0` у чата с историей — не тот случай.
        fresh = new_agent(client, label="свежий")
        blank = client.post(f"/api/agents/{fresh}/fork", json={"at": 0})
        assert blank.status_code == 200, blank.text
        sprout = blank.json()["agents"][0]
        assert sprout["history_len"] == 0, sprout["history_len"]
        assert sprout["branch"] == {"parent_id": fresh, "forked_at": 0}, sprout["branch"]
        # А `at = 1` у такого чата — 400: уносить нечего.
        assert client.post(f"/api/agents/{fresh}/fork", json={"at": 1}).status_code == 400

    # --- ветвление у занятого родителя ------------------------------------
    # Ручка обещает, что занятость родителя не мешает: история не меняется
    # до конца обмена. Ветвимся **посреди** ответа, а точку берём меньше
    # длины истории — сравнение «унесено ровно `at`» тогда отличает её
    # и от полной истории, и от укороченной.
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
    # Обрезать сводку по смыслу нельзя — она связный текст, — поэтому правило
    # по границе: едет то, что покрывает только унесённое. Сводки
    # инкрементальны, и префикс их списка сам готовая сводка своего начала.
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

    # И продолжение родителя ветку не трогает **и на уровне врезки**: общий
    # список сводок дал бы ей сводку про реплики, которых в ней нет.
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
    # `at`: её вписал человек, записи не заменяют собой ни одной реплики,
    # а ветка продолжает ту же задачу.
    early = reg.fork(listing, 2, label="ветка от второй реплики")
    late = reg.fork(listing, 10, label="ветка от конца")
    for branch in (early, late):
        assert [(r["kind"], r["content"]) for r in branch.working] == said, branch.working
        assert store.list_working(branch.id) == branch.working, store.list_working(branch.id)
        assert branch.build_prompt("ещё")[0]["content"].startswith("[факты о разговоре]"), (
            "память не встала в промпт ветки"
        )
    # Номера у копий свои: номер принадлежит одному чату, и две записи под
    # одним номером — та путаница, из-за которой он стал сквозным.
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

        # Удаление родителя ветку **не** удаляет: она самостоятельный чат,
        # а пометку про удалённого клиент строит по отсутствию id в списке.
        assert again.delete_session(parent_id) is True
        assert again.load_branch(branch_id) == {"parent_id": parent_id, "forked_at": 18}, (
            "удаление родителя унесло родство ветки"
        )
        assert len(again.message_rows(branch_id)) == 20, "ветка ушла вслед за родителем"
        orphan = {row["id"]: row for row in again.list_sessions()}[branch_id]
        assert orphan["branch"]["parent_id"] == parent_id, orphan["branch"]

        # `forget()` родства не трогает: ветка, забывшая историю, осталась
        # веткой. Клетки таблицы проходит `check_summary_apart_and_cleanup`;
        # здесь — то, чего в ней нет: забытая история и живая пометка рядом.
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
    """Третий слой памяти — долговременный: таблица **без** `session_id`,
    наполняется **только руками**, и чат ему читатель, а не владелец.

    По порядку: пустая память неотличима от отсутствующей; ручки (тип
    выбирает человек); врезка и её место; **три врезки разом**, где слот
    каждой следующей сдвинут предыдущими; удаление по одной без
    перенумерации; переживание `forget()` и удаления чата; `clear()`.
    """
    from app.agent import Agent

    # Пока разбирается пустота, служебному вызову отвечаем так, чтобы
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Пустая память неотличима от отсутствующей --------------------
        # Пусто в обоих слоях сразу: врезки нет вовсе, а не пустая, — иначе
        # каждая проверка с точной последовательностью ролей поехала бы.
        assert client.get("/api/memory").json() == {"total": 0, "records": []}, "память не пуста"
        plain = new_agent(client, system="СИС")
        start = _frame(_frames(client, plain, "первый"), "start")
        assert start["memory_at"] is None, start["memory_at"]
        assert start["working_at"] is None, start["working_at"]
        assert [m["role"] for m in _stub.CALLS[-1]["messages"]] == ["system", "user"], _stub.CALLS[-1]

        # --- 2. Ручки: тип записи выбирает человек, а не сервер ---------------
        # `kind` обязателен и без умолчания: подставь сервер тип
        # на пропущенный ключ — и выбирал бы он, а не человек.
        bad = [
            {"content": "тип не назван"},
            {"kind": None, "content": "тип снят"},
            {"kind": "profil", "content": "тип с опечаткой"},
            {"kind": "profile"},
            {"kind": "profile", "content": "   "},
            {"kind": "profile", "content": "лишнее поле", "seq": 5},
        ]
        for payload in bad:
            answer = client.post("/api/memory", json=payload)
            assert answer.status_code == 400, (payload, answer.status_code, answer.text)
        assert "profile" in client.post("/api/memory", json={"content": "х"}).json()["detail"]

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

        # --- 3. Врезка: роль, подписи, место ---------------------------------
        # Роль `user` с подписью, а не `system`: второго системного сообщения
        # у чата не бывает. Тип подписан по-русски, одной картой с интерфейсом.
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
        # Закрывающая скобка нейтральная: после памяти может встать врезка
        # стратегии, и «дальше — последние сообщения как есть» соврало бы.
        assert "как есть" not in block, block

        # Чат без системного промпта: память стоит нулевым сообщением, и ноль
        # здесь — не «врезки нет». На этом держится строгое сравнение у клиента.
        bare = new_agent(client, system="")
        start = _frame(_frames(client, bare, "голый"), "start")
        assert start["memory_at"] == 0, start["memory_at"]
        assert start["working_at"] is None, start["working_at"]
        assert start["summary_at"] is None, start["summary_at"]
        assert start["resolved_messages"][0]["content"].startswith("[долговременная память]")

        # --- 4. Три врезки разом, и слоты сдвинуты ---------------------------
        # Ни одна врезка не отменяет другую, и в промпте они стоят втроём,
        # от общего к частному. Слот каждой следующей сдвинут теми, что
        # встали перед ней: считай его по-старому — и просмотр промпта
        # подписал бы памятью сводку.
        _stub.install(reply=_service_aware)
        both = new_agent(
            client, system="СИС", strategy="summary", keep_last=2, compress_every=2
        )
        # Запись рабочей памяти вписана руками: других в этом слое не бывает,
        # и без неё врезок в промпте было бы две, а не три.
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

        # Служебный вызов на обмене ровно один — сжатие: за память к модели
        # не ходят вовсе. Кадр о паузе назван, и строка берёт текст оттуда.
        went = [_service_kind(call["messages"]) for call in _stub.CALLS]
        assert went == ["summary", None], went
        promised = [e["strategy"] for e in frames if e["event"] == "compressing"]
        assert promised == ["summary"], promised

        # **Сжатию память не достаётся ни одна**: попади она в пересказ,
        # и вернулась бы в промпт вторым экземпляром, да ещё искажённой.
        folding = _service_calls("summary")[-1]["messages"]
        assert not any("[долговременная память]" in m["content"] for m in folding), folding
        assert not any("о собеседнике: пишу на Kotlin" in m["content"] for m in folding), folding
        assert not any("цель: собрать ТЗ" in m["content"] for m in folding), folding

        # Память читается **один раз на обмен** и раздаётся троим — сборке
        # промпта и обоим слотам кадра `start`. Читай каждый хранилище сам,
        # запись от соседней вкладки попала бы в промпт, но не в номера.
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

        # --- 6. Три слоя одной ручкой ----------------------------------------
        layers = client.get(f"/api/agents/{both}/memory").json()
        assert layers["short_term"]["messages"] == len(REGISTRY.require(both).history), layers
        # Сводки — в краткосрочном разделе: сводка не запомненное, а чем
        # заменено не уехавшее. Выключи сворачивание — не пропадёт ничего.
        assert layers["short_term"]["summaries"], layers["short_term"]
        assert layers["short_term"]["summaries"][0]["upto"] == 2, layers["short_term"]
        assert "summaries" not in layers["working"], layers["working"]
        assert [(r["kind"], r["content"]) for r in layers["working"]["records"]] == [
            ("goal", "собрать ТЗ")
        ], layers["working"]
        assert [r["seq"] for r in layers["long_term"]["records"]] == [
            r["seq"] for r in client.get("/api/memory").json()["records"]
        ], layers["long_term"]
        # Слой общий: у соседнего чата он тот же, а первые два — свои.
        # Выключателя нет: врезка едет всегда, когда в слое что-то лежит.
        other = client.get(f"/api/agents/{bare}/memory").json()
        assert "enabled" not in other["long_term"], other["long_term"]
        assert other["long_term"]["records"] == layers["long_term"]["records"], other["long_term"]
        assert other["working"]["records"] == [], other["working"]

        # --- 7. Хранится врозь ------------------------------------------------
        store = REGISTRY.store
        columns = {r["name"] for r in store.conn.execute("PRAGMA table_info(memory)")}
        assert columns == {"seq", "kind", "content", "at"}, columns
        assert "session_id" not in columns, "у глобального слоя завёлся владелец"
        # Колонки авторства нет ни здесь, ни в рабочей памяти: писать в оба
        # слоя некому, кроме человека. Лишним полем в теле оно тоже
        # не проходит — сервер принимает ровно два.
        assert client.post(
            "/api/memory", json={"kind": "profile", "content": "х", "author": "human"}
        ).status_code == 400
        working_columns = {
            r["name"] for r in store.conn.execute("PRAGMA table_info(working_memory)")
        }
        assert working_columns == {"seq", "session_id", "kind", "content", "at"}, working_columns
        mine = client.post(
            "/api/memory", json={"kind": "profile", "content": "записал руками"}
        ).json()
        assert set(mine) == {"seq", "kind", "content", "at"}, mine
        was_agent = store.add_memory("knowledge", "это записал кто-то раньше")
        fixed = client.patch(
            f"/api/memory/{was_agent['seq']}", json={"content": "поправлено руками"}
        )
        assert fixed.status_code == 200, fixed.text
        assert fixed.json() == {**was_agent, "content": "поправлено руками",
                                "at": fixed.json()["at"]}, fixed.json()
        # Тип и здесь в одиночку, и текст остаётся прежним: слои правятся
        # одинаково, и правка, работающая в одном, разъехалась бы с соседним.
        typed = client.patch(f"/api/memory/{was_agent['seq']}", json={"kind": "decision"})
        assert typed.status_code == 200, typed.text
        assert typed.json()["kind"] == "decision", typed.json()
        assert typed.json()["content"] == "поправлено руками", typed.json()
        assert client.patch(f"/api/memory/{was_agent['seq']}", json={}).status_code == 400
        assert client.patch(f"/api/memory/{was_agent['seq']}", json={"kind": "х"}).status_code == 400
        # Лишнее поле в правке — 400 тем же `_record_body`: номер и время
        # выдаёт сервер, и приславший их просит не то, что ручка делает.
        assert client.patch(
            f"/api/memory/{was_agent['seq']}",
            json={"content": "х", "author": "agent"},
        ).status_code == 400
        assert client.patch(
            f"/api/memory/{was_agent['seq']}", json={"seq": 5}
        ).status_code == 400
        assert client.patch("/api/memory/9999", json={"content": "нет такой"}).status_code == 404
        assert client.delete(f"/api/memory/{was_agent['seq']}").status_code == 200
        assert client.delete(f"/api/memory/{mine['seq']}").status_code == 200
        spilled = _columns_holding(
            store.conn, "пишу на Kotlin", ("messages", "summaries", "working_memory")
        )
        assert not spilled, f"долговременная память утекла в чужие таблицы: {spilled}"

        # --- 8. Удаление по одной: номера не сдвигаются и не возвращаются -----
        # Память — список записей с идентичностью, а не снимок: перенумеруй
        # её, и вторая вкладка удалила бы по старому номеру чужую запись.
        gone = client.delete(f"/api/memory/{decision['seq']}")
        assert gone.status_code == 200 and gone.json() == {"deleted": decision["seq"]}, gone.text
        assert client.delete(f"/api/memory/{decision['seq']}").status_code == 404
        left = [r["seq"] for r in client.get("/api/memory").json()["records"]]
        assert left == [profile["seq"], knowledge["seq"]], left

        # И отдельно — про **последний** номер: его обычная `INTEGER PRIMARY
        # KEY` выдала бы заново, и разница видна только здесь.
        assert client.delete(f"/api/memory/{knowledge['seq']}").status_code == 200
        fresh = client.post("/api/memory", json={"kind": "knowledge", "content": "свежее"}).json()
        assert fresh["seq"] > knowledge["seq"], (fresh, knowledge)
        assert fresh["seq"] != decision["seq"], "номер удалённой записи выдан заново"

        # Ветвление память не копирует: слой глобальный. «Видна ли ветке
        # запись» — вопрос не тот: считаем **число** записей до и после.
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

    # --- 9. Файл: чат уходит, память остаётся; clear() замыкает круг ---------
    # Область у слоя вся база, а жизнь дольше разговора: запись обязана
    # пережить и чат, и перезапуск.
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

    # `forget()` забывает **разговор**, а долговременная память им не была:
    # забывший чат по-прежнему знает, на чём пишет собеседник.
    agent.forget()
    assert store.list_memory() == [kept], store.list_memory()
    assert "[долговременная память]" in agent.build_prompt("после forget")[0]["content"]

    with _restarted(store, agent.id) as (again, revived):
        assert again.list_memory() == [kept], again.list_memory()
        assert "[долговременная память]" in revived.build_prompt("после перезапуска")[0]["content"]

        # Агент без хранилища не падает — память у него просто пуста.
        homeless = Agent(AgentSpec(label="без базы", model="stub/model"))
        assert homeless.memory_items() == [], homeless.memory_items()
        assert homeless.prompt_slots()["memory_at"] is None, homeless.prompt_slots()
        assert homeless.build_prompt("вопрос") == [{"role": "user", "content": "вопрос"}]

        # И единственный путь, стирающий память, — служебная очистка базы:
        # забудь её там, и `kill_all()` оставлял бы врезку следующей проверке.
        again.clear()
        assert again.list_memory() == [], again.list_memory()
        assert revived.memory_items() == [], revived.memory_items()
        assert revived.working_items() == [], revived.working_items()
        assert revived.build_prompt("после очистки") == [
            {"role": "user", "content": "после очистки"}
        ], revived.build_prompt("после очистки")

    return (
        "пустая память неотличима от отсутствующей; тип записи без умолчания, "
        "шесть кривых тел дали 400; врезка ролью user с подписями, слот 1 "
        "с системным промптом и 0 без; врезок втроём — слоты 1, 2 и 3; сжатию "
        "память не досталась ни одна; за обмен она прочитана один раз; записи "
        "пережили forget() и переоткрытие файла, а clear() — нет"
    )


@check("память меняет ответ: тот же вопрос с врезкой и без неё расходится")
def check_memory_changes_answer():
    """«Как память влияет на ответы» — пункт задания, который просили именно
    **проверить**. Прочие проверки памяти смотрят на **запрос**, здесь —
    на **ответ**: тот же вопрос, те же настройки, пустой слой против
    непустого. Держится это на заглушке, отвечающей по содержимому запроса;
    моделью она не притворяется и обещает только то, что разница во врезке
    доезжает до ответа. Первый шаг — пустая память: утверждение о разнице
    обязано стоять на непустом значении с обеих сторон."""
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
        # Разные ответы сами по себе не доказывают ничего: разойтись они могли
        # бы и от вопроса, и от номера вызова. Поэтому сверяем сами запросы —
        # всё, кроме врезки памяти, совпадает в них слово в слово.
        assert [m for m in loud_prompt if "[долговременная память]" not in m["content"]] \
            == blank_prompt, (loud_prompt, blank_prompt)
        assert blank_prompt[-1]["content"] == question, blank_prompt[-1]
        assert loud_prompt[-1]["content"] == question, loud_prompt[-1]
        assert not any(
            "[долговременная память]" in m["content"] for m in blank_prompt
        ), blank_prompt

        # --- 5. И то же самое в рабочем слое -----------------------------------
        # Слои разные по области и сроку жизни, а показывают себя одинаково.
        # Запись здесь **своя**, под чатом, и соседнего чата не касается.
        task = new_agent(client, label="с задачей", system="СИС")
        client.post(
            f"/api/agents/{task}/working", json={"kind": "goal", "content": "пример на Kotlin"}
        )
        working_answer, working_prompt = answer(task, client)
        assert working_answer != empty_answer, working_answer
        assert "Kotlin" in working_answer, working_answer
        assert any("[факты о разговоре]" in m["content"] for m in working_prompt), working_prompt

        # --- 6. Ответ разный, а счёт обменов одинаковый ------------------------
        # Врезка памяти — часть промпта, а не лишний вызов: вызовов ровно
        # столько, сколько обменов, и служебных среди них нет ни одного.
        assert len(_stub.CALLS) == 3, len(_stub.CALLS)
        assert not _service_calls(), "за памятью сходили к модели"

    return (
        f"с записью — {loud!r}; без неё — {empty_answer!r}; "
        f"рабочая память меняет ответ так же — {working_answer!r}; "
        "запросы различаются одной врезкой"
    )


@check("профиль меняет ответ: один вопрос, два профиля — два разных ответа")
def check_profile_changes_answer():
    """«Ответы для разных профилей» — пункт задания, который просили именно
    **проверить**. Профиль от записи памяти отличается наклонением: память
    это факт («пишет на Kotlin»), профиль — распоряжение («отвечай кратко»),
    и потому едет системным сообщением.

    Порядок и есть проверка: пустой профиль (ни блока, ни системного
    сообщения — строже, чем у памяти, ведь оно сдвигает номера **всех**
    врезок); два разных профиля на один вопрос; ловушка — чат **без**
    системного промпта с непустым профилем, где ошибка расходится молча.
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
        # Сверяем сами запросы: всё, кроме системного сообщения, совпадает
        # в них слово в слово, а вопрос — тот же самый.
        assert short_prompt[1:] == long_prompt[1:] == blank_prompt, (short_prompt, long_prompt)
        assert short_prompt[0]["content"] != long_prompt[0]["content"], short_prompt

        # --- 6. Профиль едет **системным** сообщением, а не обычным -----------
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
        # Пустое поле уехало бы строкой «формат: » и заняло бы место
        # распоряжения. Системный промпт чата стоит в том же сообщении.
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
        # Системное сообщение заводится и у чата **без** системного промпта,
        # если профиль непуст. Забудь об этом — и все врезки уедут на единицу
        # молча: промпт соберётся верно, а подписи ролей встанут над чужим.
        naked = new_agent(client, label="без промпта, с профилем")
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{naked}/working", json={"kind": "goal", "content": "собрать ТЗ"})
        start = _frame(_frames(client, naked, question), "start")
        assert start["memory_at"] == 1 and start["working_at"] == 2, start
        assert start["resolved_messages"][0]["role"] == "system", start["resolved_messages"][0]

        # И обратно: сняли профиль — системного сообщения снова нет, номера
        # вернулись. Пустой профиль неотличим от отсутствовавшего.
        client.patch("/api/profile", json={"style": "", "context": ""})
        assert client.get("/api/profile").json() == {"profile": {}}, "профиль не снялся"
        bare = _frame(_frames(client, naked, question), "start")
        assert bare["memory_at"] == 0 and bare["working_at"] == 1, bare
        assert bare["resolved_messages"][0]["role"] == "user", bare["resolved_messages"][0]

        # --- 9. Профиль пишет только человек ----------------------------------
        # Шесть обменов позади, и ни один не дописал в профиль ни строки.
        # И лишнего вызова он не стоит — часть промпта, а не служебный вызов.
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


@check("токены служебного вызова попадают в итог по чату")
def check_service_tokens_counted():
    """Вызов на сжатие тоже уехал в модель и тоже оплачен: экономия,
    не вычитающая его, — враньё, поэтому он входит в `usage_total` отдельным
    слагаемым (в истории его нет, сам собой он туда не попадёт). Числа
    служебного вызова здесь заведомо больше всех остальных вместе взятых:
    потеряйся они, сумма разошлась бы на порядок."""
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
    """Смотрим не ответ ручки, а то, что реально ушло в модель. Три состояния
    поля: не задано — в теле его нет; задано — уехало ровно им; снято —
    исчезло снова. Клиентская половина — в `browser_check.js`."""
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
    """Три правила, без которых день не состоится: `require_parameters`
    (иначе провайдер молча проигнорирует temperature или stop), выключенное
    `context-compression` (иначе на окнах 8k он сам обрежет промпт
    посередине) и `usage: {include: true}` (иначе точных чисел не пришлёт
    никто). По телу запроса на всех путях: сообщение, перегенерация, чат
    с параметрами и чат со своим `extra_body`."""
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        # Память выключена у всех трёх: проверка перебирает пути **обмена**,
        # и служебные вызовы в него не входят.
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
        spec = AgentSpec(label="разбор", model="stub/thinking")
        events = asyncio.run(drain(llm.stream_completion(spec, prompt_override=[], **kwargs)))
    return next(e for e in events if e["type"] == "done")["metrics"]


def _streamed(lines, *, spec=None, **kwargs) -> tuple[list[dict], dict]:
    """Гоняет настоящий `stream_completion` на подменённом транспорте и отдаёт
    **все** события вместе с телом запроса: про вызовы спрашивается, сколько
    событий и в каком порядке. Утверждений здесь нет."""
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
    """Главные числа приезжают последним чанком OpenRouter, разбирает их
    `app/llm.py`. Заодно время до первого токена: `ttft_ms` на первом токене
    **ответа**, `first_token_ms` на первом вообще — на думающей модели
    это разные моменты."""
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
    # Равенство здесь так же плохо: значит, размышление записали в молчание.
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
    """Ноль и «неизвестно» — разные вещи. Провайдер вправе смолчать о цене:
    такая реплика в сумму не входит ни слагаемым, ни нулём, а чат, где чисел
    не принёс никто, даёт `None` — прочерк, а не «0 токенов»."""
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


# --- День 13: транспорт вызовов инструментов -----------------------------------


@check("вызовы инструментов: куски склеены по index, событие одно и раньше done")
def check_tool_calls_transport():
    """Разбор потока с вызовами инструментов — настоящим `stream_completion`
    на кусках той формы, что описана в машинной схеме OpenRouter, а не в их
    прозе; последний раздел — на **живом** ответе `openai/gpt-4o-mini`.

    Пять свойств формы ломают наивный разбор: `finish_reason` лежит
    на `choices[0]`, а не в `delta`; у фрагмента вызова обязателен только
    `index`, а `arguments` приезжают обрывками; `finish_reason: "tool_calls"`
    приходит дважды; текст и вызов приезжают вместе; `arguments` вправе
    оказаться битым JSON. Отсюда и страховка: событие держится на наличии
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
                  function={"name": "finish_task", "arguments": '{"ok":'}),
            piece(0, id="call_a", type="function",
                  function={"name": "update_plan", "arguments": '{"steps":'}),
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
        {"id": "call_a", "name": "update_plan", "arguments": '{"steps": ["раз", "два"]}'},
        {"id": "call_b", "name": "finish_task", "arguments": '{"ok": true}'},
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
                            "content": "составлю план.",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_m",
                                    "function": {
                                        "name": "update_plan",
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
    assert mixed[-1]["text"] == "Сейчас составлю план.", mixed[-1]
    mixed_calls = mixed[mixed_kinds.index("tool_calls")]["calls"]
    assert mixed_calls == [
        {"id": "call_m", "name": "update_plan", "arguments": '{"steps": []}'}
    ], mixed_calls

    # --- страховка: вызовы есть, а причина приехала «stop» -------------------
    saved, _ = _streamed(
        _sse_chunks(
            piece(0, id="call_s", function={"name": "finish_task", "arguments": "{}"}),
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
                                    "function": {"name": "finish_task", "arguments": "{}"},
                                },
                                {
                                    "index": 0,
                                    "id": "",
                                    "function": {"name": "update_plan", "arguments": "{}"},
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
            piece(0, id="call_j", function={"name": "update_plan", "arguments": torn}),
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

    # --- живой ответ gpt-4o-mini: 118 кусков, склеенные аргументы -------------
    # Сцена на **настоящих** данных, и главное её открытие — последнее
    # утверждение раздела: модель вернула только вызов и ни слова текста,
    # хотя её прямо просили «кратко перечисли шаги».
    #
    # Прореживать куски нельзя: обрывок `arguments` несут 116 из 118, и
    # выбрось хоть один — `json.loads` упадёт. Поэтому склеенная строка взята
    # из живого ответа дословно, а режется здесь же теми же размерами.
    # Выброшены только конверты SSE, одинаковые в каждом куске.
    REAL_ARGUMENTS = (
        '{"steps":[{"title":"Создать дизайн экрана оплаты с формой для вв'
        'ода данных карты","status":"pending"},{"title":"Реализовать вали'
        'дацию полей формы (номер карты, дата истечения, CVV)","status":"'
        'pending"},{"title":"Настроить взаимодействие с бэкендом для отпр'
        'авки данных","status":"pending"},{"title":"Обработать ответ от б'
        'экенда и отобразить пользователю результат","status":"pending"},'
        '{"title":"Провести тестирование экрана оплаты","status":"pending'
        '"}]}'
    )
    REAL_SIZES = (3, 2, 5, 7, 4, 6, 1, 8, 13, 10)
    """Размеры обрывков, какими их резал провайдер: в живом ответе их
    от 1 до 13 символов, чаще всего 3. Режем по кругу — важна не
    последовательность длин, а то, что границы обрывков рвут и ключи, и
    русские слова в значениях."""

    def real_fragments(text):
        """Строка аргументов, нарезанная на обрывки, — как их присылает
        провайдер. Сцену ставит, ничего не утверждает."""
        out, at, step = [], 0, 0
        while at < len(text):
            size = REAL_SIZES[step % len(REAL_SIZES)]
            out.append(text[at : at + size])
            at, step = at + size, step + 1
        return out

    real_pieces = real_fragments(REAL_ARGUMENTS)
    real, _ = _streamed(
        _sse_chunks(
            # Первый кусок — настоящий: `id`, `type`, имя и **пустые**
            # аргументы, а `content` в нём `null`, а не строка.
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": None,
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_CQLQ35QeDwFtj0og6Cg1AJ2C",
                                    "type": "function",
                                    "function": {"name": "update_plan", "arguments": ""},
                                }
                            ],
                        },
                        "finish_reason": None,
                        "native_finish_reason": None,
                    }
                ]
            },
            *(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": None,
                                "role": "assistant",
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": piece}}
                                ],
                            },
                            "finish_reason": None,
                            "native_finish_reason": None,
                        }
                    ]
                }
                for piece in real_pieces
            ),
            # Предпоследний и последний — тоже настоящие: причина приезжает
            # дважды, и во второй раз вместе с usage.
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "", "role": "assistant"},
                        "finish_reason": "tool_calls",
                        "native_finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "", "role": "assistant"},
                        "finish_reason": "tool_calls",
                        "native_finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 341,
                    "completion_tokens": 123,
                    "total_tokens": 464,
                    "cost": 0.00012495,
                    "is_byok": False,
                    "prompt_tokens_details": {
                        "cached_tokens": 0, "cache_write_tokens": 0,
                        "audio_tokens": 0, "video_tokens": 0,
                    },
                    "cost_details": {
                        "upstream_inference_cost": 0.00012495,
                        "upstream_inference_prompt_cost": 5.115e-05,
                        "upstream_inference_completions_cost": 7.38e-05,
                    },
                    "completion_tokens_details": {
                        "reasoning_tokens": 0, "image_tokens": 0, "audio_tokens": 0,
                    },
                },
            },
        )
    )
    real_kinds = [e["type"] for e in real]
    assert real_kinds.count("tool_calls") == 1, real_kinds
    real_call = real[real_kinds.index("tool_calls")]["calls"]
    assert len(real_call) == 1 and real_call[0]["name"] == "update_plan", real_call
    assert real_call[0]["id"] == "call_CQLQ35QeDwFtj0og6Cg1AJ2C", real_call[0]
    # Аргументы склеились в разбираемый JSON: обрывки рвали и ключи,
    # и русские слова — потеряй разбор хоть один, `json.loads` упал бы.
    real_args = json.loads(real_call[0]["arguments"])
    assert [step["status"] for step in real_args["steps"]] == ["pending"] * 5, real_args
    assert real_args["steps"][0]["title"].startswith("Создать дизайн"), real_args
    real_done = real[-1]
    assert real_done["type"] == "done", real_kinds
    assert real_done["metrics"]["prompt_tokens"] == 341, real_done["metrics"]
    assert real_done["metrics"]["completion_tokens"] == 123, real_done["metrics"]
    # И **главное открытие живого прогона**: текста в ответе нет ни одного
    # символа. Модель просили «кратко перечисли шаги» — она вернула только
    # вызов. Отсюда растёт весь следующий дифф: без второго оборота человек
    # не увидит ни слова.
    assert real_done["text"] == "", repr(real_done["text"])
    assert real_kinds.count("delta") == 0, real_kinds

    # --- объявление инструментов в теле запроса ------------------------------
    plan_tool = {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": "переписать список шагов задачи",
            "parameters": {"type": "object", "properties": {"steps": {"type": "array"}}},
        },
    }
    plain = _sse_chunks({"choices": [{"delta": {"content": "ок"}, "finish_reason": "stop"}]})

    _, declared = _streamed(plain, tools=[plan_tool])
    assert declared["tools"] == [plan_tool], declared.get("tools")
    # **Без просьбы** `tool_choice` не отправляется: обычно звать инструмент
    # или ответить словами решает модель. Просьба приезжает сверху и только
    # на планировании (`Agent.turn_choice`), а транспорт её не выдумывает.
    assert "tool_choice" not in declared, declared

    forced = {"type": "function", "function": {"name": "update_plan"}}
    _, forcing = _streamed(plain, tools=[plan_tool], tool_choice=forced)
    assert forcing["tool_choice"] == forced, forcing.get("tool_choice")
    # **Без `tools`** — не отправляется, что бы ни передали: поле без
    # объявленных функций провайдер отвергнет, и запрос не состоится вовсе.
    # Утверждение об отсутствии стоит там, где присутствие достижимо: тот же
    # `tool_choice`, та же строка выше — и разница только в `tools`.
    _, lonely = _streamed(plain, tool_choice=forced)
    assert "tools" not in lonely and "tool_choice" not in lonely, lonely
    _, empty_forced = _streamed(plain, tools=[], tool_choice=forced)
    assert "tool_choice" not in empty_forced, empty_forced
    # Три правила тела на месте и у принуждающего запроса.
    assert not _body_rules(forcing), _body_rules(forcing)

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
        tools=[plan_tool],
    )
    assert overridden["tools"] == [mine], overridden["tools"]

    # И `tool_choice` перебивает — он часть тела, а не исключение из правила.
    _, unforced = _streamed(
        plain,
        spec=AgentSpec(
            label="своё тело", model="stub/tools", extra_body={"tool_choice": "none"}
        ),
        tools=[plan_tool],
        tool_choice=forced,
    )
    assert unforced["tool_choice"] == "none", unforced["tool_choice"]

    # --- незнакомый тип события обмен переживает ------------------------------
    # Перебор событий в `ask` обязан пережить чужой тип молча — иначе
    # следующий PR уронит обмен раньше, чем научится исполнять вызов.
    _stub.install(
        reply="ок",
        tool_calls=[{"id": "call_a", "name": "update_plan", "arguments": "{}"}],
    )
    with TestClient(main.app) as client:
        chat = new_agent(client)
        answer = client.post(f"/api/agents/{chat}/messages", json={"text": "раз"})
        assert answer.status_code == 200, answer.text
        frames = sse(answer.text)
        history = client.get(f"/api/agents/{chat}").json()["transcript"]
    frame_kinds = [f["event"] for f in frames]
    # Наружу событие не уехало: `ask` перечисляет известные имена и чужое
    # не пересылает. Появись оно в ленте — клиент этого дня не знал бы,
    # что с ним делать.
    assert "tool_calls" not in frame_kinds, frame_kinds
    assert frame_kinds[-1] == "done" and frames[-1]["committed"] is True, frames[-1]
    assert [m["role"] for m in history] == ["user", "assistant"], history
    assert history[-1]["content"] == "ок", history[-1]

    return (
        "два вызова собраны из обрывков вперемешку, событие одно на две причины "
        "и раньше done; текст и вызов вместе; страховка на «stop»; id без "
        "провайдера — call_{index}; битый JSON отдан строкой; живой ответ "
        f"gpt-4o-mini собрался из {len(real_pieces)} обрывков в план на 5 шагов "
        "и не принёс ни символа текста; tools объявлены, "
        "а без них ключа в теле нет и extra_body их перебивает; обмен "
        "переживает незнакомый тип события и записывается"
    )


@check("состояние задачи: этап вычисляется из плана, переходы решает один код")
def check_task_state_machine():
    """Задание дня: «состояние задачи как конечный автомат — этап, текущий
    шаг, ожидаемое действие; пауза на любом этапе и продолжение без повторных
    объяснений». Устройство одно и оно же ответ на все три поля: **план
    и есть состояние**, а всё остальное из него **вычисляется** (`stage_of`).

    Разделы: этап и текущий шаг (шесть этапов на шести планах, разбор
    упорядоченный); выключено по умолчанию — в промпте не меняется ни слова;
    включено — правило этапа **системным** сообщением, список шагов `user`,
    и блок едет даже с пустым списком; переходы — один `plan.apply`
    на инструмент и на кнопку, а отказ у него директива, а не код ошибки;
    пауза — распоряжение человека, и держится она **кодом**, а снятие
    возвращает тот же этап само собой; объявление инструментов.
    """
    from app import plan as taskplan
    from app.agent import Agent

    def steps(*pairs):
        return [{"title": title, "status": status} for title, status in pairs]

    def plan_of(step_list, **flags):
        return {**taskplan.empty(), "steps": step_list, **flags}

    def prompt_parts(chat):
        """Системное сообщение и блок задачи — по номеру слота, который
        назвал сам агент. Ставит сцену, не измеряет её: и номер, и содержимое
        сверяет проверка."""
        messages, slots, _ = chat.prompt_head()
        return messages[0]["content"], messages[slots["plan_at"]]["content"]

    THREE = steps(("собрать требования", "pending"), ("схема базы", "pending"),
                  ("экран оплаты", "pending"))

    # --- этап и текущий шаг: шесть планов, шесть этапов --------------------
    ladder = {
        "planning": (plan_of([]), None),
        "approval": (plan_of(THREE), 0),
        "execution": (
            plan_of(steps(("раз", "done"), ("два", "in_progress"), ("три", "pending")),
                    approved=True),
            1,
        ),
        "validation": (
            plan_of(steps(("раз", "done"), ("два", "done")), approved=True), None
        ),
        "done": (
            plan_of(steps(("раз", "done"), ("два", "done")), approved=True, finished=True),
            None,
        ),
        "paused": (
            plan_of(steps(("раз", "done"), ("два", "in_progress")), approved=True, paused=True),
            1,
        ),
    }
    read = {name: taskplan.stage_of(plan) for name, (plan, _) in ladder.items()}
    assert read == {name: (name, current) for name, (_, current) in ladder.items()}, read
    # Все шесть этапов продукта прошли через разбор: этап, до которого
    # не дотянулась ни одна строка, вычислялся бы как попало.
    assert set(read) == set(taskplan.STAGES), (set(read), set(taskplan.STAGES))

    # Текущий — тот, что `in_progress`, даже если перед ним есть `pending`;
    # его нет — первый не-`done`.
    running = plan_of(steps(("раз", "pending"), ("два", "in_progress"), ("три", "pending")),
                      approved=True)
    assert taskplan.stage_of(running) == ("execution", 1), taskplan.stage_of(running)
    idle = plan_of(steps(("раз", "done"), ("два", "pending"), ("три", "pending")), approved=True)
    assert taskplan.stage_of(idle) == ("execution", 1), taskplan.stage_of(idle)

    # Разбор **упорядоченный**: план подходит и под `approval`, и под
    # `validation` сразу. Дай он `validation` — правило велело бы звать
    # `finish_task`, а тот на неутверждённом плане отказан, и кнопка
    # «Утвердить» ответила бы 409 там, где утвердить обязаны дать.
    both = plan_of(steps(("раз", "done"), ("два", "done")))
    assert taskplan.stage_of(both) == ("approval", None), taskplan.stage_of(both)
    approved_both, _ = taskplan.apply(both, "approve")
    assert taskplan.stage_of(approved_both)[0] == "validation", approved_both

    # `finish_task` на **неутверждённом** плане: сцена та же — все шаги
    # `done`, а кнопки не было. Одним вызовом модель закрыла бы задачу,
    # которую человек не утверждал, и сторож у этого один.
    try:
        taskplan.apply(both, "finish_task", {"problems": []})
        raise AssertionError("finish_task прошёл на неутверждённом плане")
    except taskplan.PlanError as exc:
        assert "план ещё не утверждён" in str(exc), str(exc)
    assert both["finished"] is False, both

    # Незнакомое действие отказано и в самом `apply`: ворота стоят
    # в `run_tool` (там имена берутся из объявления), но источник истины
    # переходов обязан отвечать за себя сам — звать его будет не только
    # обмен, а и каждая новая кнопка.
    try:
        taskplan.apply(plan_of(THREE), "плана_нет", {})
        raise AssertionError("незнакомое действие прошло через apply")
    except taskplan.PlanError as exc:
        assert "такого инструмента нет" in str(exc), str(exc)

    # Метки шагов — то, чем план **выглядит** и в промпте, и в ответе каждого
    # вызова: одной `plan_lines` построены оба. Сотри их, и в каждом промпте
    # всё выглядело бы несделанным, а текущий шаг не назывался бы вовсе.
    assert taskplan.plan_lines(ladder["execution"][0]).splitlines() == [
        "план утверждён: да",
        "1. [x] раз",
        "2. [>] два ← в работе",
        "3. [ ] три",
    ], taskplan.plan_lines(ladder["execution"][0])

    # Пауза ложится поверх **любого** этапа, и проверяется на двух разных —
    # работа в разгаре и все шаги сделаны. Снятие возвращает тот же этап
    # само собой: он вычисляется из списка, а список не менялся.
    for stage in ("execution", "validation"):
        was, current = ladder[stage]
        held, _ = taskplan.apply(was, "pause")
        assert taskplan.stage_of(held) == ("paused", current), (stage, held)
        back, _ = taskplan.apply(held, "resume")
        assert taskplan.stage_of(back) == (stage, current), (stage, back)

    # --- выключено по умолчанию: в промпте не меняется ни слова -------------
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        plain = new_agent(client, system="СИС")
        body = client.get(f"/api/agents/{plain}").json()
        assert body["workflow"] == "off", body["workflow"]
        # Ключ `plan` есть и у чата, которого никто не трогал: `workflow`
        # и `plan` — два поля одного механизма, и разъехаться внутри одного
        # тела они не вправе.
        assert body["plan"]["stage"] == "planning", body["plan"]
        start = _frame(_frames(client, plain, "вопрос"), "start")
        assert [m["role"] for m in start["resolved_messages"]] == ["system", "user"], start
        assert start["resolved_messages"][0]["content"] == "СИС", start["resolved_messages"][0]
        assert all(start[name] is None for name in agent_module.PROMPT_SLOTS), start
        assert not any("[задача]" in m["content"] for m in start["resolved_messages"]), start

        # --- ручка: своё значение принимается, чужое — 400 ------------------
        turned = client.patch(f"/api/agents/{plain}", json={"workflow": "plan"})
        assert turned.status_code == 200 and turned.json()["workflow"] == "plan", turned.text
        assert client.get(f"/api/agents/{plain}").json()["workflow"] == "plan", "не сохранилось"
        wrong = client.patch(f"/api/agents/{plain}", json={"workflow": "ведение"})
        assert wrong.status_code == 400, wrong.text
        assert "off" in wrong.text and "plan" in wrong.text, wrong.text

        # --- объявление: включённому чату инструменты объявлены -------------
        # Снимается на **обоих** чатах: «ключа нет» на выключенном стоит там,
        # где присутствие достижимо — на включённом ключ есть.
        live = REGISTRY.require(plain)
        assert len(live.tool_specs()) == 2, live.tool_specs()
        _frames(client, plain, "а теперь с планом")
        assert _stub.CALLS[-1]["payload"]["tools"] == live.tool_specs(), _stub.CALLS[-1]
        off = new_agent(client, workflow="off")
        _frames(client, off, "а этому нечего вести")
        assert "tools" not in _stub.CALLS[-1]["payload"], _stub.CALLS[-1]["payload"].keys()

        # --- кнопки человека: этап решает один код --------------------------
        chat = new_agent(client, system="СИС", workflow="plan")
        agent = REGISTRY.require(chat)
        assert client.post(f"/api/agents/{chat}/plan/approve").status_code == 409, (
            "утвердили план, которого нет"
        )
        ok, written = agent.run_tool("update_plan", json.dumps({"steps": THREE},
                                                               ensure_ascii=False))
        assert ok, written
        # Результат инструмента — **весь список**, а не «ок»: он уезжает
        # модели в контекст и работает её рабочей памятью по задаче.
        for number, step in enumerate(THREE, start=1):
            assert f"{number}. [ ] {step['title']}" in written, written
        assert "План записан: 3 шагов" in written and "жди кнопки" in written, written
        assert REGISTRY.store.load_plan(chat)["steps"] == THREE, REGISTRY.store.load_plan(chat)

        # --- ворота: пять человеческих действий модели не объявлены ---------
        # Сцена выбрана так, чтобы отказ **стерёг**: план ждёт кнопки, и
        # пройди `approve` — модель утвердила бы себе план сама. На пустом
        # плане вызов отказали бы по этапу, то есть чужим сторожем.
        assert agent.plan_view()["stage"] == "approval", agent.plan_view()
        untouched = json.dumps(agent.plan, ensure_ascii=False, sort_keys=True)
        for human in ("approve", "reopen", "pause", "resume", "reset"):
            moved, refused = agent.run_tool(human, "{}")
            assert not moved, (human, refused)
            assert "это не инструмент модели, а кнопка человека" in refused, refused
            assert json.dumps(agent.plan, ensure_ascii=False, sort_keys=True) == untouched, (
                human, agent.plan
            )
        assert agent.plan_view()["stage"] == "approval", agent.plan_view()

        # И второй сторож рядом: чату с выключенным процессом инструмент
        # плана не пишет. Записанный молча, план достался бы чату, который
        # о нём не спрашивал и в промпт его не берёт.
        muted = new_agent(client, workflow="off")
        moved, refused = REGISTRY.require(muted).run_tool(
            "update_plan", json.dumps({"steps": THREE}, ensure_ascii=False)
        )
        assert not moved and "рабочий процесс в этом чате выключен" in refused, refused
        assert REGISTRY.store.load_plan(muted) is None, REGISTRY.store.load_plan(muted)

        approve = client.post(f"/api/agents/{chat}/plan/approve")
        assert approve.status_code == 200, approve.text
        assert approve.json()["plan"]["stage"] == "execution", approve.json()
        assert client.post(f"/api/agents/{chat}/plan/approve").status_code == 409, (
            "утвердили дважды"
        )
        assert client.post(f"/api/agents/{chat}/plan/reopen").status_code == 409, (
            "переоткрыли незавершённую"
        )
        head, block = prompt_parts(agent)
        assert "шаг 1 из 3" in head and "«собрать требования»" in head, head
        assert "план утверждён: да" in block, block

        # --- пауза: любой этап, и держится она кодом ------------------------
        paused = client.post(f"/api/agents/{chat}/plan/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["plan"]["stage"] == "paused", paused.json()
        assert paused.json()["plan"]["current"] == 0, paused.json()
        rule, block = prompt_parts(agent)
        assert "ПРИОСТАНОВЛЕНА" in rule and "даже если тебя просят" in rule, rule
        assert "задача на паузе: да" in block, block

        frozen = json.dumps(agent.plan, ensure_ascii=False, sort_keys=True)
        for name, args in (
            ("update_plan", {"steps": steps(("вместо плана", "done"))}),
            ("finish_task", {"problems": []}),
        ):
            moved, refusal = agent.run_tool(name, json.dumps(args, ensure_ascii=False))
            assert not moved, (name, refusal)
            # Формула отказа — **всеми четырьмя частями**: что не выполнено
            # и почему, что запрещено утверждать, что допустимо взамен и
            # каков план сейчас. Причина и врезанный план различены нарочно:
            # слова «задача на паузе» стоят и в причине.
            assert refusal.startswith(
                f"{name} НЕ выполнен, состояние не изменилось: задача на паузе."
            ), refusal
            assert "НЕ утверждай, что это сделано." in refusal, refusal
            assert "Допустимо: дождаться, пока человек снимет паузу." in refusal, refusal
            assert "\n1. [ ] собрать требования" in refusal, refusal
        assert json.dumps(agent.plan, ensure_ascii=False, sort_keys=True) == frozen, agent.plan

        assert client.post(f"/api/agents/{chat}/plan/pause").status_code == 409, "пауза дважды"
        resumed = client.post(f"/api/agents/{chat}/plan/resume")
        assert resumed.status_code == 200, resumed.text
        # Продолжение без повторных объяснений: этап вернулся тот же, что был
        # до паузы, сам собой — он вычисляется из списка, а список не менялся,
        # и помнить, куда возвращаться, негде и не нужно.
        assert resumed.json()["plan"]["stage"] == "execution", resumed.json()
        assert client.post(f"/api/agents/{chat}/plan/resume").status_code == 409, (
            "сняли паузу, которой не было"
        )

        # --- finish_task: судья предъявляет улику ---------------------------
        moved, refusal = agent.run_tool("finish_task", json.dumps({"problems": []}))
        assert not moved, refusal
        assert "НЕ выполнен" in refusal and "НЕ утверждай" in refusal, refusal
        assert agent.plan["finished"] is False, agent.plan

        done_all = json.dumps({"steps": [{**step, "status": "done"} for step in THREE]},
                              ensure_ascii=False)
        assert agent.run_tool("update_plan", done_all)[0], "план не записался"
        assert agent.plan_view()["stage"] == "validation", agent.plan_view()
        # Непустой перечень проблем задачу **не** завершает: это и есть улика
        # вместо вердикта.
        moved, verdict = agent.run_tool(
            "finish_task", json.dumps({"problems": ["нет валидации карты", "нет теста оплаты"]},
                                      ensure_ascii=False)
        )
        assert moved, verdict
        assert "Записано проблем: 2" in verdict and "Задача НЕ завершена" in verdict, verdict
        assert agent.plan["finished"] is False, agent.plan
        assert agent.plan_view()["stage"] == "validation", agent.plan_view()

        assert agent.run_tool("finish_task", json.dumps({"problems": []}))[0], "не завершилась"
        assert agent.plan_view()["stage"] == "done", agent.plan_view()
        rule, block = prompt_parts(agent)
        # Правило этапа `done` непустое: прошлый раз завершённая задача
        # оставалась без распоряжения вовсе, и модель продолжала править план.
        assert "План менять нельзя" in rule, rule
        assert "задача завершена: да" in block, block
        # Пауза при завершённой задаче отказана: приостанавливать нечего.
        assert client.post(f"/api/agents/{chat}/plan/pause").status_code == 409, (
            "приостановили завершённую"
        )
        moved, refusal = agent.run_tool("update_plan", json.dumps({"steps": THREE},
                                                                  ensure_ascii=False))
        assert not moved and "уже завершена" in refusal, refusal
        assert [s["status"] for s in agent.plan["steps"]] == ["done"] * 3, agent.plan

        reopened = client.post(f"/api/agents/{chat}/plan/reopen")
        assert reopened.status_code == 200, reopened.text
        # Шаги остаются сделанными: переоткрыли не для того, чтобы делать
        # всё заново, а потому что в сделанном что-то не так.
        assert reopened.json()["plan"]["stage"] == "validation", reopened.json()
        assert [s["status"] for s in reopened.json()["plan"]["steps"]] == ["done"] * 3, (
            reopened.json()
        )

        # --- сброс: на непустом плане с поднятыми флажками ------------------
        assert agent.run_tool("update_plan", done_all)[0], "план не записался"
        client.post(f"/api/agents/{chat}/plan/pause")
        assert agent.plan["approved"] and agent.plan["paused"] and agent.plan["steps"], agent.plan
        reset = client.post(f"/api/agents/{chat}/plan/reset")
        assert reset.status_code == 200, reset.text
        assert reset.json()["plan"] == {
            "steps": [], "approved": False, "finished": False, "paused": False,
            "stage": "planning", "current": None,
        }, reset.json()["plan"]
        assert REGISTRY.store.load_plan(chat) is None, REGISTRY.store.load_plan(chat)

        # --- отказы: у каждой причины свой текст ----------------------------
        rejected: dict = {}
        names = ", ".join(taskplan.TOOL_NAMES)
        for reason, name, args, allowed in (
            ("пусто", "update_plan", {"steps": []}, taskplan.ALLOWED["update_plan"]),
            ("двое в работе", "update_plan",
             {"steps": steps(("раз", "in_progress"), ("два", "in_progress"))},
             taskplan.ALLOWED["update_plan"]),
            ("чужой статус", "update_plan", {"steps": [{"title": "раз", "status": "готово"}]},
             taskplan.ALLOWED["update_plan"]),
            ("без заголовка", "update_plan", {"steps": [{"title": "", "status": "pending"}]},
             taskplan.ALLOWED["update_plan"]),
            ("чужое имя", "плана_нет", {"steps": THREE}, f"звать можно {names}"),
        ):
            moved, text = agent.run_tool(name, json.dumps(args, ensure_ascii=False))
            assert not moved, (reason, text)
            assert text.startswith(f"{name} НЕ выполнен, состояние не изменилось: "), text
            assert "НЕ утверждай, что это сделано." in text, (reason, text)
            # Выход — **дословно** из таблицы, а не пересказ: её семь строк
            # иначе не названы ни одним утверждением, хотя формулу отказа
            # CLAUDE.md цитирует целиком.
            assert f"Допустимо: {allowed}." in text, (reason, text)
            assert text.endswith("Текущий план:\nплан утверждён: нет\nшагов ещё нет"), text
            assert agent.plan["steps"] == [], (reason, agent.plan)
            rejected[reason] = text
        # Причины называют себя по-разному: схлопни любую ветку в общую —
        # и модель правила бы наугад, не зная, что именно не подошло.
        assert len(set(rejected.values())) == len(rejected), rejected

        # Аргументы, которые не разобрались: исключения наружу нет, план цел.
        moved, torn = agent.run_tool("update_plan", '{"steps": [{"title": "раз"')
        assert not moved and "НЕ выполнен" in torn, torn
        assert agent.plan == taskplan.empty(), agent.plan

        # --- список короче утверждённого: утверждение снимается -------------
        # Иначе так: человек утвердил три шага, модель присылает один
        # со статусом `done`. `approved` остался бы, этап стал бы
        # `validation`, и следующий вызов закрыл бы задачу, которой никто
        # не делал.
        assert agent.run_tool("update_plan", json.dumps({"steps": THREE},
                                                        ensure_ascii=False))[0]
        assert client.post(f"/api/agents/{chat}/plan/approve").status_code == 200
        moved, shorter = agent.run_tool(
            "update_plan",
            json.dumps({"steps": [{"title": "собрать требования", "status": "done"}]},
                       ensure_ascii=False),
        )
        assert moved, shorter
        assert agent.plan["approved"] is False, agent.plan
        assert agent.plan_view()["stage"] == "approval", agent.plan_view()
        # И это **названо**: молча вернуть план к ожиданию кнопки значило бы
        # оставить модель ждать неизвестно чего.
        assert "Шагов стало меньше, чем утверждал человек" in shorter, shorter
        # Переименование утверждения не снимает: у шага нет идентификатора,
        # и сверка по заголовкам читала бы правку формулировки как «удалили
        # один, добавили другой».
        assert client.post(f"/api/agents/{chat}/plan/approve").status_code == 200
        assert agent.run_tool(
            "update_plan",
            json.dumps({"steps": [{"title": "собрать требования к оплате", "status": "done"}]},
                       ensure_ascii=False),
        )[0]
        assert agent.plan["approved"] is True, agent.plan

        # --- запись в хранилище раньше памяти -------------------------------
        # Занятая база — путь штатный. Упади запись после присваивания,
        # модель получила бы отказ «состояние не изменилось» с уже
        # изменённым планом под ним.
        assert agent.plan_view()["stage"] == "validation", agent.plan_view()
        kept = json.dumps(agent.plan, ensure_ascii=False, sort_keys=True)
        with patch.object(Store, "save_plan", side_effect=RuntimeError("база занята")):
            moved, failed = agent.run_tool("finish_task", json.dumps({"problems": []}))
            assert not moved, failed
            # И кнопка так же: пауза на этом этапе законна, и упади запись —
            # человек увидит ошибку, а план в памяти обязан остаться
            # прежним, иначе следующий обмен уедет приостановленным
            # по состоянию, которого в файле нет.
            try:
                agent.pause_plan()
                raise AssertionError("пауза прошла при упавшей записи")
            except RuntimeError:
                pass
        assert json.dumps(agent.plan, ensure_ascii=False, sort_keys=True) == kept, agent.plan

        # --- отказ обязан называть выход, которого ещё не сделали -----------
        # До причины «problems не список» доходят с утверждённым планом
        # и всеми шагами `done`: общий выход советовал бы уже сделанное,
        # а директива без выхода — тупик, обходимый объявлением успеха.
        moved, stuck = agent.run_tool(
            "finish_task", json.dumps({"problems": "нет проблем"}, ensure_ascii=False)
        )
        assert not moved, stuck
        assert "передай problems массивом строк" in stuck, stuck
        assert taskplan.ALLOWED["finish_task"] not in stuck, stuck

        # --- список слева: ключ `plan` есть и у выгруженного чата ------------
        # Список строится двумя ветвями кода, и не проверь холодную — сразу
        # после перезапуска он приезжал бы с `workflow: "plan"` и **без**
        # всякого `plan`: два поля одного механизма в одном теле.
        assert client.post(f"/api/agents/{chat}/plan/reset").status_code == 200
        assert agent.run_tool("update_plan", json.dumps({"steps": THREE},
                                                        ensure_ascii=False))[0]
        assert REGISTRY._unload(chat) is True, "чат не был живым"
        cold = {entry["id"]: entry for entry in client.get("/api/agents").json()["agents"]}
        assert cold[chat]["workflow"] == "plan", cold[chat]
        assert "plan" in cold[chat] and cold[chat]["plan"] is None, cold[chat]
        # А открытый чат поднимает план из базы — тем же путём, каким
        # поднимает историю.
        opened = client.get(f"/api/agents/{chat}").json()
        assert opened["plan"]["steps"] == THREE, opened["plan"]
        assert opened["plan"]["stage"] == "approval", opened["plan"]

    # --- четыре врезки разом: блок задачи стоит между памятью и сводкой -----
    _stub.install(reply=_service_aware)
    path = _temp_db("task-slots")
    store = Store(path).init()
    store.add_memory("knowledge", "человек пишет на Kotlin")
    full = Agent(
        AgentSpec(label="задача", model="stub/model", system="СИС", workflow="plan",
                  strategy="summary", keep_last=2, compress_every=2),
        store=store,
    )
    full.add_working_record("goal", "собрать ТЗ")
    _ask(full, 3)
    assert full.summaries, "сводка не собралась — сцена без четвёртой врезки"

    frames = asyncio.run(drain(full.ask("а теперь вопрос")))
    start = next(event for event in frames if event["type"] == "start")
    prompt = start["resolved_messages"]
    slots = {name: start[name] for name in agent_module.PROMPT_SLOTS}
    assert slots == {"memory_at": 1, "working_at": 2, "plan_at": 3, "summary_at": 4}, slots
    # Номер каждой врезки сходится с промптом: врезка, чей слот посчитали
    # суммой предыдущих вместо длины собранного начала, подписала бы чужое
    # сообщение — и заметить это было бы неоткуда.
    assert "[долговременная память]" in prompt[slots["memory_at"]]["content"], prompt
    assert "[факты о разговоре]" in prompt[slots["working_at"]]["content"], prompt
    assert prompt[slots["plan_at"]]["content"].startswith("[задача]"), prompt
    assert "пересказ начала разговора" in prompt[slots["summary_at"]]["content"], prompt
    # Блок едет **даже с пустым списком**: для модели это самая нужная
    # новость — без него она не знает, что план вообще ведётся.
    assert "шагов ещё нет" in prompt[slots["plan_at"]]["content"], prompt[slots["plan_at"]]
    assert prompt[slots["plan_at"]]["role"] == "user", prompt[slots["plan_at"]]
    # Правило этапа — в **системном** сообщении, и второго системного нет:
    # правило это распоряжение, а список шагов — сведения.
    assert [m["role"] for m in prompt].count("system") == 1, [m["role"] for m in prompt]
    assert prompt[0]["role"] == "system" and "Этап: планирование" in prompt[0]["content"], prompt[0]
    assert prompt[0]["content"].startswith("СИС\n\n"), prompt[0]["content"]

    # --- план переживает перезапуск процесса --------------------------------
    assert full.run_tool("update_plan", json.dumps({"steps": THREE}, ensure_ascii=False))[0]
    full.approve_plan()
    full.pause_plan()
    before = full.plan_view()
    agent_id = full.id
    with _restarted(store, agent_id) as (again, revived):
        assert revived.plan_view() == before, (before, revived.plan_view())
        assert revived.spec.workflow == "plan", revived.spec.workflow
        rule, block = prompt_parts(revived)
        assert "ПРИОСТАНОВЛЕНА" in rule, rule
        assert "задача на паузе: да" in block, block
        revived.resume_plan()

        # --- ветка уносит план целиком, с флажками -------------------------
        branch = Agent(AgentSpec(label="ветка", model="stub/model"), store=again)
        branch.take_branch(revived.carry_off(2), parent_id=revived.id, forked_at=2)
        assert branch.plan_view()["steps"] == THREE, branch.plan_view()
        assert branch.plan_view()["approved"] is True, branch.plan_view()
        assert branch.plan_view()["stage"] == "execution", branch.plan_view()
        # Копия глубокая: общий список шагов сделал бы два чата одним.
        # Утверждений два: поведением видно только «правка у родителя ветку
        # не трогает» (`apply` собирает новый список), а второе — про сам
        # объект, чтобы первая же правка на месте не слила два чата молча.
        assert branch.plan["steps"] is not revived.plan["steps"], "список шагов общий"
        assert revived.run_tool(
            "update_plan",
            json.dumps({"steps": [{**step, "status": "done"} for step in THREE]},
                       ensure_ascii=False),
        )[0]
        assert branch.plan_view()["steps"] == THREE, branch.plan_view()
        assert again.load_plan(branch.id)["steps"] == THREE, again.load_plan(branch.id)

        # --- незнакомое значение из базы читается как `off` ----------------
        alien = dict(again.load_session(agent_id)["config"], workflow="ведение")
        again.save_session(agent_id, label="чужое", config=alien,
                           created_at=revived.created_at)
    with _reopened(path) as reread:
        stranger = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=reread)
        # Значение сохранилось как есть — терять диалог из-за чужого поля
        # нельзя, — а читается оно как «процесса нет»: вести план по значению,
        # смысла которого мы не знаем, так же нельзя, как резать историю
        # по незнакомой стратегии.
        assert stranger.spec.workflow == "ведение", stranger.spec.workflow
        assert stranger.plan_on() is False, "незнакомое значение включило процесс"
        assert stranger.tool_specs() == [], stranger.tool_specs()
        talk = stranger.build_prompt("вопрос")
        assert not any("[задача]" in m["content"] for m in talk), talk
        assert "Этап" not in talk[0]["content"], talk[0]

    return (
        f"шесть этапов вычислены из шести планов, {len(taskplan.STAGES)} без ярлыка "
        "в базе; пересечение approval и validation разобрано порядком, метки "
        "шагов дословны; при выключенном процессе промпт слово в слово прежний, "
        "при включённом блок едет и с пустым списком, а правило этапа — "
        "системным; четыре врезки на слотах 1—4; человеческих действий модели "
        f"не объявлено: {len(taskplan.ALLOWED) - len(taskplan.TOOL_NAMES)}, и все "
        "отсечены, выключенному чату план не пишется; укороченный список снял утверждение, переименование — "
        "нет; упавшая запись не тронула память; отказ назван всеми четырьмя "
        "частями и выходом из таблицы; пауза отказала оба инструмента и вернула "
        "тот же этап; сброс снял все три флажка; план и пауза пережили "
        "перезапуск и уехали в ветку; включённому чату tools объявлены, "
        "выключенному — нет"
    )


# --- День 13: цикл оборотов ---------------------------------------------------

PLAN_STEPS = [
    {"title": "собрать требования", "status": "pending"},
    {"title": "схема базы", "status": "pending"},
    {"title": "ручки", "status": "pending"},
]
"""План, который «присылает модель». Три шага: результат `update_plan`
называет их число, и по нему видно, какой именно вызов исполнился."""


def _call(name: str, args: dict, call_id: str) -> dict:
    """Вызов в том виде, в каком его отдаёт транспорт: `arguments` — строка."""
    return {"id": call_id, "name": name, "arguments": json.dumps(args, ensure_ascii=False)}


def _first_turn(messages) -> bool:
    """Первый ли это оборот обмена — по наличию ответа ролью `tool` в ленте,
    а не по номеру вызова к заглушке: номер сквозной на весь процесс, и один
    лишний обмен в начале увёл бы сценарий на чужой оборот молча."""
    return not any(m.get("role") == "tool" for m in messages)


def _calls_on_first(*calls):
    """Вызовы на первом обороте и молчание на всех следующих."""
    return lambda messages, index: [dict(c) for c in calls] if _first_turn(messages) else None


def _approved(steps=None) -> dict:
    """План, уже утверждённый человеком, — задача на этапе `execution`. Нужен
    сценам, где этап меняться **не должен**: с пустого плана первый же
    `update_plan` уводит задачу в `approval`."""
    return {
        "steps": [dict(step) for step in (PLAN_STEPS if steps is None else steps)],
        "approved": True,
        "finished": False,
        "paused": False,
    }


def _word_on_second(messages, index) -> str:
    """Ответ живой модели: на первом обороте ни символа текста, на втором —
    слова. Так ответила `openai/gpt-4o-mini` на живом прогоне, и ради этого
    цикл и заведён."""
    return "" if _first_turn(messages) else "план готов, начинаю"


@check("цикл оборотов: вызов исполнен, слово за моделью, в истории одна пара")
def check_tool_turn_loop():
    """Чтобы рабочий процесс был виден, одного вызова к модели мало: живой
    прогон `openai/gpt-4o-mini` вернул вызов `update_plan` на пять шагов
    и **ни одного символа текста**. Цикл «модель → инструмент → модель» —
    **условие видимости**, а не оптимизация.

    Разделы: два оборота и одна пара в истории (промежуточный ход не пишется
    вовсе); что уехало обратно — ход с `tool_calls` и **по одному** ответу
    `tool` на каждый вызов; промпт не пересобирается, а стареющее в нём
    переписано на месте; два вызова в одном ответе; кадр `tool` раньше
    `done` и со свежим планом; отказ доезжает директивой; битые аргументы;
    предел `MAX_TURNS` и у последнего оборота нет `tools`; **второй сторож**
    (проверить его можно только в мире, где первого нет: `turn_tools`
    подменён в самой проверке, а запас по времени — чтобы снятый сторож
    давал красное, а не зависание); отмена; молчание на всех оборотах;
    склейка слов обоих оборотов; выключенный процесс; сжатие.
    """
    import app.plan as taskplan

    # --- два оборота, одна пара ---------------------------------------------
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    with TestClient(main.app) as client:
        chat = new_agent(client, system="СИС", workflow="plan")
        frames = _frames(client, chat, "спланируй работу")
        history = client.get(f"/api/agents/{chat}").json()["transcript"]
        plan_now = REGISTRY.require(chat).plan_view()

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    assert [m["role"] for m in history] == ["user", "assistant"], history
    assert history[-1]["content"] == "план готов, начинаю", history[-1]
    kinds = [f["event"] for f in frames]
    # Кадр `start` один на весь обмен: он показывает промпт, с которого
    # обмен начался, и второй такой же кадр развёл бы номера врезок.
    assert kinds.count("start") == 1, kinds
    assert frames[-1]["committed"] is True and frames[-1]["error"] is None, frames[-1]
    assert frames[-1]["metrics"]["turns"] == 2, frames[-1]["metrics"]

    # --- что уехало обратно --------------------------------------------------
    sent = _stub.CALLS[1]["messages"]
    move, answer = sent[-2], sent[-1]
    assert move["role"] == "assistant" and move["content"] is None, move
    assert move["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "update_plan",
                "arguments": json.dumps({"steps": PLAN_STEPS}, ensure_ascii=False),
            },
        }
    ], move["tool_calls"]
    # Аргументы уезжают **строкой**, как приехали: разобранный объект
    # провайдер не примет, а строку он прислал сам.
    assert isinstance(move["tool_calls"][0]["function"]["arguments"], str), move
    # Ответ инструмента — своё сообщение ролью `tool` с тем же `tool_call_id`
    # и без единого лишнего ключа: провайдер ждёт ровно эти три.
    assert set(answer) == {"role", "tool_call_id", "content"}, answer
    assert answer["role"] == "tool" and answer["tool_call_id"] == "call_1", answer
    assert "План записан: 3 шагов" in answer["content"], answer["content"]

    # --- промпт не пересобирается, стареющее в нём переписано на месте -------
    # Пересобирать целиком нельзя: номера врезок уже уехали кадром `start`.
    # Но правило этапа и блок задачи **устаревают**, и переписываются они
    # **на месте** — длина и позиции те же, дописано только два сообщения.
    head = _stub.CALLS[0]["messages"]
    started = _frame(frames, "start")
    assert started["resolved_messages"] == head, head
    assert len(sent) == len(head) + 2, (len(sent), len(head))
    plan_at = started["plan_at"]
    fresh = {0, plan_at}
    assert [m for i, m in enumerate(sent[: len(head)]) if i not in fresh] == [
        m for i, m in enumerate(head) if i not in fresh
    ], (sent[: len(head)], head)

    # --- кадр `tool` раньше `done`, и план в нём новый -----------------------
    assert kinds.index("tool") < kinds.index("done"), kinds
    told = _frame(frames, "tool")
    assert told["name"] == "update_plan" and told["ok"] is True, told
    assert told["message"] == answer["content"], (told["message"], answer["content"])
    # Этап в кадре — уже `approval`: план записан и ждёт кнопки. Пришли он
    # после цикла, человек увидел бы шаги позже модели.
    assert told["plan"]["stage"] == "approval", told["plan"]
    assert [s["title"] for s in told["plan"]["steps"]] == [
        s["title"] for s in PLAN_STEPS
    ], told["plan"]
    assert plan_now["stage"] == "approval", plan_now

    # --- два вызова в одном ответе: исполнены оба, в порядке приезда ---------
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(
            _call("update_plan", {"steps": PLAN_STEPS}, "call_a"),
            _call("update_plan", {"steps": PLAN_STEPS[:1]}, "call_b"),
        ),
    )
    with TestClient(main.app) as client:
        pair = new_agent(client, workflow="plan")
        frames = _frames(client, pair, "перепиши план дважды")
        after = REGISTRY.require(pair).plan_view()

    told = [f for f in frames if f["event"] == "tool"]
    assert len(told) == 2, [f["event"] for f in frames]
    # Порядок виден по числу шагов: сперва три, потом один. Исполни цикл
    # только первый — второго кадра не было бы вовсе; переставь их —
    # в плане осталось бы три шага.
    assert "3 шагов" in told[0]["message"] and "1 шагов" in told[1]["message"], told
    assert [s["title"] for s in after["steps"]] == ["собрать требования"], after
    sent = _stub.CALLS[1]["messages"]
    assert [m["role"] for m in sent[-3:]] == ["assistant", "tool", "tool"], sent[-3:]
    # По одному ответу на каждый вызов и с его собственным `tool_call_id`:
    # один ответ на все провайдер отвергнет, а чужой id — тем более.
    assert [m["tool_call_id"] for m in sent[-2:]] == ["call_a", "call_b"], sent[-2:]
    assert [c["id"] for c in sent[-3]["tool_calls"]] == ["call_a", "call_b"], sent[-3]

    # --- отказ доезжает до модели директивой ---------------------------------
    # Сцена выбрана так, чтобы отказ **стерёг**: `finish_task` на
    # неутверждённом плане. Проглоти цикл отказ — модель прочитала бы
    # молчание как согласие.
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(_call("finish_task", {"problems": []}, "call_no")),
    )
    with TestClient(main.app) as client:
        denied = new_agent(client, workflow="plan")
        frames = _frames(client, denied, "закрывай")
        left = REGISTRY.require(denied).plan_view()

    told = _frame(frames, "tool")
    assert told["ok"] is False, told
    assert told["message"].startswith("finish_task НЕ выполнен"), told["message"]
    assert "НЕ утверждай, что это сделано." in told["message"], told["message"]
    assert _stub.CALLS[1]["messages"][-1]["content"] == told["message"], _stub.CALLS[1]
    assert left["stage"] == "planning" and left["finished"] is False, left
    assert frames[-1]["committed"] is True, frames[-1]

    # --- битые аргументы: план прежний, исключения нет -----------------------
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(
            {"id": "call_x", "name": "update_plan", "arguments": '{"steps": ['}
        ),
    )
    with TestClient(main.app) as client:
        torn = new_agent(client, workflow="plan")
        frames = _frames(client, torn, "план, но обрезанный")
        broken = REGISTRY.require(torn).plan_view()

    told = _frame(frames, "tool")
    assert told["ok"] is False and "не разбираемой строкой" in told["message"], told
    assert broken["steps"] == [] and broken["stage"] == "planning", broken
    assert frames[-1]["committed"] is True and frames[-1]["error"] is None, frames[-1]

    # --- предел: `MAX_TURNS` вызовов, у последнего нет `tools` ---------------
    # Заглушка просит вызов на **каждом** обороте — так выглядит зациклившаяся
    # модель. Упирается обмен не в тишину, а в слова. Этап при этом
    # не меняется ни разу, и сцена собрана так **нарочно**: иначе первый же
    # оборот сменил бы этап, и предела не увидел бы никто.
    _stub.reset()
    _stub.install(
        reply="ещё немного",
        tool_calls=lambda messages, index: [
            _call("update_plan", {"steps": PLAN_STEPS}, f"call_{index}")
        ],
    )
    endless = _bare("предел оборотов", workflow="plan")
    endless.plan = _approved()
    events = asyncio.run(drain(endless.ask("крути")))
    told = [e for e in events if e["type"] == "tool"]

    limit = agent_module.MAX_TURNS
    assert endless.plan_stage() == "execution", endless.plan_view()
    assert len(_stub.CALLS) == limit, len(_stub.CALLS)
    assert len(told) == limit - 1, len(told)
    assert "tools" not in _stub.CALLS[limit - 1]["payload"], _stub.CALLS[limit - 1]["payload"]
    assert "tools" in _stub.CALLS[limit - 2]["payload"], _stub.CALLS[limit - 2]["payload"]
    assert events[-1]["committed"] is True, events[-1]
    assert events[-1]["metrics"]["turns"] == limit, events[-1]["metrics"]

    # --- предел держится счётчиком, а не отсутствием инструментов -----------
    # Раздел про второй сторож. Пока первый (`turn_tools`) на месте, жёсткий
    # выход по счётчику ничего не решает, и проверить его можно **только**
    # в мире, где первого нет: `turn_tools` подменён здесь и объявляет
    # инструменты на каждом обороте, а заглушка на каждом просит вызов.
    #
    # Запас по времени — чтобы снятый сторож давал **красное**, а не зависшую
    # проверку. Пауза между кусками у заглушки — чтобы отмена по времени
    # успела доехать: без единого `await` таймер не срабатывает.
    _stub.reset()
    _stub.install(
        reply="ещё немного",
        delay=0.02,
        tool_calls=lambda messages, index: [
            _call("update_plan", {"steps": PLAN_STEPS}, f"call_{index}")
        ],
    )

    async def bounded():
        chat = _bare("вечный цикл", workflow="plan")
        try:
            return chat, await asyncio.wait_for(drain(chat.ask("крути")), timeout=5)
        except asyncio.TimeoutError:
            return chat, None

    with patch.object(
        agent_module.Agent,
        "turn_tools",
        lambda self, spec, turn, stage=None: list(taskplan.TOOLS),
    ):
        spun, events = asyncio.run(bounded())

    assert events is not None, (
        f"обмен не кончился сам: вызовов к модели уже {len(_stub.CALLS)}, "
        "а предел оборотов держался только на том, что инструментов "
        "не объявили"
    )
    assert len(_stub.CALLS) == limit, len(_stub.CALLS)
    # Инструменты объявлены на всех оборотах, последний не исключение:
    # выйти обмен обязан по счётчику, а не по их отсутствию.
    assert all("tools" in call["payload"] for call in _stub.CALLS), [
        "tools" in call["payload"] for call in _stub.CALLS
    ]
    ended = events[-1]
    assert ended["type"] == "done", ended
    # Кончиться обмен обязан внятно: либо записан, либо назван
    # несостоявшимся. Молчание здесь — тот же сбой сети на экране.
    assert ended["committed"] is True or ended["error"], ended
    assert ended["metrics"]["turns"] == limit, ended["metrics"]
    assert len(spun.history) == 2, spun.history

    # --- слова обоих оборотов, а не последнего -------------------------------
    # Текст и вызов приезжают **вместе** (это стережёт своя проверка
    # транспорта), значит склейка через обороты не умозрительная: перетри
    # её последним — и половина сказанного пропала бы молча.
    _stub.reset()
    _stub.install(
        reply=lambda messages, index: "смотрю, что уже есть" if _first_turn(messages)
        else "план записан, берусь за первый шаг",
        reasoning="прикидываю порядок",
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    with TestClient(main.app) as client:
        both = new_agent(client, workflow="plan")
        frames = _frames(client, both, "начни работу")
        history = client.get(f"/api/agents/{both}").json()["transcript"]

    glued = "смотрю, что уже есть\n\nплан записан, берусь за первый шаг"
    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    assert history[-1]["content"] == glued, history[-1]["content"]
    assert frames[-1]["text"] == glued, frames[-1]["text"]
    # Ход ассистента, уехавший обратно, несёт слова **своего** оборота —
    # не склейку: модель читает их как то, что сама сказала перед вызовом.
    assert _stub.CALLS[1]["messages"][-2]["content"] == "смотрю, что уже есть", (
        _stub.CALLS[1]["messages"][-2]
    )
    # Рассуждение склеено тем же `_glue` и той же дырой не покрыто: модель
    # думала на обоих оборотах, и думанное на первом никуда не делось.
    assert history[-1]["reasoning"] == "прикидываю порядок\n\nприкидываю порядок", (
        history[-1]["reasoning"]
    )
    assert frames[-1]["reasoning"] == history[-1]["reasoning"], frames[-1]["reasoning"]

    # --- отмена: накопленное не исполняется ----------------------------------
    # Вызова два, первый — кнопка человека, он отказан и план не меняет.
    # Отменяем на его кадре, до второго. Проверяй цикл отмену только
    # на кусках потока — второй исполнился бы, и план стал бы не тот.
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(
            _call("approve", {}, "call_human"),
            _call("update_plan", {"steps": PLAN_STEPS}, "call_late"),
        ),
    )

    async def cancel_at_tool():
        agent = agent_module.Agent(
            AgentSpec(label="отмена", model="stub/model", workflow="plan")
        )
        seen = []
        async for event in agent.ask("сделай и отменись"):
            seen.append(event)
            if event["type"] == "tool":
                agent.cancel()
        return agent, seen

    stopped, seen = asyncio.run(cancel_at_tool())
    told = [e for e in seen if e["type"] == "tool"]
    assert len(told) == 1 and told[0]["name"] == "approve", told
    assert told[0]["ok"] is False, told[0]
    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    assert stopped.plan["steps"] == [], stopped.plan
    assert seen[-1]["cancelled"] is True, seen[-1]
    assert seen[-1]["error"] == "генерация отменена", seen[-1]

    # --- текста нет ни на одном обороте: обмен не записан, причина названа ---
    _stub.reset()
    _stub.install(
        reply="",
        tool_calls=lambda messages, index: [
            _call("update_plan", {"steps": PLAN_STEPS}, f"call_{index}")
        ],
    )
    with TestClient(main.app) as client:
        mute = new_agent(client, workflow="plan")
        frames = _frames(client, mute, "молчи")
        history = client.get(f"/api/agents/{mute}").json()["transcript"]

    assert history == [], history
    done = frames[-1]
    assert done["committed"] is False, done
    assert done["question"] == "молчи", done
    # Молчание в `done` человек прочитал бы как сбой сети: на экране
    # не появилось бы ни ответа, ни объяснения.
    assert done["error"] and "ни слова текста" in done["error"], done["error"]

    # --- выключенный процесс: ни `tools`, ни кадров `tool` -------------------
    # Утверждение об отсутствии стоит там, где присутствие достижимо: та же
    # заглушка с теми же вызовами только что дала и ключ в теле, и кадры.
    _stub.reset()
    _stub.install(
        reply="обычный ответ",
        tool_calls=lambda messages, index: [
            _call("update_plan", {"steps": PLAN_STEPS}, f"call_{index}")
        ],
    )
    with TestClient(main.app) as client:
        off = new_agent(client, system="СИС", workflow="off")
        frames = _frames(client, off, "просто вопрос")
        history = client.get(f"/api/agents/{off}").json()["transcript"]

    assert len(_stub.CALLS) == 1, len(_stub.CALLS)
    assert "tools" not in _stub.CALLS[0]["payload"], _stub.CALLS[0]["payload"].keys()
    assert "tool" not in [f["event"] for f in frames], [f["event"] for f in frames]
    assert [m["role"] for m in history] == ["user", "assistant"], history
    assert [m["role"] for m in _stub.CALLS[0]["messages"]] == ["system", "user"], _stub.CALLS[0]

    # --- сжатие идёт без инструментов ----------------------------------------
    _stub.reset()
    _stub.install(
        reply=_service_aware,
        tool_calls=lambda messages, index: (
            None if _service_kind(messages) else [_call("update_plan", {"steps": []}, "c")]
        ),
    )
    with TestClient(main.app) as client:
        folding = new_agent(client, workflow="plan", strategy="summary",
                            keep_last=2, compress_every=2)
        _talk(client, folding, 3)

    folded = _service_calls("summary")
    assert folded, "сжатия не случилось — сцена не та"
    for call in folded:
        assert "tools" not in call["payload"], call["payload"].keys()

    return (
        "оборот вызова и оборот слова: два обращения, одна пара в истории; "
        "обратно уехали ход с tool_calls и по ответу ролью tool на каждый вызов; "
        f"промпт не пересобран; кадр tool раньше done и с планом на этапе "
        f"approval; отказ и битые аргументы доехали директивой, план цел; "
        f"предел {limit} оборотов на неменяющемся этапе, у последнего tools нет, "
        f"а со снятым первым "
        f"сторожем обмен всё равно кончился на {limit}-м; слова и рассуждение "
        f"обоих оборотов склеены; отмена на кадре tool "
        f"оставила план пустым при одном вызове; без текста обмен не записан "
        f"и причина названа; выключенному процессу ни tools, ни кадров; "
        f"сжатие ушло без инструментов (вызовов: {len(folded)})"
    )


@check("один этап — один обмен: правило свежее на каждом обороте, смена гасит вызовы")
def check_stage_per_exchange():
    """**Правило этапа протухало внутри обмена**: промпт собирался один раз,
    и модель, прошедшая за пять оборотов работу и проверку, всё это время
    читала «Этап: выполнение, шаг 1 из 5», потому что свежий список приезжал
    только результатом вызова.

    Чиним правилом и блоком задачи, пересобираемыми перед **каждым** оборотом
    (`restage`), и сменой этапа, гасящей инструменты на следующем
    (`turn_tools`). Разделы: свежее правило и свежий список; переписано
    на месте (системное сообщение по-прежнему одно, номера врезок из кадра
    `start` показывают на те же врезки); смена этапа гасит инструменты,
    а обмен всё равно записан; этап не сменился — цикл идёт дальше;
    `execution` → `validation`; выключенный процесс.
    """
    import app.plan as taskplan

    # --- свежее правило, свежий список, переписанные на месте ---------------
    # Сцена нарочно с врезками: номера у них считаны один раз и уехали кадром
    # `start`. Перепиши мы стареющее **дописыванием** — номера показывали бы
    # на чужое, а у чата завелось бы второе системное сообщение.
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    with TestClient(main.app) as client:
        client.post("/api/memory", json={"kind": "profile", "content": "пишет на Kotlin"})
        chat = new_agent(client, system="СИС", workflow="plan")
        client.post(f"/api/agents/{chat}/working",
                    json={"kind": "goal", "content": "собрать ТЗ"})
        frames = _frames(client, chat, "спланируй работу")
        history = client.get(f"/api/agents/{chat}").json()["transcript"]
        after = REGISTRY.require(chat).plan

    first, second = _stub.CALLS[0]["messages"], _stub.CALLS[1]["messages"]
    started = _frame(frames, "start")
    assert len(_stub.CALLS) == 2, len(_stub.CALLS)

    # Правило этапа на первом обороте — про планирование, на втором — про
    # утверждение, и оба **совпадают с тем, что сказал бы `app/plan.py`**
    # про план на тот момент. Сверка с готовой строкой, а не с куском текста:
    # правило, собранное вторым местом, разошлось бы с первым молча.
    assert taskplan.stage_rule({}) in first[0]["content"], first[0]["content"]
    assert taskplan.stage_rule(after) in second[0]["content"], second[0]["content"]
    assert first[0]["content"] != second[0]["content"], first[0]["content"]
    assert taskplan.stage_of(after)[0] == "approval", after

    # Блок задачи — нынешний список, и собран он той же `task_message`, какой
    # собран блок первого оборота: вторая форма того же списка разъехалась бы
    # с первой молча.
    plan_at = started["plan_at"]
    assert "шагов ещё нет" in first[plan_at]["content"], first[plan_at]["content"]
    assert second[plan_at]["content"] == agent_module.task_message(after)["content"], (
        second[plan_at]["content"]
    )
    assert all(
        step["title"] in second[plan_at]["content"] for step in PLAN_STEPS
    ), second[plan_at]["content"]

    # Системное сообщение по-прежнему одно — на **обоих** оборотах.
    for sent in (first, second):
        assert [m["role"] for m in sent].count("system") == 1, [m["role"] for m in sent]
    # Лента выросла ровно на два дописанных сообщения: ход ассистента
    # и ответ инструмента. Ни одно стареющее место не приросло третьим.
    assert len(second) == len(first) + 2, (len(second), len(first))
    # И номера врезок из кадра `start` показывают на те же врезки в промпте
    # **любого** оборота: слоты уехали один раз и на оба оборота одни.
    for sent in (first, second):
        assert sent[started["memory_at"]]["content"].startswith("[долговременная память]"), sent
        assert sent[started["working_at"]]["content"].startswith("[факты о разговоре]"), sent
        assert sent[plan_at]["content"].startswith("[задача]"), sent[plan_at]

    # --- смена этапа гасит инструменты, но обмен записан --------------------
    assert "tools" in _stub.CALLS[0]["payload"], _stub.CALLS[0]["payload"].keys()
    assert "tools" not in _stub.CALLS[1]["payload"], _stub.CALLS[1]["payload"].keys()
    done = frames[-1]
    assert done["committed"] is True and done["error"] is None, done
    assert done["text"] == "план готов, начинаю", done["text"]
    assert [m["role"] for m in history] == ["user", "assistant"], history
    assert history[-1]["content"], history[-1]
    # Каким этап был и каким стал — данными, а не догадкой клиента.
    assert (done["stage_from"], done["stage_to"]) == ("planning", "approval"), done

    # --- этап не сменился — цикл идёт дальше --------------------------------
    # Обратная половина, рядом нарочно: та же заглушка и тот же инструмент
    # только что погасили `tools`. Здесь план утверждён, этап не двигается —
    # и инструменты объявлены снова.
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    steady = _bare("этап на месте", workflow="plan")
    steady.plan = _approved()
    events = asyncio.run(drain(steady.ask("работай")))

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    assert "tools" in _stub.CALLS[1]["payload"], _stub.CALLS[1]["payload"].keys()
    assert events[-1]["committed"] is True, events[-1]
    assert (events[-1]["stage_from"], events[-1]["stage_to"]) == (
        "execution", "execution",
    ), events[-1]

    # --- `execution` → `validation`: переход живого прогона ------------------
    # Последний шаг отмечен сделанным, и дальше модель проверяет, а не
    # работает. Внутри одного обмена она читала бы «шаг 1 из 1 — выполняй».
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(
            _call("update_plan", {"steps": [{"title": "собрать требования", "status": "done"}]},
                  "call_done")
        ),
    )
    moved = _bare("работа кончилась", workflow="plan")
    moved.plan = _approved([{"title": "собрать требования", "status": "pending"}])
    events = asyncio.run(drain(moved.ask("доделывай")))

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    assert "Этап: выполнение" in _stub.CALLS[0]["messages"][0]["content"], _stub.CALLS[0]
    assert "Этап: проверка" in _stub.CALLS[1]["messages"][0]["content"], _stub.CALLS[1]
    assert "tools" not in _stub.CALLS[1]["payload"], _stub.CALLS[1]["payload"].keys()
    assert events[-1]["committed"] is True, events[-1]
    assert (events[-1]["stage_from"], events[-1]["stage_to"]) == (
        "execution", "validation",
    ), events[-1]
    assert len(moved.history) == 2 and moved.history[-1].content, moved.history

    # --- выключенный процесс: двигаться нечему ------------------------------
    _stub.reset()
    _stub.install(reply="обычный ответ")
    plain = _bare("обычный чат")
    events = asyncio.run(drain(plain.ask("просто вопрос")))
    assert events[-1]["stage_from"] is None and events[-1]["stage_to"] is None, events[-1]

    return (
        "правило этапа и блок задачи пересобраны на каждом обороте: "
        "planning → approval, системное сообщение осталось одно, лента "
        "выросла ровно на два сообщения, слоты сошлись на обоих оборотах; "
        "смена этапа погасила tools, обмен записан и текст непуст; этап "
        "на месте — tools объявлены снова; execution → validation прошёл "
        "сменой правила; у выключенного процесса оба этапа пусты"
    )


@check("на планировании план обязателен: tool_choice, а не уговоры")
def check_planning_forces_update_plan():
    """Последнее место дня, где обещание держалось на тексте: «на планировании
    сначала план, а не решение». Живой прогон `openai/gpt-4o-mini` показал
    цену — на маленькой задаче модель писала решение сразу.

    Закрыто одним полем тела: на `planning` `tool_choice` называет **именно**
    `update_plan`, а не `"required"` — «любой инструмент» позволил бы позвать
    отказанный здесь `finish_task`. Разделы: planning — поле есть и называет
    `update_plan` (заодно правило этапа читается приказом); approval — поля
    нет, и утверждение об отсутствии стоит там, где присутствие достижимо;
    остальные этапы с названным и сверенным этапом каждой сцены; выключенный
    процесс; отказ не снимает принуждения, а обмен всё равно кончается
    словами.
    """
    import app.plan as taskplan

    forced = {"type": "function", "function": {"name": "update_plan"}}
    """Ожидаемое поле — **дословно**, а не `taskplan.FORCE_UPDATE_PLAN`:
    сверка константы с собой зелена и тогда, когда в ней оказалось
    `"required"` или чужое имя."""

    # --- planning: поле есть, и это именно update_plan ----------------------
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    chat = _bare("принуждение", workflow="plan")
    events = asyncio.run(drain(chat.ask("напиши юнит-тест на Kotlin")))

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    assert _stub.CALLS[0]["payload"]["tool_choice"] == forced, _stub.CALLS[0]["payload"]
    assert "tools" in _stub.CALLS[0]["payload"], _stub.CALLS[0]["payload"].keys()

    # Правило этапа — **приказ**, а не запрет: вызов назван единственным
    # законным ответом, и у маленькой задачи есть пол в два шага. Сверка
    # по этим двум оборотам речи, а не по «первому предложению»: там
    # `update_plan` стоял и у старой формулировки.
    rule = _stub.CALLS[0]["messages"][0]["content"]
    assert "единственный ответ" in rule, rule
    assert "не меньше двух" in rule, rule
    # Запрет остался, но **вторым**, после приказа.
    assert rule.index("update_plan") < rule.index("ничего не выполняй"), rule

    # Смена этапа гасит инструменты — вместе с ними гаснет и принуждение.
    assert "tools" not in _stub.CALLS[1]["payload"], _stub.CALLS[1]["payload"].keys()
    assert "tool_choice" not in _stub.CALLS[1]["payload"], _stub.CALLS[1]["payload"].keys()
    assert chat.plan_stage() == "approval", chat.plan_view()
    assert events[-1]["committed"] is True and events[-1]["text"], events[-1]

    # --- approval: инструменты есть, принуждения нет ------------------------
    # Тот же чат, следующий обмен: `tools` объявлены на **обоих** оборотах
    # тем же списком, и разница только в этапе.
    _stub.reset()
    _stub.install(
        reply=_word_on_second,
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_2")),
    )
    asyncio.run(drain(chat.ask("поправь второй шаг")))

    assert len(_stub.CALLS) == 2, len(_stub.CALLS)
    assert chat.plan_stage() == "approval", chat.plan_view()
    for call in _stub.CALLS:
        assert "tools" in call["payload"], call["payload"].keys()
        assert "tool_choice" not in call["payload"], call["payload"].keys()

    # --- остальные этапы: принуждения нет ни на одном -----------------------
    scenes = {
        "execution": _approved(),
        "validation": _approved([{"title": "собрать требования", "status": "done"}]),
        "paused": {**_approved(), "paused": True},
        "done": {**_approved(), "finished": True},
    }
    for stage, plan in scenes.items():
        _stub.reset()
        _stub.install(reply="отвечаю словами")
        staged = _bare(f"этап {stage}", workflow="plan")
        staged.plan = plan
        asyncio.run(drain(staged.ask("что дальше")))
        # Сцена и правда на том этапе, о котором говорит её имя: иначе
        # «на execution поля нет» держалось бы на чём угодно.
        assert staged.plan_stage() == stage, (stage, staged.plan_view())
        assert len(_stub.CALLS) == 1, len(_stub.CALLS)
        assert "tools" in _stub.CALLS[0]["payload"], (stage, _stub.CALLS[0]["payload"].keys())
        assert "tool_choice" not in _stub.CALLS[0]["payload"], (
            stage, _stub.CALLS[0]["payload"].keys()
        )

    # --- выключенный процесс: ни того, ни другого ---------------------------
    _stub.reset()
    _stub.install(reply="обычный ответ")
    plain = _bare("обычный чат")
    asyncio.run(drain(plain.ask("просто вопрос")))
    assert "tools" not in _stub.CALLS[0]["payload"], _stub.CALLS[0]["payload"].keys()
    assert "tool_choice" not in _stub.CALLS[0]["payload"], _stub.CALLS[0]["payload"].keys()

    # --- отказ не снимает принуждения, а предел всё равно кончает словами ---
    # `update_plan` с пустым списком отказан, этап остаётся `planning` —
    # и каждый следующий оборот снова принудительный. Вечным обменом это
    # не кончается: последний оборот идёт без инструментов и без поля.
    limit = agent_module.MAX_TURNS
    _stub.reset()
    _stub.install(
        reply="не выходит",
        tool_calls=lambda messages, index: [_call("update_plan", {"steps": []}, f"call_{index}")],
    )
    stuck = _bare("отказ за отказом", workflow="plan")
    events = asyncio.run(drain(stuck.ask("спланируй")))

    assert len(_stub.CALLS) == limit, len(_stub.CALLS)
    assert stuck.plan_stage() == "planning", stuck.plan_view()
    assert all(
        call["payload"].get("tool_choice") == forced for call in _stub.CALLS[: limit - 1]
    ), [call["payload"].get("tool_choice") for call in _stub.CALLS]
    # Последний разрешённый оборот: ни инструментов, ни принуждения, хотя
    # этап всё ещё `planning`.
    last = _stub.CALLS[limit - 1]["payload"]
    assert "tools" not in last and "tool_choice" not in last, last.keys()
    assert events[-1]["committed"] is True and events[-1]["text"], events[-1]

    return (
        f"на planning tool_choice называет update_plan, и правило этапа "
        f"читается приказом; на approval, execution, validation, paused "
        f"и done поля нет при объявленных инструментах; без рабочего "
        f"процесса нет ни того, ни другого; отказанный вызов принуждения "
        f"не снимает — {limit} оборотов, последний без инструментов и словами"
    )


@check("метрики обмена — сумма его оборотов, а не последний из них")
def check_turn_metrics_merged():
    """Обмен из трёх оборотов оплачен весь, и показать у него числа
    последнего значило бы соврать втрое. Разделы по `merge_turn_metrics`:
    складываются токены и цена (100 + 200 = 300); от первого оборота — время
    до первого токена; от последнего — `finish_reason` и модель; `turns`
    в суммы по чату не идёт; молчание не становится нулём."""
    numbers = {
        0: {"prompt_tokens": 10, "completion_tokens": 100, "total_tokens": 100,
            "cost_usd": 0.0001, "ttft_ms": 11.0, "first_token_ms": 5.0,
            "reasoning_tokens": None, "model": "stub/первая"},
        1: {"prompt_tokens": 20, "completion_tokens": 200, "total_tokens": 200,
            "cost_usd": 0.0002, "ttft_ms": 99.0, "first_token_ms": 50.0,
            "reasoning_tokens": 7, "model": "stub/последняя"},
    }
    _stub.install(
        reply=_word_on_second,
        usage=lambda index: numbers[index],
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    with TestClient(main.app) as client:
        chat = new_agent(client, workflow="plan")
        frames = _frames(client, chat, "два оборота")
        body = client.get(f"/api/agents/{chat}").json()
    metrics = frames[-1]["metrics"]

    assert metrics["prompt_tokens"] == 30, metrics
    assert metrics["completion_tokens"] == 300, metrics
    assert metrics["total_tokens"] == 300, metrics
    assert metrics["cost_usd"] == 0.0003, metrics
    # `None` у первого оборота и число у второго: молчание не съедает число
    # и само нулём не становится.
    assert metrics["reasoning_tokens"] == 7, metrics
    # Время до первого токена — от первого оборота: у второго оно
    # отсчитывалось бы от его собственного начала и показало бы паузу
    # короче, чем она была.
    assert metrics["ttft_ms"] == 11.0, metrics
    assert metrics["first_token_ms"] == 5.0, metrics
    # А `finish_reason` и модель — от последнего: они про тот вызов, которым
    # обмен кончился. На первом обороте провайдер назвал причиной `tool_calls`.
    assert metrics["finish_reason"] == "stop", metrics
    assert metrics["model"] == "stub/последняя", metrics
    assert metrics["turns"] == 2, metrics
    # Итог по чату считает `USAGE_FIELDS`, и `turns` в него не входит:
    # обороты это не токены и складывать их по чату незачем.
    assert body["usage_total"] == {
        "prompt_tokens": 30, "completion_tokens": 300, "total_tokens": 300,
        "cost_usd": 0.0003,
    }, body["usage_total"]
    assert body["history_len"] == 2, body["history_len"]

    # --- молчание провайдера о цифре не становится нулём ---------------------
    # Цены не назвал ни один оборот — у обмена её нет, а не ноль. `or 0`
    # дал бы 0, и вместо прочерка на экране встала бы бесплатная модель.
    _stub.reset()
    silent = {"cost_usd": None, "total_tokens": None, "reasoning_tokens": None}
    _stub.install(
        reply=_word_on_second,
        usage=lambda index: silent,
        tool_calls=_calls_on_first(_call("update_plan", {"steps": PLAN_STEPS}, "call_1")),
    )
    with TestClient(main.app) as client:
        quiet = new_agent(client, workflow="plan")
        mute = _frames(client, quiet, "молчаливый провайдер")[-1]["metrics"]
        totals = client.get(f"/api/agents/{quiet}").json()["usage_total"]

    assert mute["cost_usd"] is None, mute
    assert mute["total_tokens"] is None, mute
    assert mute["reasoning_tokens"] is None, mute
    assert mute["turns"] == 2, mute
    assert totals["cost_usd"] is None and totals["total_tokens"] is None, totals

    # --- обычный обмен: один оборот, и это не «нет оборотов» -----------------
    _stub.reset()
    _stub.install(reply="просто слова")
    with TestClient(main.app) as client:
        plain = new_agent(client)
        one = _frames(client, plain, "вопрос")[-1]["metrics"]
    assert one["turns"] == 1, one

    return (
        "100 + 200 = 300 токенов и 0.0001 + 0.0002 = 0.0003; ttft 11.0 от "
        "первого оборота, finish_reason stop и модель от последнего; turns 2 "
        "у цикла и 1 у обычного обмена, в итог по чату не входит; молчание "
        "провайдера о цене осталось прочерком, а не нулём"
    )


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
    перечисленный отстал бы ровно тогда, когда поле добавили и забыли.
    История длиннее прежних отсечек: вернись любая — станет красно."""
    from dataclasses import fields as spec_fields

    from app.agent import Agent

    # Значение, отличное от умолчания, для каждого поля конфига.
    probes: dict = {}
    for f in spec_fields(AgentSpec):
        kind = str(f.type)
        if f.name == "model":
            probes[f.name] = "проверка/модель"
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
    """Номера строк — не отделка хранения: по ним история поднимается
    (`ORDER BY seq`), и дыра или сдвиг значат, что `save_history` дописывает
    хвост вместо того, чтобы переписать историю целиком. Вторая половина —
    про то, что несостоявшийся обмен не оставляет вопроса без ответа.
    Мутациями проверено: без этой проверки молча проходят все три поломки."""
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
    """`BEGIN IMMEDIATE`, а не голый `BEGIN`: отложенная транзакция,
    начавшаяся с чтения, при повышении до записи получает SQLITE_BUSY **мимо**
    `busy_timeout` — ретрая нет, и второй процесс получает ошибку вместо
    очереди. `two_processes.py` этого не ловит: там все пути записи
    начинаются с записи.

    Проверяется наблюдаемым: пока транзакция открыта, второй писатель обязан
    её видеть (соединение с нулевым таймаутом — нужен сам факт блокировки).
    И то, ради чего она нужна: не дождавшийся получает внятный 503, а не 500.
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


@check("ключа нет ни в интерфейсе, ни в отдаваемых наружу данных")
def check_no_key_leak():
    client_src = (
        read("app/static/app.js") + read("app/static/index.html") + read("app/static/style.css")
    ).lower()
    for word in ("api key", "api_key", "apikey", "sk-or", "openrouter_api"):
        assert word not in client_src, f"в клиенте упоминается «{word}»"

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
    return "в клиенте про ключ ни слова, ручки его не отдают"


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
    """Несущий слой чистки — не явные `redact()` в методах, а `_Writer`:
    транзакция отдаёт обёртку, и чистится любой строковый параметр любого
    запроса. Стеречь это надо отдельно: `check_no_key_in_db` ходит теми
    путями, которые знает, а новый путь мимо транзакции прошёл бы зелёным.

    Поэтому список путей **выводится** из класса `Store`: всякий метод
    с INSERT/UPDATE/DELETE/REPLACE обязан идти через `tx()`. Перечисленный
    список пропустил бы ровно тот метод, который забыли в него внести.
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
        # И то же с другой стороны: путь записи обязан **открывать**
        # транзакцию, а не только не трогать соединение по известным именам.
        # Способов достать голое соединение больше, чем в списке запрещённых
        # форм, и однажды мутация прошла мимо него зелёной.
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
        leaked = _columns_holding(store.conn, key)
        assert not leaked, f"ключ уехал в базу через tx(): {leaked}"
    return f"{len(checked)} путей записи выведено из класса, все идут через tx()"


@check("клиент ничего не тянет из сети: ни шрифтов, ни библиотек, ни иконок")
def check_no_cdn():
    html = read("app/static/index.html")
    css = read("app/static/style.css")
    js = read("app/static/app.js")
    for name, src in (("index.html", html), ("style.css", css), ("app.js", js)):
        for pattern in ("http://", "https://", "//cdn", "@import", "url("):
            for hit in re.findall(re.escape(pattern) + r"[^\s\"'()]*", src):
                # Единственное допустимое вхождение — пространство имён SVG:
                # это идентификатор, по нему браузер никуда не ходит.
                assert hit.startswith("http://www.w3.org/2000/svg"), f"{name}: {hit}"
    assert "@font-face" not in css, "свои шрифты тоже не подключаем"
    assert html.count("<script") == 1 and 'src="/static/app.js"' in html
    assert html.count("<link") == 1 and 'href="/static/style.css"' in html
    return "в статике только относительные пути и w3.org-неймспейс SVG"


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
