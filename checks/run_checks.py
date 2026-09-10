"""Все проверки Дней 6 и 7 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Главный критерий дня вынесен в отдельный скрипт (`checks/spawn_100.py`) и
запускается отсюда же первым пунктом.
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
from app.llm import SAMPLING_FIELDS  # noqa: E402
from app.registry import REGISTRY  # noqa: E402
from app.schema import AgentSpec  # noqa: E402
from app.store import Store  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


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


# --- 1. главный критерий дня --------------------------------------------------


@check("спавн ста агентов с разными конфигами в одном процессе")
def check_spawn_100():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "checks", "spawn_100.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ОК: сто агентов" in result.stdout, result.stdout
    return result.stdout.strip().splitlines()[1]


@check("список плоский и на чистом старте пуст")
def check_flat_list():
    with TestClient(main.app) as client:
        data = client.get("/api/agents").json()
        assert "groups" not in data, "групп в ответе быть не должно"
        assert data["agents"] == [], f"на старте чатов быть не должно: {data['agents']}"

        # Чат, заведённый руками, — обычный: его правят, переименовывают,
        # удаляют, и никакой особости у него нет.
        agent = client.post("/api/agents", json={}).json()["agents"][0]
        for gone in ("group", "note", "origin", "draft"):
            assert gone not in agent, f"наружу торчит поле {gone}"
        renamed = client.patch(f"/api/agents/{agent['id']}", json={"label": "Просто чат"})
        assert renamed.status_code == 200 and renamed.json()["label"] == "Просто чат"
        assert client.delete(f"/api/agents/{agent['id']}").status_code == 200
        assert client.get("/api/agents").json()["agents"] == []

    js = read("app/static/app.js")
    for gone in ("list-group", "state.groups", "agent.note", "agent.draft"):
        assert gone not in js, f"в клиенте осталась механика заготовок: {gone}"
    assert "list-group" not in read("app/static/style.css"), "в стилях остался .list-group"
    return "на старте пусто, заведённый чат ничем не особенный"


@check("у свежего чата нет системного промпта, и в теле его нет вовсе")
def check_new_chat_is_blank():
    """Стенд ничего не решает за пользователя — промпт тоже.

    Проверяется не только поле в ответе ручки, но и то, что ушло в модель:
    пустой `system` обязан пропасть из промпта целиком. Пустая строка в роли
    `system` — это не «промпта нет», это заданный пустой промпт, и модель
    получила бы лишнее сообщение ни о чём.
    """
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        assert fresh["system"] == "", f"свежий чат несёт промпт: {fresh['system']!r}"
        full = client.get(f"/api/agents/{fresh['id']}").json()
        assert full["transcript"] == [], full["transcript"]

        client.post(f"/api/agents/{fresh['id']}/messages", json={"text": "вопрос"})
        sent = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in sent] == ["user"], sent
        assert not any(m["role"] == "system" for m in sent), sent

        # А заданный руками — доезжает: пустота здесь не запрет, а умолчание.
        client.patch(f"/api/agents/{fresh['id']}", json={"system": "МОЙ ПРОМПТ"})
        _stub.reset()
        client.post(f"/api/agents/{fresh['id']}/messages", json={"text": "ещё"})
        after = _stub.CALLS[-1]["messages"]
        assert after[0] == {"role": "system", "content": "МОЙ ПРОМПТ"}, after[0]

    assert "system=" not in read("app/main.py").split("NEW_CHAT_SPEC")[1].split(")")[0], \
        "в NEW_CHAT_SPEC вернулся системный промпт"
    return "свежий чат пуст, промпт появляется только заданный руками"


@check("заготовок дней 1–5 не осталось нигде")
def check_no_presets():
    """Заготовки появились ради вопроса «как открыть агентов прошлых дней».

    Ответ оказался дороже вопроса: ради двадцати одного чата в ветке жил
    целый слой. Теперь их заводят руками, а слоя быть не должно — ни кода,
    ни файла с описаниями, ни проверок, которые его стерегли.
    """
    assert not os.path.exists(os.path.join(ROOT, "day.py")), "day.py жив"
    assert not os.path.exists(os.path.join(ROOT, "checks", "_daysrc.py")), "_daysrc.py жив"

    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    # .md здесь наравне с кодом: README описывает устройство стенда, и
    # заготовки, оставшиеся в описании, — такой же след, как в коде.
    code = [
        name
        for name in tracked
        if name.endswith((".py", ".js", ".html", ".css", ".md"))
        and os.path.isfile(os.path.join(ROOT, name))
    ]
    # Слова собраны из кусков намеренно: иначе сама проверка стала бы
    # последним местом, где упоминание осталось.
    words = [f"День {n}" for n in range(1, 6)] + ["PRESET" + "_CHATS", "bootstrap" + "_chats"]
    hits = []
    for name in code:
        body = open(os.path.join(ROOT, name), encoding="utf-8").read()
        for word in words:
            if word in body:
                hits.append(f"{name}: {word}")
    assert not hits, "упоминания заготовок остались — " + "; ".join(hits)
    assert "draft" not in read("app/schema.py"), "черновик остался в конфиге"
    return f"проверено {len(code)} файлов с кодом, упоминаний нет"


@check("сценарии выпилены: ни ручек, ни кода, ни следов в клиенте")
def check_scenarios_gone():
    with TestClient(main.app) as client:
        for path in ("/api/scenarios", "/api/run/0"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/scenarios/0/agents").status_code == 404

    assert not os.path.exists(os.path.join(ROOT, "app", "commands.py")), "app/commands.py жив"
    server = read("app/main.py") + read("app/agent.py") + read("app/schema.py")
    for word in ("Scenario", "judge", "depends_on", "repeats", "прогон"):
        assert word not in server, f"в серверном коде остался {word}"

    # Родительские связи жили ради субагентов прогона. Спавнить детей больше
    # некому, и держать каскад, достижимый только из проверок, незачем.
    registry = read("app/registry.py")
    for word in ("parent_id", "kill_children", "children"):
        assert word not in registry, f"в реестре остался {word} — его никто не выставляет"
    with TestClient(main.app) as client:
        agent = client.post("/api/agents", json={}).json()["agents"][0]
        assert "parent_id" not in agent, "наружу отдаётся мёртвое поле parent_id"
    client_src = (read("app/static/app.js") + read("app/static/index.html")).lower()
    for word in ("scenario", "прогон", "судья", "колонк"):
        assert word not in client_src, f"в клиенте остался {word}"
    return "ручки отдают 404, слов Scenario/judge/depends_on/repeats в коде нет"


# --- 4. агент: память, окно, откат, 409, обрыв --------------------------------


@check("диалог помнит предыдущее: в третьем запросе виден первый вопрос")
def check_memory():
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИСТЕМА")
        for text in ("меня зовут Нина", "мне 33 года", "как меня зовут?"):
            response = client.post(f"/api/agents/{agent_id}/messages", json={"text": text})
            assert response.status_code == 200, response.text

    assert len(_stub.CALLS) == 3, len(_stub.CALLS)
    first, second, third = (c["messages"] for c in _stub.CALLS)
    assert [m["role"] for m in first] == ["system", "user"], first
    roles = [m["role"] for m in third]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"], roles
    assert third[1]["content"] == "меня зовут Нина", third[1]
    assert third[-1]["content"] == "как меня зовут?", third[-1]
    assert len(second) == 4, second
    return "3-й запрос: 6 сообщений, первый вопрос в контексте"


@check("окно памяти режет: history_limit=0 отвечает каждый вопрос как первый")
def check_history_window():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        blank = new_agent(client, history_limit=0, system="СИС")
        narrow = new_agent(client, history_limit=2, system="СИС")
        for agent_id in (blank, narrow):
            for text in ("первый", "второй", "третий"):
                client.post(f"/api/agents/{agent_id}/messages", json={"text": text})

    calls = [c["messages"] for c in _stub.CALLS]
    assert all(len(m) == 2 for m in calls[:3]), [len(m) for m in calls[:3]]
    # Окно 2 — это один обмен: системный промпт, пара из истории и новый вопрос.
    assert [len(m) for m in calls[3:]] == [2, 4, 4], [len(m) for m in calls[3:]]
    assert calls[5][1]["content"] == "второй", calls[5][1]
    return "окно 0 — по 2 сообщения, окно 2 — 2/4/4"


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


@check("несостоявшийся обмен: история не тронута, вопрос возвращён клиенту")
def check_rollback():
    _stub.install(fail=True)

    async def scenario():
        agent = REGISTRY.create(AgentSpec(label="падение", model="stub/model"))
        return agent, await drain(agent.ask("вопрос, который не доедет"))

    agent, events = asyncio.run(scenario())
    done = [e for e in events if e["type"] == "done"][0]
    assert agent.history == [], agent.history
    assert done["committed"] is False, done
    assert done["question"] == "вопрос, который не доедет", done

    async def partial():
        agent = REGISTRY.create(AgentSpec(label="частичный", model="stub/model"))

        async def half(session, *, prompt_override=None, context_length=None):
            yield {"type": "delta", "text": "полов", "metrics": {"error": None}}
            yield {"type": "error", "message": "оборвалось", "metrics": {"error": "оборвалось"}}

        agent_module.stream_completion = half
        await drain(agent.ask("вопрос"))
        return agent

    agent = asyncio.run(partial())
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.history[1].content == "полов", agent.history[1].content
    assert agent.history[1].error == "оборвалось", agent.history[1].error
    return "ответа нет → история пуста и вопрос вернулся; частичный → помечен ошибкой"


@check("обрыв клиента гасит вызов и снимает бронь")
def check_disconnect():
    _stub.install(reply="а" * 400, chunks=40, delay=0.02)

    class Gone:
        """Клиент, которого не стало на N-м опросе."""

        def __init__(self, after: int) -> None:
            self.after = after
            self.polls = 0

        async def is_disconnected(self) -> bool:
            self.polls += 1
            return self.polls > self.after

    async def scenario(after: int):
        agent = REGISTRY.create(AgentSpec(label="обрыв", model="stub/model"))
        agent.reserve()
        frames = [
            frame
            async for frame in main._pump(
                lambda: main._chat_events(agent, "вопрос"), Gone(after), agent.release
            )
        ]
        await asyncio.sleep(0.1)
        return agent, frames

    agent, frames = asyncio.run(scenario(3))
    assert _stub.ACTIVE["closed"] == 1, _stub.ACTIVE
    assert _stub.ACTIVE["now"] == 0, _stub.ACTIVE
    assert agent.busy is False, "бронь должна сниматься и на обрыве"

    # Разрыв до первого события: генератор не запускался вовсе, а бронь всё
    # равно обязана сняться — иначе агент навсегда останется занятым.
    _stub.reset()
    _stub.install(reply="б" * 200, chunks=20, delay=0.02)
    early, early_frames = asyncio.run(scenario(0))
    assert early_frames == [], early_frames
    assert early.busy is False, "бронь залипла на разрыве до первого события"
    return f"поток закрыт после {len(frames)} кадров, стрим погашен, бронь снята"


# --- 5. новые параметры -------------------------------------------------------

NEW_PARAMS = {
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.05,
    "repetition_penalty": 1.1,
    "presence_penalty": 0.5,
    "frequency_penalty": 0.25,
}


@check("новые параметры доезжают до тела запроса, а незаданные — нет")
def check_new_params():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        bare = new_agent(client)
        client.post(f"/api/agents/{bare}/messages", json={"text": "привет"})
        empty_payload = _stub.CALLS[-1]["payload"]
        for name in ("temperature", "max_tokens", *NEW_PARAMS):
            assert name not in empty_payload, f"{name} уехал в тело, хотя задан не был"

        full = new_agent(client, temperature=0.4, max_tokens=100, **NEW_PARAMS)
        client.post(f"/api/agents/{full}/messages", json={"text": "привет"})
        payload = _stub.CALLS[-1]["payload"]
        for name, value in NEW_PARAMS.items():
            assert payload.get(name) == value, (name, payload.get(name))

        # Ноль — это заданный ноль, а не «не задано».
        zero = new_agent(client, presence_penalty=0.0, temperature=0.0)
        client.post(f"/api/agents/{zero}/messages", json={"text": "привет"})
        payload = _stub.CALLS[-1]["payload"]
        assert payload["presence_penalty"] == 0.0, payload
        assert payload["temperature"] == 0.0, payload

        # PATCH с null снимает параметр: он перестаёт уходить вовсе.
        client.patch(f"/api/agents/{full}", json={"top_k": None, "min_p": None})
        client.post(f"/api/agents/{full}/messages", json={"text": "ещё"})
        payload = _stub.CALLS[-1]["payload"]
        assert "top_k" not in payload and "min_p" not in payload, payload
        assert payload["top_p"] == 0.9, payload

        # Кривой тип — 400 с текстом, а не 500 и не молчаливая отправка.
        assert client.patch(f"/api/agents/{full}", json={"top_k": 0.5}).status_code == 400
        assert client.patch(f"/api/agents/{full}", json={"top_p": "быстро"}).status_code == 400
    return "шесть новых параметров едут, незаданные отсутствуют, ноль отличим от пустоты"


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


@check("правка в панели применяется к следующему сообщению, а не к следующему чату")
def check_panel_applies_next_message():
    """Жалоба заказчика: сменил системный промпт — уезжает старый.

    Проверяем не ответ ручки, а то, что реально ушло в модель: и промпт,
    и каждое поле панели, и окно памяти.
    """
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, model="старая/модель", system="СТАРЫЙ ПРОМПТ")
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"})
        assert _stub.CALLS[-1]["messages"][0]["content"] == "СТАРЫЙ ПРОМПТ"

        patched = client.patch(
            f"/api/agents/{agent_id}",
            json={"system": "НОВЫЙ ПРОМПТ", "history_limit": 0, **PANEL_FIELDS},
        )
        assert patched.status_code == 200, patched.text

        _stub.reset()
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "второй"})

    call = _stub.CALLS[-1]
    sent, payload = call["messages"], call["payload"]
    assert sent[0]["role"] == "system", sent
    assert sent[0]["content"] == "НОВЫЙ ПРОМПТ", sent[0]["content"]
    assert call["model"] == "новая/модель", call["model"]
    for name, value in PANEL_FIELDS.items():
        if name == "model":
            continue
        assert payload.get(name) == value, (name, payload.get(name), value)
    # history_limit=0 — окно тоже применилось: в промпте только промпт и вопрос.
    assert [m["role"] for m in sent] == ["system", "user"], sent
    return "промпт, модель, окно и все параметры уехали новыми"


@check("правка панели во время генерации не теряется")
def check_patch_during_generation():
    """Находка ревью: PATCH на занятом агенте отдавал 409 и правка пропадала.

    Повторить её было нечем — единственный триггер применения уже отработал,
    и следующее сообщение уезжало со старым промптом при новом тексте
    в панели. Ровно та жалоба, с которой начинался девятый пункт.

    Запрет был не нужен: `stream_completion` собирает тело запроса и метрики
    синхронно, до первого await, поэтому правка конфига текущий ответ
    и не могла бы исказить.
    """

    async def scenario():
        from httpx import ASGITransport, AsyncClient

        _stub.install(reply="д" * 400, chunks=40, delay=0.02)
        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://bench") as client:
            created = await client.post(
                "/api/agents",
                json={"agent": {"model": "stub/model", "label": "п", "system": "СТАРЫЙ ПРОМПТ"}},
            )
            agent_id = created.json()["agents"][0]["id"]

            generating = asyncio.create_task(
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"})
            )
            await asyncio.sleep(0.15)
            patched = await client.patch(
                f"/api/agents/{agent_id}",
                json={"system": "НОВЫЙ ПРОМПТ", "temperature": 0.9},
            )
            await generating
            current = list(_stub.CALLS)

            _stub.reset()
            _stub.install(reply="ок")
            await client.post(f"/api/agents/{agent_id}/messages", json={"text": "второй"})
            return patched, current, list(_stub.CALLS)

    patched, current, following = asyncio.run(scenario())

    assert patched.status_code == 200, f"правку во время генерации отвергли: {patched.text}"

    # Текущий ответ правка не задела: тело запроса снято слепком на старте.
    assert [m["content"] for m in current[0]["messages"] if m["role"] == "system"] == [
        "СТАРЫЙ ПРОМПТ"
    ], current[0]["messages"]
    assert "temperature" not in current[0]["payload"], current[0]["payload"]

    # А следующее сообщение ушло уже новым.
    assert [m["content"] for m in following[0]["messages"] if m["role"] == "system"] == [
        "НОВЫЙ ПРОМПТ"
    ], following[0]["messages"]
    assert following[0]["payload"]["temperature"] == 0.9, following[0]["payload"]

    # Клиент, кроме того, не отправит сообщение, пока правка не доехала.
    js = read("app/static/app.js")
    assert "panelDirty" in js, "клиент не помнит о недоехавшей правке"
    assert "сообщение не отправлено" in js, "клиент отправляет при непринятой правке"
    return "PATCH во время генерации принят, текущий ответ цел, следующий — новый"


@check("обмен идёт целиком на одном конфиге: смешанного запроса не бывает")
def check_config_snapshot():
    """Находка ревью: конфиг читается в двух точках, а не в одной.

    Промпт собирает `build_prompt`, тело — `build_payload`, и между ними
    стоит `yield` события `start`. Правка, попавшая туда, дала бы смешанный
    запрос: новую модель со старым системным промптом. Сейчас через этот
    `yield` никто не приостанавливается, но держится это на устройстве
    доставки событий, а не на самом обмене.

    Шагаем генератор руками — `__anext__` останавливает его ровно в окне —
    и правим конфиг оттуда: со слепком в запрос уезжает один конфиг целиком.
    """
    _stub.install(reply="ок")

    async def scenario():
        agent = REGISTRY.create(
            AgentSpec(
                label="слепок",
                model="старая/модель",
                system="СТАРЫЙ ПРОМПТ",
                temperature=0.1,
                max_tokens=100,
            )
        )
        stream = agent.ask("вопрос")
        start = await stream.__anext__()

        # Промпт уже собран, тело ещё нет — то самое окно.
        agent.spec.model = "новая/модель"
        agent.spec.system = "НОВЫЙ ПРОМПТ"
        agent.spec.temperature = 0.9
        agent.spec.max_tokens = 999

        async for _ in stream:
            pass
        return agent, start

    agent, start = asyncio.run(scenario())
    assert start["type"] == "start", start

    call = _stub.CALLS[0]
    systems = [m["content"] for m in call["messages"] if m["role"] == "system"]
    assert systems == ["СТАРЫЙ ПРОМПТ"], systems
    assert call["payload"]["model"] == "старая/модель", call["payload"]["model"]
    assert call["payload"]["temperature"] == 0.1, call["payload"]
    assert call["payload"]["max_tokens"] == 100, call["payload"]
    # Промпт в событии start — тот же, что уехал в модель.
    assert start["resolved_messages"] == call["messages"], (start["resolved_messages"], call["messages"])

    # А следующий обмен идёт уже целиком на новом конфиге.
    _stub.reset()
    _stub.install(reply="ок")
    asyncio.run(drain(agent.ask("второй")))
    call = _stub.CALLS[0]
    assert call["payload"]["model"] == "новая/модель", call["payload"]["model"]
    assert [m["content"] for m in call["messages"] if m["role"] == "system"] == ["НОВЫЙ ПРОМПТ"]
    assert call["payload"]["temperature"] == 0.9, call["payload"]

    source = read("app/agent.py")
    assert "copy_spec(self.spec)" in source, "обмен собирает запрос из живого конфига"
    return "правка в окне между промптом и телом не смешала конфиги"


@check("системный промпт не фиксируется при создании: в модель едет нынешний")
def check_system_prompt_single_home():
    """Корень жалобы «сменил промпт — уезжает старый».

    Раньше промпт мог приехать двумя путями: полем `system` и системным
    сообщением внутри `messages`. Второй дом фиксировал текст в момент
    создания агента: панель правила `spec.system`, а в модель уезжала копия
    из заготовки. Дома теперь физически один — поля `messages` у конфига
    больше нет, — и нарушить это нечем.

    Но само поведение, ради которого дом делали одним, проверять надо
    по-прежнему: промпт читается из конфига **на каждом обращении**,
    а не запоминается при создании. И переживает перезапуск: после
    переоткрытия файла в модель обязан ехать тот же текст.
    """
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="ИСХОДНЫЙ")
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"})
        assert _stub.CALLS[-1]["messages"][0] == {"role": "system", "content": "ИСХОДНЫЙ"}

        client.patch(f"/api/agents/{agent_id}", json={"system": "ПРАВЛЕНЫЙ"})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "второй"})
        sent = _stub.CALLS[-1]["messages"]
        systems = [m["content"] for m in sent if m["role"] == "system"]
        assert systems == ["ПРАВЛЕНЫЙ"], systems
        assert "ИСХОДНЫЙ" not in " ".join(m["content"] for m in sent), sent

        # И ещё раз, третьим сообщением: промпт не «применяется однажды».
        client.patch(f"/api/agents/{agent_id}", json={"system": "ТРЕТИЙ"})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "третий"})
        assert _stub.CALLS[-1]["messages"][0]["content"] == "ТРЕТИЙ", _stub.CALLS[-1]["messages"][0]

        # Снятый промпт исчезает из тела целиком: пустая строка в роли
        # `system` — это не «промпта нет», это заданный пустой промпт.
        client.patch(f"/api/agents/{agent_id}", json={"system": None})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "четвёртый"})
        assert not any(m["role"] == "system" for m in _stub.CALLS[-1]["messages"]), _stub.CALLS[-1]

    # Второго дома нет по построению: конфиг агента его не описывает,
    # а ручка создания не принимает.
    assert not hasattr(AgentSpec("л", "m"), "messages"), "у конфига снова есть messages"
    with TestClient(main.app) as client:
        extra = client.post(
            "/api/agents",
            json={"agent": {"model": "stub/m", "messages": [{"role": "system", "content": "х"}]}},
        )
        assert extra.status_code == 200, extra.text
        created = extra.json()["agents"][0]
        assert created["system"] == "", f"messages протекли в промпт: {created['system']}"
        client.post(f"/api/agents/{created['id']}/messages", json={"text": "?"})
        assert not any(m["role"] == "system" for m in _stub.CALLS[-1]["messages"]), _stub.CALLS[-1]
    return "промпт читается из конфига на каждом обращении; второго дома нет"


@check("stop и response_format правятся из панели и доезжают до тела запроса")
def check_stop_and_format():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "раз"})
        payload = _stub.CALLS[-1]["payload"]
        assert "stop" not in payload and "response_format" not in payload, payload

        client.patch(
            f"/api/agents/{agent_id}",
            json={"stop": ["КОНЕЦ", "СТОП"], "response_format": {"type": "json_object"}},
        )
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "два"})
        payload = _stub.CALLS[-1]["payload"]
        assert payload["stop"] == ["КОНЕЦ", "СТОП"], payload["stop"]
        assert payload["response_format"] == {"type": "json_object"}, payload["response_format"]

        # Пустое значение снимает параметр: он перестаёт уходить вовсе.
        client.patch(f"/api/agents/{agent_id}", json={"stop": None, "response_format": None})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "три"})
        payload = _stub.CALLS[-1]["payload"]
        assert "stop" not in payload and "response_format" not in payload, payload

        # Пустые строки в списке — не стоп-строки.
        client.patch(f"/api/agents/{agent_id}", json={"stop": ["", "  "]})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "четыре"})
        assert "stop" not in _stub.CALLS[-1]["payload"], _stub.CALLS[-1]["payload"]

        assert client.patch(f"/api/agents/{agent_id}", json={"stop": "СТОП"}).status_code == 400
        assert (
            client.patch(f"/api/agents/{agent_id}", json={"response_format": "json"}).status_code
            == 400
        )

    html = read("app/static/index.html")
    assert 'id="f-stop"' in html and 'id="f-response_format"' in html, "полей нет в панели"
    return "оба параметра задаются, снимаются и не уходят пустыми"


@check("создание и правка разбирают конфиг одинаково")
def check_create_and_patch_agree():
    """Дефект аудита: POST и PATCH расходились на пустых стоп-строках.

    POST сохранял ["", "  ", "КОНЕЦ"] как есть, PATCH выбрасывал пустые.
    Пустая стоп-строка не косметика: она остановила бы генерацию сразу,
    а с provider.require_parameters ещё и сузила бы список провайдеров.
    Ни одна проверка расхождения не ловила — обе ручки разбирали поля
    двумя независимыми кусками кода.

    Правильное поведение — то, которое было у PATCH и которого ждёт панель:
    поле стоп-строк построчное, и лишний перевод строки не параметр.
    `readStopLines` в клиенте делает ровно это.

    Проверяется не «PATCH чистит», а **согласие двух ручек**: любое поле,
    заданное при создании и той же правкой, обязано дать один конфиг.
    """
    cases = [
        {"stop": ["", "  ", "КОНЕЦ", " СТОП "]},
        {"stop": ["", "   "]},
        {"system": None},
        {"history_limit": 0},
        {"response_format": {"type": "json_object"}},
        {"temperature": 0.0, "top_p": 0, "max_tokens": 7},
    ]
    watched = ("stop", "system", "history_limit", "response_format", *SAMPLING_FIELDS)
    with TestClient(main.app) as client:
        for case in cases:
            created = client.post("/api/agents", json={"agent": {"model": "stub/model", **case}})
            assert created.status_code == 200, created.text
            born = created.json()["agents"][0]

            blank_id = new_agent(client)
            patched = client.patch(f"/api/agents/{blank_id}", json=case)
            assert patched.status_code == 200, patched.text
            grown = patched.json()

            for field in watched:
                assert born[field] == grown[field], (
                    f"{case}: поле {field} после создания {born[field]!r}, "
                    f"после правки {grown[field]!r} — ручки разбирают его по-разному"
                )

        # Согласие в отказах тоже: кривой тип обе ручки обязаны отвергнуть.
        for bad in ({"stop": "СТОП"}, {"top_k": 0.5}, {"history_limit": -1}):
            born = client.post("/api/agents", json={"agent": {"model": "stub/m", **bad}})
            grown = client.patch(f"/api/agents/{new_agent(client)}", json=bad)
            assert born.status_code == 400 and grown.status_code == 400, (
                bad, born.status_code, grown.status_code
            )

    # И то, ради чего чистка нужна: пустая стоп-строка не уезжает в модель.
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, stop=["", "  ", "КОНЕЦ"])
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"})
        assert _stub.CALLS[-1]["payload"]["stop"] == ["КОНЕЦ"], _stub.CALLS[-1]["payload"]
    return f"{len(cases)} конфигов и 3 отказа: создание и правка сходятся"


@check("любое поле конфига переживает переоткрытие файла, включая ещё не придуманные")
def check_config_survives_by_construction():
    """Дыра аудита: `_migrate` можно было удалить, не уронив ни одной проверки.

    Значит схему базы не стерёг никто: колонку можно было потерять молча,
    а «конфиг едет одним JSON-полем, поэтому новое поле сохраняется само»
    было обещанием без сторожа.

    Список полей здесь **выводится** из `AgentSpec`, а не перечисляется:
    перечисленный отстал бы от датакласса ровно тогда, когда поле добавили
    и забыли — то есть в единственном случае, ради которого проверка нужна.
    Значения подбираются по типу поля, тоже без ручного списка.
    """
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

    path = _temp_db("schema-roundtrip")
    store = Store(path).init()
    spec = AgentSpec(**probes)
    agent = Agent(spec, store=store)
    agent.remember("user", "вопрос")
    agent.remember("assistant", "ответ", metrics={"provider": "stub"})
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
        assert [t.content for t in revived.history] == ["вопрос", "ответ"], revived.history
        assert revived.history[1].metrics == {"provider": "stub"}, revived.history[1].metrics

        # Колонки схемы и колонки, которые читает код, — один набор.
        # Лишняя колонка так же плоха, как потерянная: она либо мёртвая,
        # либо её кто-то пишет мимо `_session_row`.
        in_db = {r["name"] for r in again.conn.execute("PRAGMA table_info(sessions)")}
        row = again.load_session(agent_id)
        expected = set(row) | {"updated_at"}
        assert in_db == expected, f"схема и код разошлись: в базе {in_db}, код читает {expected}"
    finally:
        again.close()
    return f"{len(probes)} полей конфига пережили переоткрытие файла, схема сходится с кодом"


@check("мимо redact() записать нельзя: пути записи выводятся из класса")
def check_every_write_path_redacts():
    """Дыра аудита: явные `redact()` в `save_session`/`save_history` можно было
    убрать, не уронив ничего.

    И правильно — несущий слой не они, а `_Writer`: транзакция отдаёт обёртку,
    и чистится любой строковый параметр. Но само это свойство никто не стерёг:
    `check_no_key_in_db` ходит теми путями записи, которые знает, а новый путь
    мимо транзакции прошёл бы зелёным.

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
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (:k, :v)", {"k": "т", "v": key}
            )
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


