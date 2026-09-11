"""Ядро проверок Дней 6 и 7 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Каждая проверка стережёт одно обещание продукта: пункт задания одного из двух
дней или сквозное свойство. Отдельные скрипты (`spawn_100.py`, `restart.py`,
`two_processes.py`) запускаются отсюда же.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from checks import _stub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_stub.install_offline()

import app.agent as agent_module  # noqa: E402
import app.main as main  # noqa: E402
from app.registry import REGISTRY, AgentRegistry  # noqa: E402
from app.schema import AgentSpec  # noqa: E402
from app.store import Store, StoreBusyError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

CHECKS: list = []
"""Проверки в порядке объявления. Наполняет декоратор `check`."""


def check(name):
    def wrap(fn):
        def run():
            _stub.reset()
            _reset_registry()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
            except AssertionError as exc:
                RESULTS.append((name, False, str(exc)))
            except (Exception, SystemExit) as exc:  # noqa: BLE001
                # SystemExit тоже краснеет, а не уносит прогон: им падает CLI,
                # и упавшая проверка не должна забирать с собой все следующие.
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))

        run.__name__ = fn.__name__
        CHECKS.append(run)
        return run

    return wrap


def _reset_registry() -> None:
    """Гасит живых и стирает базу перед каждой проверкой: процесс один,
    а состояние друг от друга проверки наследовать не должны."""
    for agent_id in list(REGISTRY._agents):
        REGISTRY._unload(agent_id)
    REGISTRY.store.clear()


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


def _restart(store):
    """Новый реестр на том же файле — так выглядит перезапуск процесса."""
    registry = AgentRegistry(max_agents=1000, store=store)
    main.REGISTRY = registry
    return registry


def _rows(store, session_id: str) -> list[tuple]:
    """(seq, role, content) реплик чата как они лежат в базе: ими проверяются
    нумерация и изоляция лент."""
    with store.reading() as conn:
        rows = conn.execute(
            "SELECT seq, role, content FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    return [(r["seq"], r["role"], r["content"]) for r in rows]


def _counter(store, key: str):
    """Значение счётчика прямо из таблицы `meta`: в приложении она нужна одному
    счётчику, и заводить ради проверки публичный геттер незачем."""
    with store.reading() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


def _errors(store, session_id: str) -> list:
    """Колонка `error` реплик чата по порядку — ею помечен оборванный ответ."""
    with store.reading() as conn:
        rows = conn.execute(
            "SELECT error FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
    return [row["error"] for row in rows]


# --- День 6: агент как отдельная сущность, сто агентов в одном процессе -------


@check("спавн ста агентов с разными конфигами в одном процессе")
def check_spawn_100():
    return _script("spawn_100.py", "ОК: сто агентов", 1)


@check("конфиг агента копируется вглубь: сто агентов не делят один extra_body")
def check_spec_deep_copy():
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


@check("агент живёт без веб-слоя: CLI говорит с ним и продолжает сохранённый чат")
def check_cli():
    """Самое короткое доказательство обоих дней: тот же класс работает без
    сервера, а `--session` поднимает разговор из базы — с его конфигом,
    а не с дефолтами командной строки."""
    import io

    from app import cli

    _stub.install(reply="запомнил")
    agent = cli.build_agent(cli._parse_args(["--model", "stub/m", "--label", "консоль"]))
    answer = asyncio.run(cli.ask(agent, "меня зовут Нина", io.StringIO()))
    assert answer == "запомнил", answer
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.id in {a.id for a in REGISTRY.list()}, "CLI-агент виден в реестре процесса"

    # Чат выгружен из памяти: продолжение поднимает его из базы.
    REGISTRY._unload(agent.id)
    again = cli.build_agent(cli._parse_args(["--session", agent.id, "--model", "другая/модель"]))
    assert again.id == agent.id, (again.id, agent.id)
    assert [t.content for t in again.history] == ["меня зовут Нина", "запомнил"], again.history
    assert again.spec.model == "stub/m", (
        f"продолжение сменило модель на {again.spec.model} — аргументы поверх "
        "сохранённого конфига применяться не должны"
    )

    listing = io.StringIO()
    cli._print_sessions(listing)
    assert agent.id in listing.getvalue(), listing.getvalue()

    try:
        cli.build_agent(cli._parse_args(["--session", "ag_99999"]))
        raise AssertionError("несуществующий чат должен честно падать")
    except SystemExit:
        pass
    return "ответ напечатан, история записана, --session поднял чат из базы с его конфигом"


# --- История: помнится и уезжает в модель целиком ------------------------------


@check("в модель уезжает вся история: ни хвоста, ни отсечки по росту")
def check_whole_history_goes_to_model():
    """Управление контекстом — задание Дня 9; здесь обрезки нет вовсе.
    Пороги выше прежних отсечек (20 в окне, 400 хранимых): вернись любая
    из них — станет красно."""
    _stub.install(reply=lambda m, i: f"ответ {i}")

    # 1. Живой маршрут: 25 обменов — больше прежнего окна по умолчанию.
    turns = 25
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИС")
        for i in range(turns):
            response = client.post(
                f"/api/agents/{agent_id}/messages", json={"text": f"вопрос {i}"}
            )
            assert response.status_code == 200, response.text

    sent = _stub.CALLS[-1]["messages"]
    # Системный промпт + вся переписка (по две реплики на обмен) + новый вопрос.
    assert [m["role"] for m in sent] == (
        ["system"] + ["user", "assistant"] * (turns - 1) + ["user"]
    ), [m["role"] for m in sent]
    assert sent[1]["content"] == "вопрос 0", sent[1]
    assert sent[-1]["content"] == f"вопрос {turns - 1}", sent[-1]
    assert [m["content"] for m in sent[1:-1:2]] == [f"вопрос {i}" for i in range(turns - 1)]

    agent = REGISTRY.require(agent_id)
    assert len(agent.history) == 2 * turns, len(agent.history)

    # 2. Рост истории: 500 реплик — больше прежнего потолка хранимого.
    long_chat = agent_module.Agent(AgentSpec(label="длинный", model="stub/model", system="СИС"))
    for i in range(500):
        long_chat.remember("user", f"реплика {i}")
    assert len(long_chat.history) == 500, "история подрезана при росте"
    assert long_chat.history[0].content == "реплика 0", "у истории отъели начало"

    prompt = long_chat.build_prompt("последний вопрос")
    assert len(prompt) == 502, len(prompt)
    assert prompt[1]["content"] == "реплика 0", prompt[1]
    return f"{len(sent)} сообщений в промпте после {turns} обменов, 500 реплик хранятся целиком"


@check("два параллельных запроса к одному агенту: 409, история не перемешана")
def check_parallel():
    _stub.install(reply=lambda m, i: f"ответ {i}", chunks=8, delay=0.02)

    async def scenario():
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://bench") as client:
            created = await client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "п"}}
            )
            agent_id = created.json()["agents"][0]["id"]
            first, second = await asyncio.gather(
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"}),
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "второй"}),
            )
            return agent_id, sorted([first.status_code, second.status_code])

    agent_id, codes = asyncio.run(scenario())
    assert codes == [200, 409], codes
    agent = REGISTRY.require(agent_id)
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert len(_stub.CALLS) == 1, f"в модель ушло {len(_stub.CALLS)} вызовов, а должен один"
    return f"коды {codes}, в истории 2 реплики, вызов к модели один"


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
        # Правка панели — часть чата, а не состояние вкладки: не запиши её
        # в базу, и после перезапуска вопрос ушёл бы со старым конфигом.
        stored = REGISTRY.store.load_session(agent_id)["config"]
        assert stored["system"] == "НОВЫЙ ПРОМПТ", stored
        assert stored["model"] == "новая/модель" and stored["temperature"] == 0.9, stored

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

        # Ноль — это заданный ноль, а не «не задано»: с require_parameters
        # отправить 0 и не отправить параметр — два разных списка провайдеров.
        client.patch(f"/api/agents/{agent_id}", json={"temperature": 0, "top_p": 0})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "четвёртый"})
        zero = _stub.CALLS[-1]["payload"]
        assert zero["temperature"] == 0.0 and zero["top_p"] == 0.0, zero

        # Кривой тип — 400 с текстом, а не 500 и не молчаливая отправка.
        assert client.patch(f"/api/agents/{agent_id}", json={"top_k": 0.5}).status_code == 400
        assert client.patch(f"/api/agents/{agent_id}", json={"stop": "СТОП"}).status_code == 400
        # true — не число: в Python True это int, и без отдельной проверки
        # «temperature: true» уехало бы к провайдеру единицей.
        assert (
            client.patch(f"/api/agents/{agent_id}", json={"temperature": True}).status_code == 400
        )
        # Менять можно только то, что в панели видно.
        assert client.patch(f"/api/agents/{agent_id}", json={"note": "х"}).status_code == 400
        # Пустое имя дало бы пустую строку в списке слева, пустой текст —
        # пустую реплику в истории, а лента в теле значила бы, что историю
        # диктует браузер, а не агент.
        assert client.patch(f"/api/agents/{agent_id}", json={"label": "  "}).status_code == 400
        assert client.post(
            f"/api/agents/{agent_id}/messages", json={"text": " "}
        ).status_code == 400
        with_feed = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"text": "привет", "messages": [{"role": "user", "content": "привет"}]},
        )
        assert with_feed.status_code == 400 and "только text" in with_feed.json()["detail"], with_feed.text

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


@check("тело каждого вызова: require_parameters стоит, usage запрошен")
def check_call_body_invariants():
    """Два правила, без которых день не состоится:

    - `provider.require_parameters` — без него OpenRouter вправе увести запрос
      к провайдеру, который молча проигнорирует temperature или stop;
    - `usage: {include: true}` — токены, цену и поставщика называет провайдер,
      и без просьбы их не пришлёт никто.

    По телу запроса на всех путях: сообщение, перегенерация, чат
    с параметрами и чат со своим `extra_body`."""
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        bare = new_agent(client)
        client.post(f"/api/agents/{bare}/messages", json={"text": "раз"})
        client.post(f"/api/agents/{bare}/regenerate")

        loaded = new_agent(client, temperature=0.7, stop=["СТОП"])
        client.post(f"/api/agents/{loaded}/messages", json={"text": "два"})

        # Закреплённый поставщик дополняет provider, а не затирает его:
        # extra_body мержится поверх, и require_parameters обязан уцелеть.
        pinned = new_agent(client, extra_body={"provider": {"order": ["openai"]}})
        client.post(f"/api/agents/{pinned}/messages", json={"text": "три"})

    assert len(_stub.CALLS) == 4, len(_stub.CALLS)
    for call in _stub.CALLS:
        payload = call["payload"]
        provider = payload.get("provider")
        assert provider and provider.get("require_parameters") is True, (
            f"вызов ушёл без provider.require_parameters: {provider!r}"
        )
        assert payload.get("usage") == {"include": True}, (
            f"вызов ушёл без просьбы о usage: {payload.get('usage')!r}"
        )
    assert _stub.CALLS[-1]["payload"]["provider"]["order"] == ["openai"], _stub.CALLS[-1]["payload"]

    # И то же самое напрямую, без веб-слоя: правила живут в build_payload,
    # а не в ручке, поэтому CLI и любой другой вызывающий получают их тоже.
    from app.llm import build_payload

    payload = build_payload(AgentSpec(label="без веба", model="stub/m"))
    assert payload["provider"]["require_parameters"] is True, payload["provider"]
    assert payload["usage"] == {"include": True}, payload
    return "4 вызова через ручки и один напрямую — оба правила на каждом"


# --- День 7: память переживает перезапуск, чаты изолированы -------------------


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
    agent = Agent(AgentSpec(**probes), store=store, context_length=128_000)
    for i in range(messages):
        agent.remember("user" if i % 2 == 0 else "assistant", f"реплика {i}", persist=False)
    agent.remember("assistant", "ответ", metrics={"provider": "stub"})
    agent.persist()
    agent_id = agent.id
    store.close()

    # Настоящее переоткрытие файла, а не тот же объект в памяти.
    again = Store(path).init()
    try:
        revived = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=again)
        lost = {
            f.name: (probes[f.name], getattr(revived.spec, f.name))
            for f in spec_fields(AgentSpec)
            if getattr(revived.spec, f.name) != probes[f.name]
        }
        assert not lost, f"поля конфига не пережили переоткрытие файла: {lost}"
        # Длину контекста каталог отдаёт сетевым запросом — ждать его
        # восстановлению нельзя, поэтому она тоже лежит в базе.
        assert revived.context_length == 128_000, revived.context_length

        assert len(revived.history) == messages + 1, f"из базы поднялось {len(revived.history)}"
        assert revived.history[0].content == "реплика 0", "у поднятого чата отъели начало"
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
    finally:
        again.close()
    return (
        f"{len(probes)} полей конфига и история из {messages + 1} реплик пережили "
        "переоткрытие файла, схема сходится с кодом"
    )


@check("чат поднимается из строки, которую писали не мы: чужое поле, битый JSON")
def check_foreign_row():
    """Строку в базе мог записать сервер другой версии — или правка руками.
    Подъём чата обязан пережить и незнакомый ключ в конфиге, и пропавшую
    модель, и битый JSON в колонке: уронить чтение — значит потерять
    сохранённый диалог, а он единственное, ради чего день делался.
    """
    from app.agent import Agent

    path = _temp_db("foreign")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="чужой", model="stub/m", system="СИС", top_k=7), store=store)
    agent.remember("user", "меня зовут Нина")
    agent.remember("assistant", "привет", metrics={"provider": "stub"})
    chat = agent.id

    # 1) Конфиг с полями, которых в этой версии нет: незнакомое отбрасывается
    #    молча, знакомое доезжает.
    with store.tx() as conn:
        conn.execute(
            "UPDATE sessions SET config = ? WHERE id = ?",
            (
                json.dumps(
                    {
                        "label": "чужой",
                        "model": "stub/m",
                        "system": "СИС",
                        "top_k": 7,
                        "history_limit": 4,
                        "seed_messages": [{"role": "user", "content": "чужое"}],
                    },
                    ensure_ascii=False,
                ),
                chat,
            ),
        )
    revived = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=chat, store=store)
    assert revived.spec.model == "stub/m" and revived.spec.top_k == 7, revived.spec
    assert revived.spec.system == "СИС", revived.spec.system
    assert [t.content for t in revived.history] == ["меня зовут Нина", "привет"], revived.history

    # 2) Конфиг без модели: чат всё равно виден и открывается — в нём лежит
    #    переписка, а модель пользователь выберет заново.
    with store.tx() as conn:
        conn.execute("UPDATE sessions SET config = ? WHERE id = ?", ('{"label": "без модели"}', chat))
    registry = AgentRegistry(max_agents=10, store=store)
    entries = registry.catalogue()
    assert [e["id"] for e in entries] == [chat], entries
    assert entries[0]["model"], "чат без модели исчез бы из списка слева"
    bare = registry.require(chat)
    assert [t.content for t in bare.history] == ["меня зовут Нина", "привет"], bare.history

    # 3) Битый JSON: конфиг падает на пустой, метрики — на «их нет»,
    #    а не роняют чтение целиком.
    with store.tx() as conn:
        conn.execute("UPDATE sessions SET config = 'не json' WHERE id = ?", (chat,))
        conn.execute("UPDATE messages SET metrics = '{битое' WHERE session_id = ?", (chat,))
    store.close()

    again = Store(path).init()
    try:
        assert again.load_session(chat)["config"] == {}, "битый конфиг не упал на пустой"
        assert [m["metrics"] for m in again.load_messages(chat)] == [None, None], (
            "битые метрики не упали на «их нет»"
        )
        second = AgentRegistry(max_agents=10, store=again)
        assert len(second.require(chat).history) == 2, "битая строка уронила подъём чата"
    finally:
        again.close()
    return "чужое поле отброшено, чат без модели открылся, битый JSON не уронил чтение"


@check("два столбца времени: заведение не сдвигается правкой, свежесть — сдвигается записью")
def check_session_timestamps():
    """Каждый столбец держит свой порядок: `created_at` — список слева
    (он по заведению), `updated_at` — `/сессии` в консоли (там свежие сверху).

    Оба теряются молча. `save_session` зовётся на каждую правку панели и
    не вправе двигать `created_at`, иначе переименованный чат прыгнет в конец
    списка. `save_history` обязан двигать `updated_at`, иначе разговор
    не поднимает чат наверх и список свежести замирает навсегда. И подъём
    чата из базы — тоже не заведение: открытый после перезапуска чат не должен
    уезжать в конец.
    """
    from app.agent import Agent, Turn

    path = _temp_db("stamps")
    store = Store(path).init()
    older = {"model": "stub/m", "label": "старший"}
    store.save_session("ag_00001", label="старший", config=older, created_at=100.0)
    time.sleep(0.01)
    store.save_session("ag_00002", label="младший", config={"model": "stub/m"}, created_at=200.0)

    # Правка конфига живого чата: имя новое, время заведения прежнее.
    time.sleep(0.01)
    store.save_session("ag_00001", label="переименован", config=older, created_at=100.0)
    saved = store.load_session("ag_00001")
    assert saved["label"] == "переименован", saved["label"]
    assert saved["created_at"] == 100.0, (
        f"правка сдвинула время заведения на {saved['created_at']} — "
        "переименованный чат уедет в конец списка слева"
    )
    assert [row["id"] for row in store.list_sessions()] == ["ag_00001", "ag_00002"]

    # А теперь в младшем говорят: разговор обязан поднять его наверх.
    time.sleep(0.01)
    store.save_history(
        "ag_00002", [Turn(role="user", content="привет"), Turn(role="assistant", content="и тебе")]
    )
    order = [row["id"] for row in store.list_sessions()]
    assert order == ["ag_00002", "ag_00001"], (
        f"список свежести {order}: запись истории не сдвинула updated_at "
        "или порядок сортировки перевёрнут"
    )
    assert [row["history_len"] for row in store.list_sessions()] == [2, 0]

    # Подъём чата из базы — не заведение заново.
    lifted = Agent(AgentSpec(label="старший", model="stub/m"), agent_id="ag_00001", store=store)
    assert lifted.created_at == 100.0, (
        f"поднятый чат получил новое время заведения ({lifted.created_at}) — "
        "после перезапуска он уедет в конец списка слева"
    )
    assert store.load_session("ag_00001")["created_at"] == 100.0, "подъём переписал created_at"
    store.close()
    return "правка не трогает created_at, подъём — тоже; запись истории двигает updated_at"


@check("рассуждение видно в ленте, но не уходит ни в базу, ни обратно в модель")
def check_reasoning_and_transcript():
    """Стенограмма — то, из чего клиент рисует ленту и плитки: после каждого
    обмена он перечитывает агента и перерисовывает всё заново, а не полагается
    на дорисованное по дороге. Поэтому её формат — часть поведения: поле,
    пропавшее из реплики, — это молча переставшая рисоваться карточка.

    Рассуждение в ленте есть, а в базе и в следующем запросе — нет: в контекст
    оно не возвращается, а места занимает больше самого ответа.
    """
    _stub.install(reply="итоговый ответ", reasoning="я подумал про панду")
    with TestClient(main.app) as client:
        chat = new_agent(client)
        client.post(f"/api/agents/{chat}/messages", json={"text": "вопрос"})
        body = client.get(f"/api/agents/{chat}").json()

    transcript = body["transcript"]
    assert [t["role"] for t in transcript] == ["user", "assistant"], transcript
    for turn in transcript:
        for field in ("role", "content", "error", "reasoning", "metrics"):
            assert field in turn, f"в реплике нет поля {field}: {turn}"
    answer = transcript[-1]
    assert answer["content"] == "итоговый ответ" and answer["error"] is None, answer
    assert answer["reasoning"] == "я подумал про панду", answer
    assert answer["metrics"] and answer["metrics"]["provider"] == "stub", answer["metrics"]
    # По `history_len` клиент обновляет строку списка, не перечитывая ленту.
    assert body["history_len"] == len(transcript), (body["history_len"], len(transcript))

    # В базе рассуждения нет — ни колонкой, ни текстом.
    store = REGISTRY.store
    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(messages)")}
    assert "reasoning" not in columns, "рассуждению в базе не место: в контекст оно не входит"
    blob = " ".join(str(v) for row in store.conn.execute("SELECT * FROM messages") for v in row)
    assert "панду" not in blob, "рассуждение осело в базе"

    # И обратно в модель не уезжает: в контексте только ответ.
    _stub.reset()
    _stub.install(reply="второй ответ")
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{chat}/messages", json={"text": "ещё"})
    sent = " ".join(m["content"] for m in _stub.CALLS[0]["messages"])
    assert "панду" not in sent, f"рассуждение вернулось в контекст: {sent}"
    assert "итоговый ответ" in sent, sent

    # Оборванный ответ помечен, и ошибка доезжает до ленты.
    with TestClient(main.app) as client:
        broken = new_agent(client)
        hurt = REGISTRY.require(broken)
        hurt.remember("user", "вопрос")
        hurt.remember("assistant", "огрыз", error="оборвалось")
        failed = client.get(f"/api/agents/{broken}").json()["transcript"][-1]
    assert failed["error"] == "оборвалось", failed
    return f"{len(transcript)} реплики со всеми полями; рассуждения нет ни в базе, ни в промпте"


@check("изоляция чатов: у сообщений есть session_id, ленты не сливаются")
def check_session_isolation():
    _stub.install(reply=lambda m, i: f"ответ{i}")
    with TestClient(main.app) as client:
        first = new_agent(client, label="чат 1")
        second = new_agent(client, label="чат 2")
        client.post(f"/api/agents/{first}/messages", json={"text": "меня зовут Нина"})
        _stub.reset()
        client.post(f"/api/agents/{second}/messages", json={"text": "как меня зовут?"})

    asked = " ".join(m["content"] for m in _stub.CALLS[0]["messages"])
    assert "Нина" not in asked, f"вторая сессия видит чужую историю: {asked}"

    store = REGISTRY.store
    assert [r[2] for r in _rows(store, first)] == ["меня зовут Нина", "ответ0"]
    assert [r[2] for r in _rows(store, second)] == ["как меня зовут?", "ответ0"]

    # Вторая запись первого чата переписывает его историю целиком, начиная
    # с `DELETE`. Забыть в нём `WHERE session_id = ?` — значит стереть ленту
    # соседа, и набор обязан это увидеть.
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{first}/messages", json={"text": "и ещё раз"})
    assert [r[2] for r in _rows(store, second)] == ["как меня зовут?", "ответ0"], (
        "перезапись истории одного чата стёрла реплики соседнего"
    )

    # И тот же путь, которым лента поднимается после перезапуска: выборка
    # обязана быть по `session_id`. Условие, которое его не сужает, даёт
    # каждому чату все строки базы — ленты сливаются в одну.
    assert [m["content"] for m in store.load_messages(second)] == ["как меня зовут?", "ответ0"], (
        store.load_messages(second)
    )

    # Схема не даёт записать реплику без сессии: ключ составной, и это
    # единственная защита от «все чаты в одной ленте» после перезапуска.
    columns = {row[1]: row for row in store.conn.execute("PRAGMA table_info(messages)")}
    keys = [name for name, row in columns.items() if row[5]]
    assert keys == ["session_id", "seq"], keys
    indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(messages)")}
    assert "messages_by_session" in indexes, indexes
    for required in ("session_id", "seq", "role", "content", "at"):
        assert columns[required][3], f"{required} стал NULLable — реплику можно записать без него"
    return "две сессии — две ленты; PK (session_id, seq), индекс по session_id есть"


@check("в базу ложится ровно то, что случилось: seq от нуля, без дыр и половин")
def check_what_lands_in_db():
    """Номера строк — не отделка хранения: по ним история поднимается в том же
    порядке (`ORDER BY seq`), и дыра или сдвиг значат, что `save_history`
    дописывает хвост вместо того, чтобы переписать историю целиком. А хвост
    после отката оставил бы в базе ответ, которого в истории уже нет.

    Остальное — про то, что именно попадает в файл: несостоявшийся обмен
    не оставляет вопроса без ответа; оборванный «Стопом» оставляет пару
    с пометкой (он уже оплачен); неудачная перегенерация не плодит ни дублей,
    ни дыр, а удачная заменяет ответ.
    """
    from app.agent import Agent, Turn

    path = _temp_db("seq")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="seq", model="stub/m"), store=store)

    # 1) Ответа не случилось — в базе не появилось ничего, даже вопроса.
    _stub.install(fail=True)
    asyncio.run(drain(agent.ask("вопрос, на который не ответили")))
    assert _rows(store, agent.id) == [], _rows(store, agent.id)

    # 2) Обычные обмены: номера идут подряд и от нуля.
    _stub.install(reply="ок")
    for i in range(3):
        asyncio.run(drain(agent.ask(f"вопрос {i}")))
    seqs = [row[0] for row in _rows(store, agent.id)]
    assert seqs == list(range(6)), f"номера пошли с дырами или со сдвигом: {seqs}"

    # 3) Укорачивание истории — так выглядит откат обмена.
    agent.history = agent.history[-2:]
    agent.persist()
    rows = _rows(store, agent.id)
    assert [r[0] for r in rows] == [0, 1], f"после укорачивания номера не от нуля: {rows}"
    assert [r[2] for r in rows] == ["вопрос 2", "ок"], rows

    # 4) Длинный чат пишется целиком: ни отсечки, ни дыр в нумерации.
    long_chat = 500
    agent.history = [Turn(role="user", content=f"т{i}") for i in range(long_chat)]
    agent.persist()
    rows = _rows(store, agent.id)
    assert len(rows) == long_chat, f"историю подрезали при записи: {len(rows)}"
    assert [r[0] for r in rows] == list(range(long_chat)), "номера пошли с дырами"
    assert rows[0][2] == "т0", rows[0]
    store.close()

    # 5) «Стоп» посреди ответа: половина уже оплачена, и следующий вопрос
    #    обязан видеть, чем кончилось, — значит она в базе, но помечена.
    _stub.install(reply="а" * 200, chunks=20, delay=0.02)

    async def stopped():
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://bench") as client:
            created = await client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "стоп"}}
            )
            chat = created.json()["agents"][0]["id"]
            talking = asyncio.create_task(
                client.post(f"/api/agents/{chat}/messages", json={"text": "вопрос"})
            )
            await asyncio.sleep(0.1)
            halted = await client.post(f"/api/agents/{chat}/cancel")
            return chat, halted, await talking

    chat, halted, response = asyncio.run(stopped())
    assert halted.status_code == 200 and halted.json()["was_busy"] is True, halted.text
    done = next(e for e in sse(response.text) if e["event"] == "done")
    assert done["cancelled"] is True and done["error"] == "генерация отменена", done
    live = REGISTRY.store
    cut = _rows(live, chat)
    assert [(r[0], r[1]) for r in cut] == [(0, "user"), (1, "assistant")], cut
    assert 0 < len(cut[1][2]) < 200, len(cut[1][2])
    assert _errors(live, chat) == [None, "генерация отменена"], _errors(live, chat)

    # 6) Перегенерация: неудачная возвращает исходную пару, удачная заменяет
    #    ответ — в обоих случаях в базе ровно два номера, 0 и 1.
    _stub.install(reply="первый ответ")
    with TestClient(main.app) as client:
        again_id = new_agent(client)
        client.post(f"/api/agents/{again_id}/messages", json={"text": "мой вопрос"})
        before = _rows(live, again_id)
        assert before == [(0, "user", "мой вопрос"), (1, "assistant", "первый ответ")], before

        _stub.install(fail=True)
        failed = client.post(f"/api/agents/{again_id}/regenerate")
        assert failed.status_code == 200, failed.text
        assert next(e for e in sse(failed.text) if e["event"] == "done")["restored"] is True
        assert _rows(live, again_id) == before, _rows(live, again_id)

        _stub.install(reply="второй ответ")
        client.post(f"/api/agents/{again_id}/regenerate")
    replaced = _rows(live, again_id)
    assert replaced == [(0, "user", "мой вопрос"), (1, "assistant", "второй ответ")], replaced
    return f"нет ответа — нет записи; «Стоп» пишет половину с пометкой; seq 0..{long_chat - 1}"


@check("выгрузка — не удаление: чат сверх потолка виден в списке и открывается")
def check_eviction_keeps_session():
    """Главное различие дня. Вытеснение по потолку живых — это **выгрузка**:
    объект уходит из памяти, строка в базе остаётся, и обращение по id
    поднимает разговор с того же места. Удаление — это удаление, из обоих
    слоёв сразу, и только своё.

    Список слева строится по базе и потолка не имеет: обрезка резала бы
    по времени последней записи, то есть по чатам, с которыми ещё не
    говорили, — а список единственный путь к диалогу.
    """
    _stub.install(reply="ответ")
    path = _temp_db("eviction")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        registry = AgentRegistry(max_agents=3, store=store)
        main.REGISTRY = registry
        made = []
        first_object = None
        for i in range(10):
            agent = registry.create_many(
                [AgentSpec(label=f"чат {i}", model="stub/m", temperature=i / 10)],
                context_lengths={"stub/m": 128_000},
            )[0]
            asyncio.run(drain(agent.ask(f"вопрос {i}")))
            made.append(agent.id)
            first_object = first_object or agent

        assert len(registry) <= 3, f"в памяти {len(registry)} при потолке 3"
        assert registry.evicted >= 7, registry.evicted

        listing = asyncio.run(main.list_agents())
        assert [a["id"] for a in listing["agents"]] == made, listing["agents"]
        assert listing["stored"] == 10 and listing["live"] <= 3, listing
        assert all(a["history_len"] == 2 for a in listing["agents"]), listing["agents"]

        # Поднятый из базы чат — новый объект с прежним разговором и конфигом.
        revived = registry.require(made[0])
        assert revived is not first_object, "поднят тот же объект — выгрузки не было"
        assert [t.content for t in revived.history] == ["вопрос 0", "ответ"], revived.history
        assert revived.spec.temperature == 0.0, revived.spec.temperature
        assert revived.context_length == 128_000, revived.context_length

        # Выгруженный объект права писать в сессию лишается: иначе придержанная
        # ссылка затёрла бы реплики того, кого подняли на его место.
        assert first_object.store is None, "выгруженный обязан отцепиться от базы"
        first_object.remember("user", "мусор")
        assert [r[2] for r in _rows(store, made[0])] == ["вопрос 0", "ответ"]

        # `/api/health` считает чаты по базе, а не по памяти: выгруженные
        # из неё ушли, но сохранены.
        with TestClient(main.app) as client:
            health = client.get("/api/health").json()
            body = client.get(f"/api/agents/{made[0]}").json()
        assert [t["content"] for t in body["transcript"]] == ["вопрос 0", "ответ"], body
        assert health["agents_live"] <= 3 < health["sessions_stored"] == 10, health

        # Занятого не вытесняют никогда: его ответа кто-то ждёт.
        held = registry.require(made[9])
        held.reserve()
        registry.create_many([AgentSpec(label=f"н {i}", model="stub/m") for i in range(3)])
        assert registry.require(made[9]) is held, "занятого вытеснять нельзя"
        held.release()

        # Удаление — из обоих слоёв и только своё.
        assert registry.kill(made[0]) is True
        assert store.load_session(made[0]) is None, "kill обязан стереть и строку в базе"
        assert _rows(store, made[0]) == [], "реплики удалённого чата остались"
        assert [r[2] for r in _rows(store, made[1])] == ["вопрос 1", "ответ"], (
            "удаление чата стёрло реплики соседнего"
        )

        # Потолка у списка нет: обрезка была бы молчаливой.
        now = time.time()
        with store.tx():
            for i in range(1200):
                store.save_session(
                    f"chat_{i:05d}",
                    label=f"чат {i}",
                    config={"model": "stub/m", "label": f"чат {i}"},
                    created_at=now + i,
                )
        entries = asyncio.run(main.list_agents())["agents"]
        assert len(entries) == 1200 + 12, f"в списке {len(entries)} — список обрезан"
        assert registry.require(entries[-1]["id"]).spec.label == entries[-1]["label"]
    finally:
        main.REGISTRY = saved_registry
        store.close()
    return "10 чатов при потолке 3 все в списке и открываются; удаление хирургично"


@check("id чатов и номера имён выдаёт база, а не процесс")
def check_ids_come_from_db():
    """Консоль запускают рядом с сервером, файл у них один, и счётчик в памяти
    выдал бы обоим `ag_00004`: второй стёр бы диалог первого — `save_history`
    начинается с `DELETE`. Поэтому id занимается вставкой строки, а арбитр —
    первичный ключ; номер имени берётся из таблицы `meta` тем же порядком.
    Настоящие два процесса разбирает `checks/two_processes.py`.
    """
    from app.agent import Agent

    path = _temp_db("claim")
    store = Store(path).init()

    stranger = Agent(AgentSpec(label="чужой", model="stub/m"), store=store)
    stranger.remember("user", "СЕКРЕТ соседа")
    stranger.remember("assistant", "ответ соседа")
    taken = stranger.id

    # Откатываем счётчик ровно на этот номер: так и выглядит давно поднятый
    # сервер, мимо которого консоль успела занять следующий id.
    agent_module._last_id = int(taken.removeprefix("ag_")) - 1
    mine = Agent(AgentSpec(label="мой", model="stub/m"), store=store)
    assert mine.id != taken, f"выдан занятый id {taken}"
    assert [row[2] for row in _rows(store, taken)] == ["СЕКРЕТ соседа", "ответ соседа"]
    assert store.load_session(taken)["label"] == "чужой", "чужой конфиг перезаписан"

    # Разрыв в сотни номеров — обычное дело: консоль писала, пока сервер спал.
    # Перебором по одному его не пройти, поэтому на конфликте счётчик догоняет
    # базу разом; иначе попытки кончаются и чат не завести вовсе.
    occupied = 400
    with store.tx() as conn:
        for number in range(1, occupied + 1):
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, created_at, updated_at) VALUES (?, 1, 1)",
                (f"ag_{number:05d}",),
            )
    agent_module._last_id = 0
    caught_up = Agent(AgentSpec(label="догнал", model="stub/m"), store=store)
    assert int(caught_up.id.removeprefix("ag_")) > occupied, (
        f"{caught_up.id} не догнал базу, где заняты первые {occupied} номеров"
    )

    # Строку под id занимают до того, как агент достроился. Упал конструктор —
    # строку надо убрать, иначе в списке слева повиснет чат без объекта.
    before = store.count_sessions()
    saved_save = store.save_session
    store.save_session = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("бум на записи"))
    try:
        Agent(AgentSpec(label="недостроенный", model="stub/m"), store=store)
        raise AssertionError("конструктор должен был упасть")
    except RuntimeError as exc:
        assert "бум" in str(exc), exc
    finally:
        store.save_session = saved_save
    assert store.count_sessions() == before, "занятая строка не убрана"
    store.close()

    # Номер имени — тот же порядок: он лежит в базе и перезапуск переживает.
    numbers = Store(_temp_db("numbers")).init()
    saved_registry = main.REGISTRY
    try:
        _restart(numbers)
        with TestClient(main.app) as client:
            first = client.post("/api/agents", json={}).json()["agents"][0]["label"]
            client.post("/api/agents", json={}).json()
        # Смотрим не только на имена, но и на то, **где лежит число**: в одном
        # процессе счётчик в памяти «пережил» бы рестарт сам собой, и проверка
        # по именам ничего бы не поймала.
        assert _counter(numbers, main.CHAT_NUMBER_KEY) == "2", (
            "номер выдан не базой: после настоящего перезапуска счёт начнётся "
            "заново и выдаст занятое имя"
        )
        # Перезапуск процесса: счётчик в памяти начался бы с единицы заново.
        _restart(numbers)
        with TestClient(main.app) as client:
            third = client.post("/api/agents", json={}).json()["agents"][0]["label"]
            labels = [a["label"] for a in client.get("/api/agents").json()["agents"]]
        assert [first, third] == ["Новый чат 1", "Новый чат 3"], (first, third)
        assert _counter(numbers, main.CHAT_NUMBER_KEY) == "3", "номер выдан не базой"
        assert len(labels) == len(set(labels)), f"имена повторились: {sorted(labels)}"

        # `clear()` уносит и счётчики: оставленный номер отдал бы следующей
        # базе имя из середины ряда.
        numbers.clear()
        assert _counter(numbers, main.CHAT_NUMBER_KEY) is None, "clear() оставил счётчик имён"
    finally:
        main.REGISTRY = saved_registry
        numbers.close()
    return f"{taken} остался за соседом, свежий получил {mine.id}; имена — {first}, {third}"


@check("транзакция берёт блокировку сразу и откатывается целиком; занятая база — 503")
def check_tx_locks_immediately():
    """`BEGIN IMMEDIATE`, а не голый `BEGIN`.

    Отложенная транзакция берёт блокировку на первой записи. Начавшись
    с чтения, при повышении до записи она получает SQLITE_BUSY **мимо**
    `busy_timeout`: ретрая нет, и второй процесс получает ошибку вместо
    очереди. `two_processes.py` этого не ловит — там все пути записи
    начинаются с записи.

    Проверяется наблюдаемым: пока транзакция открыта и не сделала ни одного
    запроса, второй писатель обязан её видеть. Соединение берём с нулевым
    таймаутом — ждать нечего, нужен сам факт блокировки.

    И то, ради чего блокировка нужна: не дождавшийся своей очереди получает
    внятный 503 с объяснением, а не голый 500, и записи после себя
    не оставляет — ни на записи, ни на чтении.
    """
    from app.store import _busy

    # Обещание «половины обмена в базе не бывает» держит не `save_history`,
    # а откат: упала транзакция — в файле не должно остаться ни строки чата,
    # ни половины реплик. И база после отката обязана остаться рабочей:
    # незакрытая транзакция валит следующую запись на «transaction within
    # a transaction».
    class Boom(RuntimeError):
        pass

    rolled = Store(_temp_db("rollback")).init()
    try:
        with rolled.tx() as conn:
            conn.execute(
                "INSERT INTO sessions (id, label, created_at, updated_at) "
                "VALUES ('ag_00777', 'половина', 1, 1)"
            )
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, at) "
                "VALUES ('ag_00777', 0, 'user', 'вопрос без ответа', 1)"
            )
            raise Boom
    except Boom:
        pass
    assert rolled.load_session("ag_00777") is None, "строка недописанной транзакции осталась"
    assert _rows(rolled, "ag_00777") == [], "реплики недописанной транзакции остались"
    rolled.save_session("ag_00778", label="после отката", config={}, created_at=1.0)
    assert rolled.load_session("ag_00778")["label"] == "после отката", (
        "после отката база не пишет — транзакция осталась открытой"
    )
    rolled.close()

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
            saved_store = main.REGISTRY.store
            main.REGISTRY.store = waiting
            try:
                response = client.post("/api/agents", json={"agent": {"model": "stub/m"}})
            finally:
                main.REGISTRY.store = saved_store
        assert response.status_code == 503, (response.status_code, response.text)
        assert "занята другим процессом" in response.json()["detail"], response.text
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        waiting.close()

    reopened = Store(busy_path).init()
    try:
        assert reopened.list_sessions() == [], reopened.list_sessions()
    finally:
        reopened.close()

    # Чтение обязано объясниться так же. Под WAL читатель писателя не ждёт,
    # но эксклюзивную блокировку соседа переждать не может — и там, где
    # `reading()` перестанет переводить ошибку, наружу поедет голый 500.
    locked = _temp_db("busy-read")
    Store(locked).init().close()
    keeper = sqlite3.connect(str(locked), isolation_level=None)
    keeper.execute("PRAGMA busy_timeout=0")
    keeper.execute("PRAGMA locking_mode=EXCLUSIVE")
    keeper.execute("BEGIN IMMEDIATE")
    keeper.execute("INSERT INTO sessions (id, created_at, updated_at) VALUES ('ag_00001', 1, 1)")
    try:
        on_read = None
        try:
            Store(locked).list_sessions()
        except StoreBusyError as exc:
            on_read = exc
        assert on_read is not None, "чтение занятой базы упало голым sqlite3"
        assert "занята другим процессом" in str(on_read), str(on_read)
    finally:
        keeper.execute("ROLLBACK")
        keeper.close()
    return (
        "упавшая транзакция не оставила ни строки; открытая видна второму "
        "писателю сразу; занятая база даёт 503 и на записи, и на чтении"
    )


# --- Сквозное: ключ, сеть, клиент ---------------------------------------------


@check("ключа нет ни в интерфейсе и ни в одном ответе; без ключа вызова нет")
def check_no_key_leak():
    client_src = (
        read("app/static/app.js") + read("app/static/index.html") + read("app/static/style.css")
    ).lower()
    for word in ("api key", "api_key", "apikey", "sk-or", "openrouter_api"):
        assert word not in client_src, f"в клиенте упоминается «{word}»"

    # Подставляем заведомо ненастоящую строку в форме ключа и смотрим,
    # не вылезет ли она в ответах ручек. Настоящий ключ проверке не нужен.
    import app.config as config

    saved = config.api_key
    config.api_key = lambda: "sk-or-v1-ЭТО-НЕ-КЛЮЧ-А-ПРИМАНКА-ДЛЯ-ПРОВЕРКИ"
    try:
        with TestClient(main.app) as client:
            agent_id = client.post("/api/agents", json={}).json()["agents"][0]["id"]
            bodies = [
                client.get("/api/agents").text,
                client.get("/api/health").text,
                client.get(f"/api/agents/{agent_id}").text,
            ]
    finally:
        config.api_key = saved
    for body in bodies:
        assert "sk-or-v1" not in body, "ключ утёк в ответ ручки"

    # Свежий клон без .env — обычное состояние, и в нём вызова к модели быть
    # не должно: 503 с объяснением вместо запроса, который всё равно вернёт 401.
    _stub.install(reply="ок")
    saved_has_key = main.has_key
    main.has_key = lambda: False
    try:
        with TestClient(main.app) as client:
            chat = new_agent(client)
            blocked = client.post(f"/api/agents/{chat}/messages", json={"text": "?"})
            repeated = client.post(f"/api/agents/{chat}/regenerate")
            listing = client.get("/api/agents").json()
    finally:
        main.has_key = saved_has_key
    assert blocked.status_code == 503, blocked.text
    assert repeated.status_code == 503, repeated.text
    assert listing["has_key"] is False, listing
    assert not _stub.CALLS, f"до модели дошло {len(_stub.CALLS)} вызовов"
    assert REGISTRY.require(chat).busy is False, "бронь не должна залипнуть на отказе"
    return "в клиенте про ключ ни слова, ручки его не отдают, без ключа — 503"


@check("ключа OpenRouter в базе нет ни в одной колонке — включая ещё не придуманные")
def check_no_key_in_db():
    """Ключ подставляется во все текстовые пути, а не только туда, где есть redact()."""
    from app.agent import Agent

    key = "sk-or-v1-ТЕСТОВЫЙ-КЛЮЧ-КОТОРЫЙ-НЕ-ДОЛЖЕН-УТЕЧЬ"
    saved_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = key
    path = _temp_db("secret")
    store = Store(path).init()
    try:
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

        # А это — про колонки, которых ещё нет: любая запись идёт через
        # транзакцию, и параметр чистится независимо от того, вспомнил ли
        # автор про redact() в этом конкретном методе.
        with store.tx() as conn:
            conn.execute("UPDATE sessions SET label = ? WHERE id = ?", (key, agent.id))
            conn.execute("INSERT INTO meta (key, value) VALUES ('ловушка', ?)", (key,))

        leaked = []
        for table in ("sessions", "messages", "meta"):
            for row in store.conn.execute(f"SELECT * FROM {table}"):
                for name in row.keys():
                    if isinstance(row[name], str) and key in row[name]:
                        leaked.append(f"{table}.{name}")
        assert not leaked, f"ключ лежит в колонках: {sorted(set(leaked))}"

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

        # У редакции есть нижний порог: она работает подстрокой и чистит
        # **любой** строковый параметр, включая id и роль реплики. С вырожденным
        # ключом (буквально `1`) она изрезала бы `ag_00001` в `ag_***0000***`.
        from app.store import MIN_SECRET_LENGTH, redact

        os.environ["OPENROUTER_API_KEY"] = "1"
        assert redact("ag_00001") == "ag_00001" and redact("assistant") == "assistant"
        assert len(key) >= MIN_SECRET_LENGTH
    finally:
        if saved_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved_key
    return f"ключ не найден ни в одной колонке и ни в одном файле базы ({', '.join(files)})"


@check("мимо redact() записать нельзя: пути записи выводятся из класса")
def check_every_write_path_redacts():
    """Несущий слой чистки — не явные `redact()` в отдельных методах, а `_Writer`:
    транзакция отдаёт обёртку, и чистится любой строковый параметр любого
    запроса. Но само это свойство надо стеречь отдельно: `check_no_key_in_db`
    ходит теми путями записи, которые знает, а новый путь мимо транзакции
    прошёл бы зелёным.

    Поэтому список путей здесь **выводится** из класса `Store`: всякий метод,
    в теле которого есть INSERT/UPDATE/DELETE/REPLACE, обязан идти через
    `tx()`, а не через голое соединение. Перечисленный список пропустил бы
    ровно тот метод, который забыли в него внести.
    """
    import inspect

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
    assert len(checked) >= 5, f"путей записи нашлось всего {checked} — обход сузился"

    # Транзакция отдаёт обёртку, а не соединение: иначе обещание держалось бы
    # на внимательности автора каждого метода.
    store = Store(store_module.MEMORY).init()
    key = "sk-or-v1-" + "e" * 64
    saved_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = key
    try:
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
        leaked = []
        for table in ("sessions", "messages", "meta"):
            for row in store.conn.execute(f"SELECT * FROM {table}"):
                for column in row.keys():
                    if isinstance(row[column], str) and key in row[column]:
                        leaked.append(f"{table}.{column}")
        assert not leaked, f"ключ уехал в базу через tx(): {sorted(set(leaked))}"
    finally:
        if saved_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved_key
        store.close()
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