@check("панель правит живого агента: модель, промпт, окно памяти")
def check_patch_panel():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, model="stub/old", system="старый")
        patched = client.patch(
            f"/api/agents/{agent_id}",
            json={
                "model": "stub/new",
                "system": "новый промпт",
                "label": "Переименован",
                "history_limit": 4,
            },
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["model"] == "stub/new" and body["label"] == "Переименован"
        assert body["history_limit"] == 4
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "привет"})
        assert client.patch(f"/api/agents/{agent_id}", json={"note": "х"}).status_code == 400

    call = _stub.CALLS[-1]
    assert call["model"] == "stub/new", call["model"]
    assert call["messages"][0]["content"] == "новый промпт", call["messages"][0]
    return "PATCH меняет модель, промпт, имя и окно; чужие поля не принимаются"


# --- 6. лента: рассуждение и перегенерация ------------------------------------


@check("стенограмма — это ровно диалог, и в ней есть всё, что рисует клиент")
def check_transcript_shape():
    """Формат стенограммы не стерёг никто: поля можно было убрать молча.

    Так и вышло — удаление seed-реплик из `transcript()` и поля
    `seed_messages` из ответа ручки не уронило ни одной из 65 проверок.
    Клиент рисует ленту **из ответа ручки**, а не из того, что дорисовал
    по дороге: после каждого обмена он перечитывает агента и перерисовывает
    всё заново. Значит контракт стенограммы — часть поведения, и он такой:
    в ней ровно реплики диалога, по одной на ход, в порядке разговора,
    и у каждой есть поля, которые клиент читает.
    """
    _stub.install(reply="ответ", reasoning="я подумал")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИСТЕМА")
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос"})
        body = client.get(f"/api/agents/{agent_id}").json()

    transcript = body["transcript"]
    # Ровно диалог: системный промпт — это конфиг, а не реплика разговора,
    # и в ленте ему делать нечего. Он виден в панели, полем `system`.
    assert [t["role"] for t in transcript] == ["user", "assistant"], transcript
    assert body["system"] == "СИСТЕМА", body["system"]
    assert transcript[0]["content"] == "вопрос", transcript[0]
    assert transcript[1]["content"] == "ответ", transcript[1]
    assert "seed_messages" not in body, "наружу вернулась заготовка"

    # Поля, которые читает клиент. Убрать любое молча нельзя: карточка
    # перестанет показывать то, что показывала, а ошибку — вовсе проглотит.
    for turn in transcript:
        for field in ("role", "content", "error", "reasoning", "metrics"):
            assert field in turn, f"в реплике нет поля {field}: {turn}"
    answer = transcript[1]
    assert answer["reasoning"] == "я подумал", answer
    assert answer["error"] is None, answer
    assert answer["metrics"] and answer["metrics"]["provider"] == "stub", answer["metrics"]

    # Длина стенограммы и history_len — про одно и то же: клиент по второму
    # обновляет строку списка, не перечитывая ленту.
    assert body["history_len"] == len(transcript), (body["history_len"], len(transcript))

    # Оборванный ответ помечен ошибкой, и она доезжает до стенограммы.
    _stub.install(fail=True)
    with TestClient(main.app) as client:
        broken = new_agent(client)
        agent = REGISTRY.require(broken)
        agent.remember("user", "вопрос")
        agent.remember("assistant", "огрыз", error="оборвалось")
        failed = client.get(f"/api/agents/{broken}").json()["transcript"][-1]
    assert failed["error"] == "оборвалось", failed
    return f"{len(transcript)} реплики, поля на месте, ошибка доезжает"


@check("рассуждение приезжает отдельным событием и в ответ не входит")
def check_reasoning():
    _stub.install(reply="итоговый ответ", reasoning="я подумал вот так")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        events = sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": "привет"}).text)
        transcript = client.get(f"/api/agents/{agent_id}").json()["transcript"]

    names = [e["event"] for e in events]
    assert "reasoning" in names, names
    thought = next(e for e in events if e["event"] == "reasoning")
    assert thought["text"] == "я подумал вот так", thought
    done = next(e for e in events if e["event"] == "done")
    assert done["reasoning"] == "я подумал вот так", done
    assert done["text"] == "итоговый ответ", done

    answer = transcript[-1]
    assert answer["reasoning"] == "я подумал вот так", answer
    assert answer["content"] == "итоговый ответ", answer

    # Обратно в модель рассуждение не уходит: в контексте только ответ.
    _stub.reset()
    _stub.install(reply="второй ответ")
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "ещё"})
    sent = " ".join(m["content"] for m in _stub.CALLS[0]["messages"])
    assert "я подумал вот так" not in sent, sent
    assert "итоговый ответ" in sent, sent
    return "reasoning виден в потоке и в стенограмме, в контекст не возвращается"


@check("время до первого токена честное и на думающей модели")
def check_first_token():
    """Находка ревью: ttft_ms ставится на первом токене **ответа**.

    На reasoning-модели это момент, когда модель додумала, а не когда
    заговорила, и плитка удивляла бы на записи. Считаем отдельно первый
    токен вообще и показываем именно его.
    """
    _stub.install(reply="ответ", reasoning="я думаю")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        events = sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"}).text)

    done = next(e for e in events if e["event"] == "done")
    metrics = done["metrics"]
    assert "first_token_ms" in metrics, metrics
    assert metrics["first_token_ms"] is not None, metrics
    # Рассуждение приходит первым, значит первый токен не позже начала ответа.
    assert metrics["first_token_ms"] <= metrics["ttft_ms"], metrics

    js = read("app/static/app.js")
    assert "Первый токен, с" in js, "плитка должна называть то, что показывает"
    assert "first_token_ms" in js, "клиент обязан брать честное время, а не ttft"
    return "first_token_ms есть в метриках и стоит на плитке"


@check("перегенерация заменяет последний ответ, а не добавляет второй")
def check_regenerate():
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос"})
        before = client.get(f"/api/agents/{agent_id}").json()["transcript"]
        again = client.post(f"/api/agents/{agent_id}/regenerate")
        assert again.status_code == 200, again.text
        after = client.get(f"/api/agents/{agent_id}").json()["transcript"]

    assert len(before) == len(after) == 2, (len(before), len(after))
    assert [t["role"] for t in after] == ["user", "assistant"], after
    assert after[0]["content"] == "вопрос", after[0]
    assert after[1]["content"] != before[1]["content"], "ответ должен быть новым"
    # Второй вызов видел тот же контекст, что и первый: пара снята до вызова.
    assert _stub.CALLS[0]["messages"] == _stub.CALLS[1]["messages"], _stub.CALLS[1]["messages"]

    with TestClient(main.app) as client:
        empty = new_agent(client)
        assert client.post(f"/api/agents/{empty}/regenerate").status_code == 409
    return "после перегенерации в истории по-прежнему один вопрос и один ответ"


@check("неудачная перегенерация возвращает и вопрос, и прошлый ответ")
def check_regenerate_failure():
    """Находка ревью: пара снималась с истории до вызова и не возвращалась.

    Сценарий короткий и сам напрашивается: у агента закреплён провайдер
    через provider.order, смена модели роняет вызов, и «перегенерировать»
    уносило и вопрос, и уже полученный ответ. Восстановить их было нечем —
    историю хранит сервер.
    """
    _stub.install(reply="живой ответ")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "мой вопрос"})

        def history():
            body = client.get(f"/api/agents/{agent_id}").json()["transcript"]
            return [(t["role"], t["content"]) for t in body]

        before = history()
        assert before == [("user", "мой вопрос"), ("assistant", "живой ответ")], before

        # Вызов падает целиком, не отдав ни одного токена, — как HTTP 402.
        _stub.install(fail=True)
        response = client.post(f"/api/agents/{agent_id}/regenerate")
        assert response.status_code == 200, response.text
        events = sse(response.text)
        after = history()

    done = next(e for e in events if e["event"] == "done")
    assert done["committed"] is False, done
    assert done["restored"] is True, done
    assert after == before, f"история должна остаться прежней, а стала {after}"

    # Частичный ответ — другое дело: он записан, и возвращать старое поверх
    # нельзя, иначе в ленте окажется два ответа на один вопрос.
    async def partial():
        agent = REGISTRY.create(AgentSpec(label="частичная", model="stub/model"))
        agent.remember("user", "вопрос")
        agent.remember("assistant", "старый ответ")

        async def half(session, *, prompt_override=None, context_length=None):
            yield {"type": "delta", "text": "новый огрыз", "metrics": {"error": None}}
            yield {"type": "error", "message": "оборвалось", "metrics": {"error": "оборвалось"}}

        agent_module.stream_completion = half
        taken = agent.take_last_exchange()
        await drain(main._regenerate_events(agent, taken))
        return agent

    agent = asyncio.run(partial())
    pairs = [(t.role, t.content) for t in agent.history]
    assert pairs == [("user", "вопрос"), ("assistant", "новый огрыз")], pairs
    return "провал вернул пару на место, частичный ответ её заменил"


@check("обрыв до первого события не теряет снятую перегенерацией пару")
def check_regenerate_disconnect():
    """Дыра того же класса, что залипшая бронь, и закрыта тем же приёмом.

    `take_last_exchange` вызывается в обработчике, до `_stream`, а возврат
    жил только внутри генератора событий. Если клиент отвалился до первого
    опроса, генератор отменяется, не начав выполняться, и его `finally`
    не срабатывает никогда — пара уходила вместе с вопросом.
    """
    _stub.install(reply="новый ответ", chunks=10, delay=0.02)

    class Gone:
        """Клиента уже нет к моменту первого опроса."""

        async def is_disconnected(self) -> bool:
            return True

    async def scenario():
        agent = REGISTRY.create(AgentSpec(label="обрыв", model="stub/model"))
        agent.remember("user", "мой вопрос")
        agent.remember("assistant", "живой ответ")
        agent.reserve()
        taken = agent.take_last_exchange()
        assert taken is not None
        assert agent.history == [], "пара обязана сняться до вызова"
        frames = [
            frame
            async for frame in main._pump(
                lambda: main._regenerate_events(agent, taken),
                Gone(),
                # Ровно то, что вешает на поток сама ручка перегенерации.
                main._restore_and_release(agent, taken),
            )
        ]
        await asyncio.sleep(0.05)
        return agent, frames

    agent, frames = asyncio.run(scenario())
    assert frames == [], frames
    pairs = [(t.role, t.content) for t in agent.history]
    assert pairs == [("user", "мой вопрос"), ("assistant", "живой ответ")], pairs
    assert agent.busy is False, "бронь должна сниматься и здесь"
    assert not _stub.CALLS, "до модели дело дойти не должно было"
    return "0 кадров, пара на месте, бронь снята"


# --- 7. чаты: имена, удаление, отсутствие «очистить всё» ----------------------


@check("«Очистить все чаты» убрана вместе с ручкой и диалогом")
def check_no_clear_all():
    with TestClient(main.app) as client:
        # 405 — путь совпал с GET /api/agents/{id}: ручки reset всё равно нет.
        assert client.post("/api/agents/reset").status_code in (404, 405), "ручка reset жива"
    for path in ("app/static/app.js", "app/static/index.html"):
        source = read(path)
        for gone in ("Очистить все", "clear-all", "clearAll", "/api/agents/reset"):
            assert gone not in source, f"{path}: остался {gone}"
    return "ручки нет, кнопки нет, диалога очистки нет"


@check("имена по умолчанию нумеруются, автоимени нет")
def check_numbered_names():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        first = client.post("/api/agents", json={}).json()["agents"][0]
        second = client.post("/api/agents", json={}).json()["agents"][0]
        assert first["label"] != second["label"], (first["label"], second["label"])
        import re as _re

        for agent in (first, second):
            assert _re.fullmatch(r"Новый чат \d+", agent["label"]), agent["label"]
        numbers = [int(a["label"].split()[-1]) for a in (first, second)]
        assert numbers[1] == numbers[0] + 1, numbers

        # Номер удалённого чата второй раз не выдаётся: двух «Новых чатов N»
        # одновременно быть не должно.
        client.delete(f"/api/agents/{second['id']}")
        third = client.post("/api/agents", json={}).json()["agents"][0]
        assert third["label"] != second["label"], third["label"]
        assert int(third["label"].split()[-1]) > numbers[1], third["label"]

        # Первое сообщение имя не меняет — автоимени больше нет.
        client.post(f"/api/agents/{first['id']}/messages", json={"text": "расскажи про кэш"})
        again = client.get(f"/api/agents/{first['id']}").json()
        assert again["label"] == first["label"], again["label"]

        live = [a["label"] for a in client.get("/api/agents").json()["agents"]]
        assert len(live) == len(set(live)), "имена по умолчанию не должны повторяться"

    js = read("app/static/app.js")
    for gone in ("maybeAutoName", "chatTitle", "autoname"):
        assert gone not in js, f"в клиенте осталось автоимя: {gone}"
    assert "autoname" not in read("app/static/index.html"), "переключатель автоимени жив"
    return f"{first['label']}, {second['label']}, после удаления — {third['label']}"


@check("переименование и удаление живут в списке слева")
def check_list_actions():
    js = read("app/static/app.js")
    assert "function startRename" in js and "function askDelete" in js
    assert "miniButton(\"pencil\"" in js, "карандаша в строке списка нет"
    assert "miniButton(\"trash\"" in js, "корзины в строке списка нет"
    assert "Escape" in js and "Enter" in js, "переименование должно слушать Enter и Escape"
    assert 'id="f-label"' not in read("app/static/index.html"), "имя всё ещё правится в панели"

    with TestClient(main.app) as client:
        agent_id = new_agent(client, label="Было")
        renamed = client.patch(f"/api/agents/{agent_id}", json={"label": "Стало"})
        assert renamed.status_code == 200 and renamed.json()["label"] == "Стало"
        assert client.patch(f"/api/agents/{agent_id}", json={"label": "  "}).status_code == 400
        assert client.delete(f"/api/agents/{agent_id}").status_code == 200
        assert client.get(f"/api/agents/{agent_id}").status_code == 404
    return "карандаш и корзина в строке, поля имени в панели нет"


@check("пояснений прошлой постановки нет ни в данных, ни в разметке")
def check_no_leftover_texts():
    with TestClient(main.app) as client:
        client.post("/api/agents", json={})
        body = client.get("/api/agents").text
    for leftover in ("Единственное отличие", "База пары", "ступень", "Панель экспертов"):
        assert leftover not in body, f"наружу уехало пояснение: {leftover}"

    for path in ("app/static/app.js", "app/static/index.html"):
        assert "note" not in read(path).replace("field-note", ""), f"{path}: остались пояснения"

    # Подписи, объясняющие интерфейс сам себе, — тоже приписки: карандаш
    # в списке виден и без пояснения под панелью.
    html = read("app/static/index.html")
    assert "карандаш" not in html, "приписка про карандаш осталась"
    return "ни note, ни group, ни подписей к очевидному"


@check("имени клиента-референса нет нигде в репозитории")
def check_no_reference_name():
    """Референс был инструментом разработки, а не частью продукта.

    Имя собирается из кусков намеренно: иначе сама проверка стала бы
    единственным местом, где оно осталось.
    """
    needle = ("o" + "mlx").encode()
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    hits = []
    for name in tracked:
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            continue
        if needle.decode() in name.lower():
            hits.append(name)
            continue
        with open(path, "rb") as handle:
            if needle in handle.read().lower():
                hits.append(name)
    assert not hits, f"упоминания остались в: {', '.join(hits)}"
    return f"проверено {len(tracked)} файлов под контролем версий"


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
    return "в клиенте про ключ ни слова, ручки его не отдают"


@check("каталог отдаёт данные, по которым панель предупреждает о параметрах")
def check_catalog_capabilities():
    """Отбор моделей в чате не нужен, а вот данные о них — нужны.

    С `provider.require_parameters=true` параметр, которого модель не
    заявляет, выкашивает провайдеров, и вместо ответа приходит невнятная
    ошибка. Предупредить об этом заранее можно только по каталогу.
    """
    import app.catalog as catalog

    plain = catalog._normalize(
        {
            "id": "openai/gpt-4o-mini",
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            "supported_parameters": ["temperature", "max_tokens", "stop"],
            "context_length": 128000,
        }
    )
    assert plain["supported_parameters"] == ["temperature", "max_tokens", "stop"], plain
    assert plain["is_free"] is False and plain["temperature_capped"] is False, plain
    assert plain["temperature_cap"] is None, plain

    # Ловушка, ради которой мало одного supported_parameters: температуру
    # семейство заявляет, а на 1.2 всё равно отвечает 400.
    capped = catalog._normalize(
        {
            "id": "anthropic/claude-sonnet-4",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["temperature", "max_tokens"],
        }
    )
    assert "temperature" in capped["supported_parameters"], capped
    assert capped["temperature_capped"] is True, capped
    assert capped["temperature_cap"] == catalog.TEMPERATURE_CAP, capped

    free = catalog._normalize({"id": "x/y:free", "pricing": {"prompt": "0", "completion": "0"}})
    assert free["is_free"] is True, free

    # Отбор при этом не вернулся: чат показывает каталог целиком.
    assert not hasattr(catalog, "filter_models"), "фильтры каталога вернулись"
    assert "exclude_free" not in read("app/main.py"), "ручка снова отбирает модели"

    async def catalog_stub():
        return [plain, capped, free]

    saved = catalog.fetch_models
    catalog.fetch_models = catalog_stub
    try:
        with TestClient(main.app) as client:
            models = client.get("/api/models").json()["models"]
    finally:
        catalog.fetch_models = saved
    assert len(models) == 3, "каталог отдаётся целиком, включая :free"
    assert all("supported_parameters" in m for m in models), models[0]
    assert any(m["temperature_capped"] for m in models), models

    js = read("app/static/app.js")
    assert "function paramWarnings" in js, "панель не считает предупреждения"
    # Привязка к поставщику — настройка, которая ломает смену модели.
    # Панель говорит о ней в момент смены; сам текст проверяется вызовами
    # в checks/browser_check.js, здесь — что данные для него есть.
    assert "extra_body" in js, "панель не смотрит на extra_body"
    assert "baseModel" in js, "панель не помнит, с какой модели начинали"
    for field in ("supported_parameters", "temperature_capped", "temperature_cap"):
        assert field in js, f"клиент не смотрит на {field}"
    assert 'id="model-warn"' in read("app/static/index.html"), "блока предупреждения нет"
    return "три поля на месте, отбор не вернулся, панель их читает"


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


@check("лента прокручивается, а не сжимает карточки; композер закреплён")
def check_feed_scrolls():
    """Находка заказчика: карточки сплющивались в полоску вместо прокрутки.

    Лента — колоночный флексбокс, и её элементы по умолчанию сжимаются.
    У карточки к тому же `overflow: hidden`, из-за чего её автоматический
    минимальный размер равен нулю — сжаться она может до полосы. Прошлая
    правка (`min-height: 0` у колонки чата) закрепила композер, но сжатие
    шло по другой причине и осталось.
    """
    css = read("app/static/style.css")
    block = css[css.index(".chat {") : css.index(".chat-body")]
    for rule in ("min-height: 0", "overflow: hidden", "flex-direction: column"):
        assert rule in block, f"у .chat нет правила {rule}"
    assert "min-height: 0" in css[css.index(".chat-body") : css.index(".feed {")]
    assert ".composer { flex: 0 0 auto" in css, "композер должен быть нерастяжимым"
    assert "overflow-y: auto" in css[css.index(".feed {") :], "лента должна прокручиваться"

    # Главное: элементам ленты запрещено сжиматься.
    assert ".feed > * { flex: 0 0 auto; }" in css, "элементы ленты всё ещё сжимаются"
    feed = css[css.index(".feed {") : css.index(".feed > *")]
    assert "scroll-behavior: smooth" not in feed, (
        "плавная прокрутка ленты дёргает её на каждом куске ответа"
    )
    return "элементы ленты не сжимаются, лента прокручивается, композер закреплён"


@check("клиент: разбор markdown и раскладка проверены настоящими вызовами")
def check_browser():
    """Находка ревью: прежняя проверка была grep'ом по исходнику.

    Она прошла бы и если экранирование переедет **после** разбора — то есть
    самая опасная поверхность демо была прикрыта пустышкой. Теперь клиентский
    код исполняется под node: payload на входе, утверждения про выход.
    """
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


# --- 9. реестр и инфраструктура -----------------------------------------------


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


@check("вытеснение по потолку берёт самых старых простаивающих и щадит занятых")
def check_eviction():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=5)
    old = registry.create_many([AgentSpec(label=f"старый {i}", model="stub/m") for i in range(3)])
    fresh = registry.create_many([AgentSpec(label=f"свежий {i}", model="stub/m") for i in range(2)])
    for agent in fresh:
        agent.last_used_at += 100
    registry.create_many([AgentSpec(label=f"новый {i}", model="stub/m") for i in range(3)])

    assert len(registry) == 5, len(registry)
    assert all(registry.get(a.id) is None for a in old), "старые должны быть вытеснены"
    assert all(registry.get(a.id) is not None for a in fresh), "свежие вытесняться не должны"
    assert registry.evicted == 3, registry.evicted

    busy = AgentRegistry(max_agents=2)
    held = busy.create(AgentSpec(label="занят", model="stub/m"))
    held.reserve()
    busy.create_many([AgentSpec(label=f"н {i}", model="stub/m") for i in range(3)])
    assert busy.get(held.id) is not None, "занятого вытеснять нельзя"
    held.release()
    return "вытеснены три самых старых, свежие и занятый на месте"


@check("потолок пачки при спавне")
def check_batch_limit():
    with TestClient(main.app) as client:
        too_many = client.post(
            "/api/agents",
            json={"agents": [{"model": "stub/m"} for _ in range(main.MAX_SPAWN_BATCH + 1)]},
        )
        assert too_many.status_code == 400, too_many.status_code
        assert str(main.MAX_SPAWN_BATCH) in too_many.json()["detail"], too_many.text
        batch = client.post("/api/agents", json={"agents": [{"model": "stub/m"}] * 3})
        assert batch.status_code == 200, batch.text
    return f"пачка больше {main.MAX_SPAWN_BATCH} → 400"


@check("общий httpx-клиент и семафор на процесс")
def check_shared_client():
    import app.llm as llm

    async def scenario():
        first = llm.shared_client()
        assert first is llm.shared_client(), "клиент должен быть один на процесс"
        assert llm.call_slots() is llm.call_slots()
        await llm.aclose()
        assert llm.shared_client() is not first, "после закрытия создаётся новый"
        await llm.aclose()

    asyncio.run(scenario())

    os.environ["LLM_MAX_CONCURRENCY"] = "3"
    try:
        assert llm.max_concurrency() == 3, llm.max_concurrency()
    finally:
        os.environ.pop("LLM_MAX_CONCURRENCY")
    assert llm.max_concurrency() == llm.DEFAULT_MAX_CONCURRENCY
    assert read("app/llm.py").count("httpx.AsyncClient(") == 1, "клиент создаётся в одном месте"
    return "клиент один, семафор один, LLM_MAX_CONCURRENCY читается"


@check("клиент шлёт только текст: лента в теле запроса запрещена")
def check_no_feed():
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        bad = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"text": "привет", "messages": [{"role": "user", "content": "привет"}]},
        )
        assert bad.status_code == 400, bad.text
        assert "только text" in bad.json()["detail"], bad.text
        assert client.post(f"/api/agents/{agent_id}/messages", json={"text": " "}).status_code == 400

    js = read("app/static/app.js")
    assert "{ text }" in js, "клиент должен слать в теле только text"
    assert "/messages" in js and "/regenerate" in js
    return "лишние поля в теле → 400, в app.js ленты нет"


@check("CLI говорит с агентом без веб-слоя")
def check_cli():
    _stub.install(reply="привет из консоли")
    import io

    from app import cli

    args = cli._parse_args(["--model", "stub/m", "--label", "консоль", "--history-limit", "5"])
    agent = cli.build_agent(args)
    out = io.StringIO()
    answer = asyncio.run(cli.ask(agent, "как дела?", out))
    assert answer == "привет из консоли", answer
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.id in {a.id for a in REGISTRY.list()}, "CLI-агент виден в реестре процесса"
    return "ответ напечатан, история записана, агент в реестре"



# --- 12. День 7: память между запусками ---------------------------------------


def _temp_db(name: str) -> str:
    """Свежий файл базы под одну проверку. Каталога заранее нет — его создаёт Store."""
    import tempfile

    return os.path.join(tempfile.mkdtemp(prefix=f"check-{name}-"), "nested", "agents.db")


def _meta(store, key: str):
    """Значение счётчика прямо из таблицы `meta`.

    Читаем схему, а не зовём метод: в приложении `meta` нужна одному счётчику,
    и заводить в `Store` публичный геттер ради проверки — тот самый код,
    который существует только для тестов.
    """
    row = store.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else None


class _Turn:
    """Минимальная реплика: хранилищу от неё нужны эти поля, и только они."""

    def __init__(self, role, content, error=None, metrics=None, at=1.0):
        self.role, self.content, self.error = role, content, error
        self.metrics, self.at = metrics, at


@check("настоящий перезапуск: файл базы закрыт и открыт заново")
def check_store_reopen():
    path = _temp_db("reopen")
    assert not os.path.isdir(os.path.dirname(path)), "каталога быть не должно — его создаёт init()"

    first = Store(path).init()
    first.save_session(
        "ag_00042",
        label="Нина",
        config={"model": "stub/m", "history_limit": 7, "top_p": 0.9},
        created_at=100.0,
        context_length=128_000,
    )
    first.save_history(
        "ag_00042", [_Turn("user", "меня зовут Нина"), _Turn("assistant", "привет, Нина")]
    )
    first.close()

    # Новый объект на том же файле — это и есть перезапуск, а не «тот же
    # объект в памяти»: соединение закрыто, кеш sqlite ушёл вместе с ним.
    second = Store(path).init()
    saved = second.load_session("ag_00042")
    assert saved is not None, "чат не пережил закрытие файла"
    assert saved["config"]["history_limit"] == 7 and saved["config"]["top_p"] == 0.9
    assert saved["created_at"] == 100.0 and saved["context_length"] == 128_000
    rows = second.message_rows("ag_00042")
    assert rows == [(0, "user", "меня зовут Нина"), (1, "assistant", "привет, Нина")], rows
    assert second.max_agent_seq() == 42, second.max_agent_seq()
    second.close()
    return f"каталог создан init(), после переоткрытия {len(rows)} реплики и конфиг на месте"


@check("диалог продолжается в новом процессе программы")
def check_restart_process():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "checks", "restart.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ОК: диалог продолжился" in result.stdout, result.stdout
    return result.stdout.strip().splitlines()[2]


@check("два процесса на одной базе: id не пересекаются, чужой диалог цел")
def check_two_processes():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "checks", "two_processes.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ОК: два процесса" in result.stdout, result.stdout
    return result.stdout.strip().splitlines()[-3]


@check("восстановление истории — в конструкторе агента, а не отдельным вызовом")
def check_restore_in_constructor():
    from app.agent import Agent

    path = _temp_db("ctor")
    store = Store(path).init()
    spec = AgentSpec(label="ctor", model="stub/m", system="СИС")

    born = Agent(spec, store=store)
    born.remember("user", "меня зовут Нина")
    born.remember("assistant", "привет, Нина")
    store.close()

    # Тот же id, новый объект, новое открытие файла — история обязана приехать
    # сама, без единого дополнительного вызова.
    again = Store(path).init()
    revived = Agent(spec, agent_id=born.id, store=again)
    assert [t.content for t in revived.history] == ["меня зовут Нина", "привет, Нина"]

    _stub.install(reply="ок")
    asyncio.run(drain(revived.ask("как меня зовут?")))
    sent = [m["content"] for m in _stub.CALLS[0]["messages"]]
    assert sent == ["СИС", "меня зовут Нина", "привет, Нина", "как меня зовут?"], sent
    again.close()
    return "агент поднят конструктором: реплики из базы уехали в следующий запрос"


@check("изоляция сессий: у сообщений есть session_id, ленты не сливаются")
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
    assert [r[2] for r in store.message_rows(first)] == ["меня зовут Нина", "ответ0"]
    assert [r[2] for r in store.message_rows(second)] == ["как меня зовут?", "ответ0"]

    # Схема не даёт записать реплику без сессии: ключ составной, и это
    # единственная защита от «все чаты в одной ленте» после перезапуска.
    keys = [row[1] for row in store.conn.execute("PRAGMA table_info(messages)") if row[5]]
    assert keys == ["session_id", "seq"], keys
    indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(messages)")}
    assert "messages_by_session" in indexes, indexes
    return "две сессии — две ленты; PK (session_id, seq), индекс по session_id есть"


@check("порядковые номера без дыр: откат и кап окна пересчитывают seq от нуля")
def check_seq_renumbered():
    from app.agent import MAX_STORED_MESSAGES, Agent

    path = _temp_db("seq")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="seq", model="stub/m"), store=store)

    # 1) Несостоявшийся обмен: в базе не должно появиться вопроса без ответа.
    _stub.install(fail=True)
    asyncio.run(drain(agent.ask("вопрос, на который не ответили")))
    assert store.message_rows(agent.id) == [], store.message_rows(agent.id)

    # 2) Обычные обмены и кап хранимого.
    _stub.reset()
    _stub.install(reply="ок")
    for i in range(3):
        asyncio.run(drain(agent.ask(f"вопрос {i}")))
    assert [row[0] for row in store.message_rows(agent.id)] == list(range(6))

    agent.history = agent.history[-2:]  # так выглядит история после кап-а окна
    agent.persist()
    seqs = [row[0] for row in store.message_rows(agent.id)]
    assert seqs == [0, 1], f"после укорачивания номера должны идти от нуля: {seqs}"

    # 3) Жёсткий потолок хранимого держится и в базе.
    agent.history = [_Turn("user", f"т{i}") for i in range(MAX_STORED_MESSAGES + 10)]
    agent._trim()
    agent.persist()
    rows = store.message_rows(agent.id)
    assert len(rows) == MAX_STORED_MESSAGES and [r[0] for r in rows] == list(
        range(MAX_STORED_MESSAGES)
    )
    store.close()
    return f"нет ответа — нет записи; после укорачивания seq = 0..N, потолок {MAX_STORED_MESSAGES}"


@check("частичный ответ пишется в базу с пометкой ошибки, парой с вопросом")
def check_partial_persisted():
    from app.agent import Agent

    path = _temp_db("partial")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="partial", model="stub/m"), store=store)

    async def broken(session, *, prompt_override=None, context_length=None):
        yield {"type": "delta", "text": "начал отвеч", "metrics": None}
        raise RuntimeError("провод оборвался")

    agent_module.stream_completion = broken
    asyncio.run(drain(agent.ask("вопрос")))
    store.close()

    reopened = Store(path).init()
    rows = reopened.message_rows(agent.id)
    assert [(r[0], r[1]) for r in rows] == [(0, "user"), (1, "assistant")], rows
    errors = [
        row["error"]
        for row in reopened.conn.execute(
            "SELECT error FROM messages WHERE session_id = ? ORDER BY seq", (agent.id,)
        )
    ]
    assert errors[0] is None and errors[1], errors
    reopened.close()
    return "после обрыва в базе пара реплик, у ответа проставлена ошибка"


@check("рассуждение в базу не пишется, метрики пишутся")
def check_reasoning_not_stored():
    _stub.install(reply="ответ", reasoning="я думаю про панду")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос"})

    store = REGISTRY.store
    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(messages)")}
    assert "reasoning" not in columns, "рассуждению в базе не место: в контекст оно не входит"
    blob = " ".join(str(v) for row in store.conn.execute("SELECT * FROM messages") for v in row)
    assert "панду" not in blob, "рассуждение осело в базе"

    stored = store.load_messages(agent_id)
    assert stored[-1]["metrics"], "метрики ответа должны пережить перезапуск: по ним плитки"
    assert stored[-1]["metrics"]["provider"] == "stub", stored[-1]["metrics"]

    with TestClient(main.app) as client:
        full = client.get(f"/api/agents/{agent_id}").json()
    assert full["transcript"][-1]["reasoning"] == "я думаю про панду", full["transcript"][-1]
    return "колонки reasoning в базе нет, метрики сохранены"


@check("неудачная перегенерация: в базе ровно исходная пара, без дублей и дыр")
def check_regenerate_failure_db():
    _stub.install(reply="первый ответ")
    with TestClient(main.app) as client:
        agent_id = new_agent(client)
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "мой вопрос"})
        before = REGISTRY.store.message_rows(agent_id)
        assert before == [(0, "user", "мой вопрос"), (1, "assistant", "первый ответ")], before

        _stub.install(fail=True)
        response = client.post(f"/api/agents/{agent_id}/regenerate")
        assert response.status_code == 200, response.text
        done = next(e for e in sse(response.text) if e["event"] == "done")
        assert done.get("restored") is True, done

    after = REGISTRY.store.message_rows(agent_id)
    assert after == before, f"после неудачной перегенерации база разошлась: {after}"

    _stub.install(reply="второй ответ")
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{agent_id}/regenerate")
    replaced = REGISTRY.store.message_rows(agent_id)
    assert replaced == [(0, "user", "мой вопрос"), (1, "assistant", "второй ответ")], replaced
    return "неудача возвращает исходную пару, удача заменяет ответ — в обоих случаях seq 0,1"


def _restart(store):
    """Новый реестр на том же файле — так выглядит перезапуск процесса."""
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=1000, store=store)
    main.REGISTRY = registry
    return registry


@check("нумерация имён переживает перезапуск: занятый номер второй раз не выдаётся")
def check_numbering_survives_restart():
    """Счётчик имён обязан жить в базе, а не в процессе.

    Проверяется это не только поведением — в одном процессе счётчик в памяти
    рестарт бы «пережил» сам собой, и проверка ничего бы не поймала, — а тем,
    где лежит число: после каждой выдачи оно должно стоять в `meta`. Реальный
    второй процесс на той же базе разбирает `checks/two_processes.py`.
    """
    path = _temp_db("numbers")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        _restart(store)
        with TestClient(main.app) as client:
            first = client.post("/api/agents", json={}).json()["agents"][0]["label"]
            second = client.post("/api/agents", json={}).json()["agents"][0]["label"]
        assert _meta(store, main.CHAT_NUMBER_KEY) == "2", (
            "номер выдан не базой: после перезапуска счётчик начнётся заново "
            f"и выдаст занятое имя (в meta {_meta(store, main.CHAT_NUMBER_KEY)!r})"
        )

        # Перезапуск процесса: счётчик в памяти начался бы с единицы заново.
        _restart(store)
        with TestClient(main.app) as client:
            third = client.post("/api/agents", json={}).json()["agents"][0]["label"]
            labels = [a["label"] for a in client.get("/api/agents").json()["agents"]]
        assert _meta(store, main.CHAT_NUMBER_KEY) == "3", _meta(store, main.CHAT_NUMBER_KEY)
    finally:
        main.REGISTRY = saved_registry
        store.close()

    numbers = [int(name.split()[-1]) for name in (first, second, third)]
    assert numbers == [1, 2, 3], numbers
    assert len(labels) == len(set(labels)), f"имена повторились: {sorted(labels)}"
    return f"до рестарта {first}, {second}; после — {third}, номер стоит в базе"


@check("чаты сверх потолка реестра: все видны в списке и любой открывается")
def check_list_beyond_cap():
    from app.registry import AgentRegistry

    _stub.install(reply="ответ")
    path = _temp_db("beyond-cap")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        registry = AgentRegistry(max_agents=3, store=store)
        main.REGISTRY = registry
        made = []
        for i in range(10):
            agent = registry.create(AgentSpec(label=f"чат {i}", model="stub/m"))
            asyncio.run(drain(agent.ask(f"вопрос {i}")))
            made.append(agent.id)

        assert len(registry) <= 3, f"в памяти {len(registry)} при потолке 3"
        assert registry.evicted >= 7, registry.evicted

        listing = main._listing()
        assert [a["id"] for a in listing["agents"]] == made, listing["agents"]
        assert listing["stored"] == 10 and listing["live"] <= 3
        assert all(a["history_len"] == 2 for a in listing["agents"])

        with TestClient(main.app) as client:
            body = client.get(f"/api/agents/{made[0]}").json()
        texts = [t["content"] for t in body["transcript"]]
        assert texts == ["вопрос 0", "ответ"], texts
    finally:
        main.REGISTRY = saved_registry
        store.close()
    return "10 чатов при потолке 3: все в списке, длина истории на месте, любой открывается"


@check("вытеснение — выгрузка, а не удаление: чат остаётся в базе")
def check_eviction_keeps_session():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=2, store=REGISTRY.store)
    old = registry.create(AgentSpec(label="старый", model="stub/m"))
    old.remember("user", "меня зовут Нина")
    old.remember("assistant", "привет")
    old.spec.temperature = 0.9
    old.save_config()
    old_id = old.id

    registry.create_many([AgentSpec(label=f"новый {i}", model="stub/m") for i in range(3)])
    assert registry.get(old_id) is None, "старый должен быть выгружен из памяти"
    assert registry.evicted >= 1, registry.evicted
    assert registry.store.load_session(old_id) is not None, "выгрузка не должна удалять чат"

    revived = registry.require(old_id)
    assert revived is not old, "поднят новый объект, а не тот же самый"
    assert [t.content for t in revived.history] == ["меня зовут Нина", "привет"]
    assert revived.spec.temperature == 0.9, revived.spec.temperature

    registry.kill(revived.id)
    assert registry.store.load_session(old_id) is None, "kill обязан стереть и строку в базе"
    return "выгруженный чат поднялся с историей и конфигом; kill стёр его из базы"


@check("выгруженный объект больше не пишет в сессию")
def check_detached_does_not_clobber():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=1, store=REGISTRY.store)
    first = registry.create(AgentSpec(label="долгая", model="stub/m"))
    first.remember("user", "вопрос 1")
    first.remember("assistant", "ответ 1")

    registry.create(AgentSpec(label="вытесняющая", model="stub/m"))
    assert registry.get(first.id) is None, "первый должен быть выгружен"
    assert first.detached is True and first.store is None, "выгруженный обязан отцепиться"

    second = registry.require(first.id)
    assert second is not first, "поднят тот же объект — проверка ничего не проверяет"
    second.remember("user", "вопрос 2")
    second.remember("assistant", "ответ 2")

    first.remember("user", "мусор")
    stored = [row[2] for row in registry.store.message_rows(first.id)]
    assert stored == ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2"], stored
    return "выгруженный объект пишет только в память, база остаётся за поднятым"


@check("параллельная запись: восемь чатов пишут одновременно, ничего не теряется")
def check_parallel_writes():
    from app.agent import Agent

    path = _temp_db("parallel")
    store = Store(path).init()
    _stub.install(reply=lambda m, i: f"ответ-{i}", chunks=6, delay=0.002)

    agents = [Agent(AgentSpec(label=f"чат {i}", model="stub/m"), store=store) for i in range(8)]

    async def all_at_once():
        await asyncio.gather(*(drain(a.ask(f"вопрос {i}")) for i, a in enumerate(agents)))

    asyncio.run(all_at_once())
    store.close()

    reopened = Store(path).init()
    for i, agent in enumerate(agents):
        rows = reopened.message_rows(agent.id)
        assert [r[0] for r in rows] == [0, 1], (agent.id, rows)
        assert rows[0][2] == f"вопрос {i}", rows
    total = reopened.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert total == 16, total
    reopened.close()
    return "8 чатов писали одновременно, в базе 16 реплик, порядок в каждом свой"


@check("id выдаёт база: занятый номер не выдаётся второй раз")
def check_id_claimed_in_store():
    import app.agent as agent_mod
    from app.agent import Agent

    path = _temp_db("claim")
    store = Store(path).init()

    stranger = Agent(AgentSpec(label="чужой", model="stub/m"), store=store)
    stranger.remember("user", "СЕКРЕТ соседа")
    stranger.remember("assistant", "ответ соседа")
    taken = stranger.id

    # Откатываем счётчик ровно на этот номер: так и выглядит давно поднятый
    # сервер, мимо которого консоль успела занять следующий id.
    agent_mod._last_id = int(taken.removeprefix("ag_")) - 1
    mine = Agent(AgentSpec(label="мой", model="stub/m"), store=store)
    assert mine.id != taken, f"выдан занятый id {taken}"

    assert [row[2] for row in store.message_rows(taken)] == ["СЕКРЕТ соседа", "ответ соседа"]
    assert store.load_session(taken)["label"] == "чужой", "чужой конфиг перезаписан"
    store.close()
    return f"{taken} остался за соседом, свежий агент получил {mine.id}"


@check("недостроенный агент не оставляет пустую строку чата")
def check_claim_rolled_back():
    from app.agent import Agent

    path = _temp_db("claim-rollback")
    store = Store(path).init()
    good = Agent(AgentSpec(label="живой", model="stub/m"), store=store)
    before = store.count_sessions()

    saved = store.save_session
    store.save_session = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("бум на записи"))
    try:
        Agent(AgentSpec(label="недостроенный", model="stub/m"), store=store)
        raise AssertionError("конструктор должен был упасть")
    except RuntimeError as exc:
        assert "бум" in str(exc), exc
    finally:
        store.save_session = saved

    assert store.count_sessions() == before, "занятая строка не убрана"
    assert [row["label"] for row in store.list_sessions()] == ["живой"]
    assert store.load_session(good.id) is not None, "уборка задела чужой чат"
    store.close()
    return "строка, занятая под упавший конструктор, убрана; соседняя цела"


@check("context_length переживает выгрузку и перезапуск")
def check_context_length_restored():
    from app.registry import AgentRegistry

    path = _temp_db("ctxlen")
    store = Store(path).init()
    registry = AgentRegistry(max_agents=10, store=store)
    agent = registry.create(AgentSpec(label="контекст", model="stub/m"), context_length=128_000)
    registry._unload(agent.id)
    assert registry.require(agent.id).context_length == 128_000

    store.close()
    reopened = Store(path).init()
    assert reopened.load_session(agent.id)["context_length"] == 128_000
    reopened.close()
    return "после выгрузки и после переоткрытия файла context_fill_pct снова считается"


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
    finally:
        if saved_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved_key
    return f"ключ не найден ни в одной колонке и ни в одном файле базы ({', '.join(files)})"


@check("вырожденный ключ не режет идентификаторы: у редакции есть нижний порог")
def check_redact_floor():
    from app.agent import Agent
    from app.store import MIN_SECRET_LENGTH, redact

    saved_key = os.environ.get("OPENROUTER_API_KEY")
    path = _temp_db("floor")
    store = Store(path).init()
    try:
        os.environ["OPENROUTER_API_KEY"] = "1"
        assert redact("ag_00001") == "ag_00001" and redact("assistant") == "assistant"

        agent = Agent(AgentSpec(label="порог", model="stub/m"), store=store)
        agent.remember("user", "встретимся 1 числа")
        assert [r[2] for r in store.message_rows(agent.id)] == ["встретимся 1 числа"]
        assert store.load_session(agent.id)["label"] == "порог", "id или имя изрезаны редакцией"

        real = "sk-or-v1-" + "a" * 64
        assert len(real) >= MIN_SECRET_LENGTH
        os.environ["OPENROUTER_API_KEY"] = real
        agent.remember("user", f"мой ключ {real}")
        stored = " ".join(r[2] for r in store.message_rows(agent.id))
        assert real not in stored and "***" in stored, stored
    finally:
        if saved_key is None:
            os.environ.pop("OPENROUTER_API_KEY", None)
        else:
            os.environ["OPENROUTER_API_KEY"] = saved_key
        store.close()
    return f"ключ короче {MIN_SECRET_LENGTH} символов данные не трогает, настоящий — режется"


@check("занятая база — внятный 503, а не голый 500")
def check_busy_message():
    import sqlite3

    from app.store import StoreBusyError, _busy

    translated = _busy(sqlite3.OperationalError("database is locked"), "/tmp/agents.db")
    assert isinstance(translated, StoreBusyError), type(translated)
    text = str(translated)
    assert "занята другим процессом" in text and "повторите" in text, text
    assert _busy(sqlite3.OperationalError("no such table: sessions"), "/tmp/a.db") is None

    path = _temp_db("busy")
    store = Store(path).init()
    blocker = sqlite3.connect(str(path), isolation_level=None)
    blocker.execute("PRAGMA busy_timeout=0")
    blocker.execute("BEGIN IMMEDIATE")
    blocker.execute("INSERT INTO sessions (id, created_at, updated_at) VALUES ('ag_99999', 1, 1)")
    store.conn.execute("PRAGMA busy_timeout=50")
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            saved_store = main.REGISTRY.store
            main.REGISTRY.store = store
            try:
                response = client.post("/api/agents", json={"agent": {"model": "stub/m"}})
            finally:
                main.REGISTRY.store = saved_store
        assert response.status_code == 503, (response.status_code, response.text)
        assert "занята другим процессом" in response.json()["detail"], response.text
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
        store.close()

    reopened = Store(path).init()
    assert [r["label"] for r in reopened.list_sessions()] == [], reopened.list_sessions()
    reopened.close()
    return "HTTP 503 с объяснением; чат, который не записали, база не завела"


@check(".gitignore ловит базу и её WAL-файлы")
def check_gitignore_db():
    patterns = [
        line.strip()
        for line in open(os.path.join(ROOT, ".gitignore"), encoding="utf-8")
        if line.strip() and not line.startswith("#")
    ]
    for needed in ("*.db", "*.db-wal", "*.db-shm"):
        assert needed in patterns, f"{needed} не в .gitignore: база уедет в публичный репозиторий"

    probe = ["data/agents.db", "data/agents.db-wal", "data/agents.db-shm", "agents.db"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *probe], capture_output=True, text=True, cwd=ROOT
    )
    ignored = set(result.stdout.split())
    assert set(probe) <= ignored, f"git не игнорирует: {sorted(set(probe) - ignored)}"
    return "git игнорирует *.db, *.db-wal, *.db-shm и каталог data/"


@check("CLI продолжает сохранённый чат: --session и /сессии")
def check_cli_session():
    import io

    from app import cli

    _stub.install(reply="запомнил")
    first = cli.build_agent(cli._parse_args(["--model", "stub/m", "--label", "консоль"]))
    asyncio.run(cli.ask(first, "меня зовут Нина", io.StringIO()))

    REGISTRY._unload(first.id)
    assert REGISTRY.get(first.id) is None

    second = cli.build_agent(cli._parse_args(["--session", first.id]))
    assert second.id == first.id and [t.content for t in second.history] == [
        "меня зовут Нина",
        "запомнил",
    ], second.history

    out = io.StringIO()
    saved_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        asyncio.run(cli.repl(second, once=True, out=out))
    finally:
        sys.stdin = saved_stdin
    assert "[продолжаем]" in out.getvalue(), out.getvalue()

    listing = io.StringIO()
    cli._print_sessions(listing)
    assert first.id in listing.getvalue(), listing.getvalue()

    try:
        cli.build_agent(cli._parse_args(["--session", "ag_99999"]))
        raise AssertionError("несуществующий чат должен честно падать")
    except SystemExit:
        pass
    return "--session поднимает чат из базы, /сессии его показывает"





@check("чистый старт: чатов нет, заведённый переживает перезапуск вместе с перепиской")
def check_clean_start_survives_restart():
    """Список начинается пустым — заготовок больше нет.

    Дальше всё как у любого чата: завели, поговорили, перезапустились —
    он на месте, с перепиской и со всеми правками панели.
    """
    _stub.install(reply="и тебе привет")
    path = _temp_db("clean-start")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        first = _restart(store)
        assert main._listing()["agents"] == [], "на чистой базе чатов быть не должно"

        chat = first.create(AgentSpec(label="Мой чат", model="stub/m"))
        asyncio.run(drain(chat.ask("привет")))
        chat.spec.system = "ты гид"
        chat.spec.top_p = 0.42
        chat.spec.stop = ["СТОП"]
        chat.spec.response_format = {"type": "json_object"}
        chat.spec.history_limit = 4
        chat.save_config()
        chat_id = chat.id

        store.close()
        # Настоящее переоткрытие файла, а не тот же объект в памяти.
        again = Store(path).init()
        second = _restart(again)
        listing = main._listing()["agents"]
        revived = second.require(chat_id)
    finally:
        main.REGISTRY = saved_registry

    assert [a["label"] for a in listing] == ["Мой чат"], listing
    assert [t.content for t in revived.history] == ["привет", "и тебе привет"]
    assert revived.spec.system == "ты гид" and revived.spec.top_p == 0.42
    assert revived.spec.stop == ["СТОП"] and revived.spec.history_limit == 4
    assert revived.spec.response_format == {"type": "json_object"}
    again.close()
    return "пустой старт, один чат, после переоткрытия файла переписка и панель на месте"


@check("поднятый из базы чат отдаёт те же поля панели: повторный PATCH ничего не меняет")
def check_panel_stable_after_restart():
    """«Применено» не должно появляться на чате, поднятом из базы.

    Клиент показывает эту строку, только если правка что-то изменила: сравнивает
    конфиг до и после `PATCH` по полям панели. Значит, чат после перезапуска
    обязан отдавать ровно те же значения — если хоть одно поле изменит тип или
    форму по дороге через JSON (`stop` — список, `response_format` — объект,
    незаданный параметр — `null`, а не ноль), первое же сообщение показало бы
    «Применено», хотя пользователь ничего не трогал.

    Стенд клиента этого поймать не может: у него свой сервер и своя память.
    """
    panel = (
        "system",
        "model",
        "stop",
        "response_format",
        "temperature",
        "max_tokens",
        "top_p",
        "top_k",
        "min_p",
        "repetition_penalty",
        "presence_penalty",
        "frequency_penalty",
        "history_limit",
    )
    settings = {
        "system": "ты гид",
        "model": "stub/model",
        "stop": ["СТОП", "КОНЕЦ"],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 200,
        "top_p": 0.0,
        "top_k": 7,
        "min_p": 0.05,
        "repetition_penalty": 1.1,
        "presence_penalty": 0.2,
        "frequency_penalty": 0.3,
        "history_limit": 6,
    }

    path = _temp_db("panel-stable")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        _restart(store)
        with TestClient(main.app) as client:
            chat_id = client.post("/api/agents", json={}).json()["agents"][0]["id"]
            saved = client.patch(f"/api/agents/{chat_id}", json=settings).json()
        before = {name: saved[name] for name in panel}
        assert before["top_p"] == 0.0 and before["temperature"] == 0.0, before
        # Незаданные параметры остаются null, а не превращаются в ноль.
        assert saved["history_limit"] == 6, saved["history_limit"]

        store.close()
        again = Store(path).init()
        _restart(again)
        with TestClient(main.app) as client:
            restored = client.get(f"/api/agents/{chat_id}").json()
            # Панель показывает то, что пришло с сервера, и шлёт это же обратно.
            repeated = client.patch(
                f"/api/agents/{chat_id}", json={name: restored[name] for name in panel}
            ).json()
    finally:
        main.REGISTRY = saved_registry
        again.close()

    after_load = {name: restored[name] for name in panel}
    assert after_load == before, (
        "поднятый из базы чат отдаёт другие значения — «Применено» вылезло бы "
        f"на первом же сообщении: {[k for k in panel if after_load[k] != before[k]]}"
    )
    after_patch = {name: repeated[name] for name in panel}
    assert after_patch == before, (
        f"повторный PATCH что-то изменил: {[k for k in panel if after_patch[k] != before[k]]}"
    )
    # Типы тоже те же: список остался списком, объект — объектом.
    assert isinstance(after_load["stop"], list) and after_load["stop"] == ["СТОП", "КОНЕЦ"]
    assert isinstance(after_load["response_format"], dict), after_load["response_format"]
    return f"{len(panel)} полей панели пережили переоткрытие файла без единого расхождения"


@check("переименованный чат остаётся переименованным после перезапуска")
def check_rename_survives_restart():
    """Переименование — то, чем пользуются постоянно, и оно обязано пережить рестарт.

    Проверяется настоящим переоткрытием файла, а не тем же объектом в памяти:
    имя лежит и отдельной колонкой, и внутри конфига, и разойтись они не должны.
    Номер, из которого имя собиралось, при этом не переиспользуется — иначе
    следующий чат получил бы имя, только что освобождённое переименованием.
    """
    _stub.install(reply="ответ")
    path = _temp_db("rename")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        _restart(store)
        with TestClient(main.app) as client:
            created = client.post("/api/agents", json={}).json()["agents"][0]
            assert created["label"] == "Новый чат 1", created["label"]
            client.post(f"/api/agents/{created['id']}/messages", json={"text": "привет"})
            renamed = client.patch(
                f"/api/agents/{created['id']}", json={"label": "Про Казань"}
            )
            assert renamed.status_code == 200 and renamed.json()["label"] == "Про Казань"

        # Закрываем базу и открываем файл заново — это и есть перезапуск.
        store.close()
        again = Store(path).init()
        second = _restart(again)
        listing = main._listing()["agents"]
        with TestClient(main.app) as client:
            after = client.post("/api/agents", json={}).json()["agents"][0]["label"]
    finally:
        main.REGISTRY = saved_registry

    assert [a["label"] for a in listing] == ["Про Казань"], listing
    revived = second.require(created["id"])
    assert revived.spec.label == "Про Казань", revived.spec.label
    # Имя лежит и колонкой, и в конфиге: разойтись они не должны.
    row = again.load_session(created["id"])
    assert row["label"] == "Про Казань", row["label"]
    assert row["config"]["label"] == "Про Казань", row["config"]["label"]
    # Переписка при переименовании не пострадала.
    assert [t.content for t in revived.history] == ["привет", "ответ"], revived.history
    # И освободившийся номер не переиспользован.
    assert after == "Новый чат 2", after
    again.close()
    return f"«{created['label']}» → «Про Казань» пережило переоткрытие файла, следующий — {after}"


@check("удалили последний чат — список пуст, а номер следующего не повторяется")
def check_last_chat_deleted():
    """Клиент на пустом списке заводит чат сам, и номер ему нужен свежий."""
    path = _temp_db("last-chat")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        registry = _restart(store)
        with TestClient(main.app) as client:
            first = client.post("/api/agents", json={}).json()["agents"][0]
            assert client.delete(f"/api/agents/{first['id']}").status_code == 200
            assert client.get("/api/agents").json()["agents"] == [], "чат остался в списке"
            assert store.load_session(first["id"]) is None, "строка чата осталась в базе"
            second = client.post("/api/agents", json={}).json()["agents"][0]

        # Перезапуск — и номер по-прежнему не отматывается назад.
        _restart(store)
        with TestClient(main.app) as client:
            third = client.post("/api/agents", json={}).json()["agents"][0]
            labels = [a["label"] for a in client.get("/api/agents").json()["agents"]]
    finally:
        main.REGISTRY = saved_registry
        store.close()

    numbers = [int(a["label"].split()[-1]) for a in (first, second, third)]
    assert numbers == [1, 2, 3], numbers
    assert len(labels) == len(set(labels)), labels
    return f"после удаления {first['label']} следующим стал {second['label']}, после рестарта {third['label']}"


@check("список не обрезается: тысяча с лишним чатов видна целиком")
def check_list_not_truncated():
    """Обрезка была бы молчаливой, а список — единственный путь к диалогу."""
    from app.registry import AgentRegistry

    path = _temp_db("no-cap")
    store = Store(path).init()
    saved_registry = main.REGISTRY
    try:
        main.REGISTRY = AgentRegistry(max_agents=5, store=store)
        made = 1200
        now = time.time()
        with store.bulk():
            for i in range(made):
                store.save_session(
                    f"chat_{i:05d}",
                    label=f"чат {i}",
                    config={"model": "stub/m", "label": f"чат {i}"},
                    created_at=now + i,
                )
        entries = main._listing()["agents"]
        assert len(entries) == made, f"в списке {len(entries)} из {made} — список обрезан"
        far = entries[-1]
        assert main.REGISTRY.require(far["id"]).spec.label == far["label"]
    finally:
        main.REGISTRY = saved_registry
        store.close()
    return f"{made} чатов — в списке все, любой открывается"


CHECKS = [
    check_spawn_100,
    check_flat_list,
    check_new_chat_is_blank,
    check_no_presets,
    check_scenarios_gone,
    check_memory,
    check_history_window,
    check_parallel,
    check_rollback,
    check_disconnect,
    check_new_params,
    check_panel_applies_next_message,
    check_patch_during_generation,
    check_config_snapshot,
    check_system_prompt_single_home,
    check_stop_and_format,
    check_create_and_patch_agree,
    check_config_survives_by_construction,
    check_every_write_path_redacts,
    check_patch_panel,
    check_transcript_shape,
    check_reasoning,
    check_first_token,
    check_regenerate,
    check_regenerate_failure,
    check_regenerate_disconnect,
    check_no_clear_all,
    check_numbered_names,
    check_list_actions,
    check_no_leftover_texts,
    check_no_reference_name,
    check_no_key_leak,
    check_catalog_capabilities,
    check_no_cdn,
    check_feed_scrolls,
    check_browser,
    check_spec_deep_copy,
    check_eviction,
    check_batch_limit,
    check_shared_client,
    check_no_feed,
    check_cli,
    # --- День 7: память между запусками ---
    check_store_reopen,
    check_restart_process,
    check_two_processes,
    check_restore_in_constructor,
    check_session_isolation,
    check_seq_renumbered,
    check_partial_persisted,
    check_reasoning_not_stored,
    check_regenerate_failure_db,
    check_numbering_survives_restart,
    check_list_beyond_cap,
    check_eviction_keeps_session,
    check_detached_does_not_clobber,
    check_parallel_writes,
    check_id_claimed_in_store,
    check_claim_rolled_back,
    check_context_length_restored,
    check_no_key_in_db,
    check_redact_floor,
    check_busy_message,
    check_gitignore_db,
    check_cli_session,
    check_clean_start_survives_restart,
    check_panel_stable_after_restart,
    check_rename_survives_restart,
    check_last_chat_deleted,
    check_list_not_truncated,
]


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
