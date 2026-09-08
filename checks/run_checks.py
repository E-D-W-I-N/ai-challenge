"""Все проверки Дня 6 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Главный критерий дня вынесен в отдельный скрипт (`checks/spawn_100.py`) и
запускается отсюда же первым пунктом.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from checks import _stub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_stub.install_offline()

import app.agent as agent_module  # noqa: E402
import app.main as main  # noqa: E402
from app.registry import REGISTRY  # noqa: E402
from app.schema import AgentSpec, Scenario  # noqa: E402

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


def sse(text: str) -> list[dict]:
    """Разбирает тело SSE-ответа в список событий."""
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


# --- 1. спавн сотни -----------------------------------------------------------


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


# --- 2. диалог помнит предыдущее ----------------------------------------------


@check("диалог помнит предыдущее: в третьем запросе виден первый вопрос")
def check_memory():
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        agent_id = client.post(
            "/api/agents",
            json={"agent": {"model": "stub/model", "label": "память", "system": "СИСТЕМА"}},
        ).json()["agents"][0]["id"]

        for text in ("меня зовут Нина", "мне 33 года", "как меня зовут?"):
            response = client.post(f"/api/agents/{agent_id}/messages", json={"text": text})
            assert response.status_code == 200, response.text

    assert len(_stub.CALLS) == 3, len(_stub.CALLS)
    first, second, third = (c["messages"] for c in _stub.CALLS)

    assert [m["role"] for m in first] == ["system", "user"], first
    assert first[0]["content"] == "СИСТЕМА"
    # Третий запрос: системный промпт + два обмена + новый вопрос.
    roles = [m["role"] for m in third]
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"], roles
    assert third[1]["content"] == "меня зовут Нина", third[1]
    assert third[-1]["content"] == "как меня зовут?", third[-1]
    assert len(second) == 4, second

    # Агент без памяти при тех же трёх вопросах каждый раз шлёт только вопрос.
    _stub.reset()
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        blank = client.post(
            "/api/agents",
            json={"agent": {"model": "stub/model", "label": "без памяти", "history_limit": 0}},
        ).json()["agents"][0]["id"]
        for text in ("меня зовут Нина", "мне 33 года", "как меня зовут?"):
            client.post(f"/api/agents/{blank}/messages", json={"text": text})
    assert all(len(c["messages"]) == 1 for c in _stub.CALLS), [
        len(c["messages"]) for c in _stub.CALLS
    ]
    return "с памятью 3-й запрос: 6 сообщений; history_limit=0: по 1 сообщению"


# --- 3. клиент больше не шлёт ленту -------------------------------------------


@check("клиент шлёт только текст: лента в теле запроса запрещена")
def check_no_feed():
    with TestClient(main.app) as client:
        agent_id = client.post(
            "/api/agents", json={"agent": {"model": "stub/model", "label": "т"}}
        ).json()["agents"][0]["id"]

        # Мёртвой ручки нет.
        assert client.post("/api/chat", json={"model": "x", "messages": []}).status_code == 404

        # Лента в теле — 400 с внятным текстом, а не молчаливое игнорирование.
        bad = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"text": "привет", "messages": [{"role": "user", "content": "привет"}]},
        )
        assert bad.status_code == 400, bad.text
        assert "только text" in bad.json()["detail"], bad.text

        # Модель в теле тоже нельзя: для неё есть PATCH.
        bad = client.post(
            f"/api/agents/{agent_id}/messages", json={"text": "привет", "model": "other/model"}
        )
        assert bad.status_code == 400, bad.text

        assert client.post(f"/api/agents/{agent_id}/messages", json={"text": "  "}).status_code == 400

    source = open(os.path.join(ROOT, "app", "static", "app.js"), encoding="utf-8").read()
    assert "/api/chat" not in source, "клиент всё ещё зовёт /api/chat"
    assert "col.base.concat" not in source, "клиент всё ещё склеивает ленту"
    assert "{ text }" in source, "клиент должен слать в теле только text"
    assert "/api/agents/${agentId}/messages" in source, "клиент шлёт сообщение агенту по id"
    return "POST /api/chat → 404; лишние поля в теле → 400; в app.js ленты нет"


# --- 4. два параллельных запроса к одному агенту ------------------------------


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
    roles = [t.role for t in agent.history]
    assert roles == ["user", "assistant"], roles
    assert len(_stub.CALLS) == 1, f"в модель ушёл {len(_stub.CALLS)} вызов(а), а должен один"
    return f"коды {codes}, в истории {len(agent.history)} реплики, вызов к модели один"


# --- 5. серия repeats не раздувает историю ------------------------------------


@check("серия repeats не раздувает историю: коммитится только последний ответ")
def check_repeats():
    _stub.install(reply=lambda m, i: f"прогон {i}")

    async def scenario():
        agent = REGISTRY.create(
            AgentSpec(label="серия", model="stub/model", messages=[], repeats=3)
        )
        events = [e async for e in agent.ask("вопрос")]
        return agent, events

    agent, events = asyncio.run(scenario())
    kinds = [e["type"] for e in events]
    assert kinds.count("repeat_start") == 3, kinds
    assert kinds.count("repeat_done") == 3, kinds
    assert len(_stub.CALLS) == 3, len(_stub.CALLS)

    roles = [t.role for t in agent.history]
    assert roles == ["user", "assistant"], roles
    assert agent.history[1].content == "прогон 2", agent.history[1].content
    # Все три прогона отправили один и тот же промпт: серия не наращивает контекст.
    assert len({json.dumps(c["messages"], ensure_ascii=False) for c in _stub.CALLS}) == 1
    return "3 прогона → 2 реплики в истории, промпт у всех трёх одинаковый"


# --- 6. откат несостоявшегося обмена ------------------------------------------


@check("несостоявшийся обмен: история не тронута, вопрос возвращён клиенту")
def check_rollback():
    _stub.install(fail=True)

    async def scenario():
        agent = REGISTRY.create(AgentSpec(label="падение", model="stub/model", messages=[]))
        events = [e async for e in agent.ask("вопрос, который не доедет")]
        return agent, events

    agent, events = asyncio.run(scenario())
    done = [e for e in events if e["type"] == "done"][0]
    assert agent.history == [], agent.history
    assert done["committed"] is False, done
    assert done["question"] == "вопрос, который не доедет", done

    # Частичный ответ пишется, но помечен ошибкой.
    _stub.reset()

    async def partial():
        agent = REGISTRY.create(AgentSpec(label="частичный", model="stub/model", messages=[]))

        async def half(session, *, prompt_override=None, context_length=None):
            yield {"type": "delta", "text": "полов", "metrics": {"error": None}}
            yield {"type": "error", "message": "оборвалось", "metrics": {"error": "оборвалось"}}

        agent_module.stream_completion = half
        [e async for e in agent.ask("вопрос")]
        return agent

    agent = asyncio.run(partial())
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.history[1].content == "полов", agent.history[1].content
    assert agent.history[1].error == "оборвалось", agent.history[1].error
    return "ответа нет → история пуста и вопрос вернулся; частичный → помечен ошибкой"


# --- 7. обрыв клиента гасит вызов ---------------------------------------------


@check("обрыв клиента гасит вызов: стрим закрыт, ответ в историю не дописан")
def check_disconnect():
    _stub.install(reply="а" * 400, chunks=40, delay=0.02)

    class FakeRequest:
        """Клиент, который уходит со страницы после нескольких событий."""

        def __init__(self, after: int) -> None:
            self.after = after
            self.polls = 0

        async def is_disconnected(self) -> bool:
            self.polls += 1
            return self.polls > self.after

    async def scenario():
        agent = REGISTRY.create(AgentSpec(label="обрыв", model="stub/model", messages=[]))
        request = FakeRequest(after=3)
        chunks = []
        async for frame in main._pump(lambda: main._chat_events(agent, "вопрос"), request):
            chunks.append(frame)
        # Даём отменённой задаче добежать до finally.
        await asyncio.sleep(0.1)
        return agent, chunks

    agent, chunks = asyncio.run(scenario())
    assert _stub.ACTIVE["closed"] == 1, _stub.ACTIVE
    assert _stub.ACTIVE["now"] == 0, _stub.ACTIVE
    assert len(chunks) < 40, len(chunks)
    assert agent.busy is False, "бронь агента должна сниматься и на обрыве"
    # Частичный ответ либо не записан, либо записан помеченным — но не как целый.
    if agent.history:
        assert agent.history[-1].error, agent.history[-1]
    return f"поток закрыт после {len(chunks)} кадров, стрим к модели погашен"


@check("обрыв на прогоне гасит все колонки")
def check_disconnect_run():
    _stub.install(reply="б" * 400, chunks=40, delay=0.02)
    scenario = Scenario(
        title="обрыв",
        description="",
        sessions=[
            AgentSpec(label="A", model="stub/model", messages=[{"role": "user", "content": "a"}]),
            AgentSpec(label="B", model="stub/model", messages=[{"role": "user", "content": "b"}]),
        ],
    )

    class FakeRequest:
        def __init__(self, after: int) -> None:
            self.after = after
            self.polls = 0

        async def is_disconnected(self) -> bool:
            self.polls += 1
            return self.polls > self.after

    async def run():
        with _scenarios([scenario]):
            async for _ in main._pump(lambda: main._run_events(0, {}, None), FakeRequest(after=3)):
                pass
        await asyncio.sleep(0.15)

    asyncio.run(run())
    assert _stub.ACTIVE["now"] == 0, _stub.ACTIVE
    assert _stub.ACTIVE["closed"] == 2, _stub.ACTIVE
    return "обе колонки погашены, открытых стримов не осталось"


# --- 8. прежнее поведение стенда ----------------------------------------------


class _scenarios:
    """Временно подменяет сценарии дня — проверкам нужен свой стенд."""

    def __init__(self, scenarios: list[Scenario]) -> None:
        self.scenarios = scenarios

    def __enter__(self):
        self.saved = main.SCENARIOS
        main.SCENARIOS = self.scenarios
        return self.scenarios

    def __exit__(self, *exc):
        main.SCENARIOS = self.saved
        return False


LADDER = Scenario(
    title="Проверочный прогон",
    description="depends_on, серия и судья в одном сценарии.",
    layout="split",
    judge_questions=["Кто лучше?"],
    sessions=[
        AgentSpec(
            label="Донор",
            model="stub/one",
            messages=[{"role": "user", "content": "придумай слово"}],
        ),
        AgentSpec(
            label="Потребитель",
            model="stub/two",
            messages=[{"role": "user", "content": "используй {{depends_on}} в предложении"}],
            depends_on="Донор",
        ),
        AgentSpec(
            label="Серия",
            model="stub/three",
            messages=[{"role": "user", "content": "три раза"}],
            repeats=3,
        ),
    ],
)


@check("прогон цел: depends_on, серия, судья, метрики, имена событий")
def check_run_intact():
    _stub.install(reply=lambda m, i: f"текст-{i}")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            response = client.get("/api/run/0")
            assert response.status_code == 200, response.text
            events = sse(response.text)

    names = [e["event"] for e in events]
    for required in (
        "run_start",
        "session_waiting",
        "session_start",
        "delta",
        "metrics",
        "session_done",
        "repeat_start",
        "repeat_done",
        "judge_start",
        "judge_delta",
        "judge_done",
        "run_done",
    ):
        assert required in names, f"нет события {required}: {sorted(set(names))}"

    # depends_on: {{depends_on}} заменён выводом донора.
    consumer_start = next(
        e for e in events if e["event"] == "session_start" and e["session"] == "Потребитель"
    )
    resolved = consumer_start["resolved_messages"][0]["content"]
    assert "{{depends_on}}" not in resolved, resolved
    assert "текст-" in resolved, resolved

    # Серия: три прогона, длина серии объявлена заранее.
    series_start = next(
        e for e in events if e["event"] == "session_start" and e["session"] == "Серия"
    )
    assert series_start["repeats"] == 3, series_start
    assert sum(1 for e in events if e["event"] == "repeat_done") == 3
    series_done = next(e for e in events if e["event"] == "session_done" and e["session"] == "Серия")
    assert series_done["repeats"] == 3 and len(series_done["texts"]) == 3, series_done
    assert series_done["unique"] == 3, series_done

    # Метрики доезжают: по ним клиент считает сводку.
    done = next(e for e in events if e["event"] == "session_done" and e["session"] == "Донор")
    assert done["metrics"]["cost_usd"] == 0.000123, done["metrics"]
    assert done["metrics"]["provider"] == "stub", done["metrics"]

    # Судья — отдельный агент в реестре, и он свежий.
    judge_start = next(e for e in events if e["event"] == "judge_start")
    assert judge_start["model"] == main.JUDGE_MODEL, judge_start
    judge = REGISTRY.require(judge_start["agent"])
    assert judge.spec.label == "Судья", judge.spec.label
    judge_call = _stub.CALLS[-1]["messages"]
    assert judge_call[0]["role"] == "system", judge_call[0]
    assert "Донор" in judge_call[1]["content"], judge_call[1]["content"][:200]
    assert "придумай слово" not in judge_call[1]["content"], "промпты колонок судье не уходят"

    # Каждое событие колонки помечено id агента — по нему клиент открывает дорожку.
    for event in events:
        if event.get("session"):
            assert event.get("agent"), event
    return f"{len(events)} событий, все имена прежние, судья {judge.id}"


@check("судья спавнится свежим на каждый прогон")
def check_judge_fresh():
    _stub.install(reply=lambda m, i: f"т{i}")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            first = sse(client.get("/api/run/0").text)
            second = sse(client.get("/api/run/0").text)
    a = next(e for e in first if e["event"] == "judge_start")["agent"]
    b = next(e for e in second if e["event"] == "judge_start")["agent"]
    assert a != b, (a, b)
    assert len(REGISTRY.require(b).history) == 2, "у свежего судьи только текущий вердикт"
    return f"первый прогон {a}, второй {b}"


@check("«Старт» убивает предыдущий набор субагентов")
def check_run_replaces_agents():
    _stub.install(reply="ок")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            first = sse(client.get("/api/run/0").text)
            live_after_first = client.get("/api/health").json()["agents_live"]
            second = sse(client.get("/api/run/0").text)
            live_after_second = client.get("/api/health").json()["agents_live"]
    old = {a["agent"] for a in next(e for e in first if e["event"] == "run_start")["agents"]}
    new = {a["agent"] for a in next(e for e in second if e["event"] == "run_start")["agents"]}
    assert not (old & new), (old, new)
    assert all(REGISTRY.get(i) is None for i in old), "старый набор должен быть убит"
    assert live_after_second <= live_after_first + 1, (live_after_first, live_after_second)
    return f"после первого прогона {live_after_first}, после второго {live_after_second}"


# --- 9. команда /прогон -------------------------------------------------------


@check("/прогон спавнит субагентов, поток событий тот же, сводка уходит родителю")
def check_command_run():
    _stub.install(reply=lambda m, i: f"вывод-{i}")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            parent = client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "Ассистент"}}
            ).json()["agents"][0]["id"]
            response = client.post(f"/api/agents/{parent}/messages", json={"text": "/прогон 1"})
            assert response.status_code == 200, response.text
            events = sse(response.text)
            children = client.get(f"/api/agents?parent={parent}&children_only=true").json()

    names = [e["event"] for e in events]
    assert names[0] == "command_start", names[:2]
    assert "run_start" in names and "run_done" in names and "session_done" in names
    assert names[-1] == "command_done", names[-3:]

    labels = {c["label"] for c in children["agents"]}
    assert {"Донор", "Потребитель", "Серия", "Судья"} <= labels, labels
    assert all(c["parent_id"] == parent for c in children["agents"])

    agent = REGISTRY.require(parent)
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.history[0].content == "/прогон 1"
    assert "Донор" in agent.history[1].content, agent.history[1].content
    return f"субагентов {len(children['agents'])}, сводка в истории родителя есть"


@check("команда — только с начала строки, // экранирует")
def check_command_parsing():
    from app import commands

    assert commands.parse("привет")[0] is None
    assert commands.parse("скажи /прогон")[0] is None
    assert commands.parse("//прогон") == (None, "/прогон")
    command, _ = commands.parse("/прогон 2")
    assert command is not None and commands.is_run(command) and command.arg == "2"
    assert commands.resolve_scenario("", [LADDER]) == 0
    try:
        commands.resolve_scenario("9", [LADDER])
        raise AssertionError("несуществующий сценарий должен ронять ValueError")
    except ValueError:
        pass

    _stub.install(reply="ок")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            parent = client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "p"}}
            ).json()["agents"][0]["id"]
            escaped = client.post(f"/api/agents/{parent}/messages", json={"text": "//прогон"})
            assert escaped.status_code == 200, escaped.text
            unknown = client.post(f"/api/agents/{parent}/messages", json={"text": "/чепуха"})
            assert unknown.status_code == 400, unknown.text
    assert _stub.CALLS, "экранированная команда обязана уйти в модель"
    assert _stub.CALLS[0]["messages"][-1]["content"] == "/прогон", _stub.CALLS[0]["messages"]
    return "«скажи /прогон» — не команда, «//прогон» уходит текстом, «/чепуха» → 400"


# --- 10. жизненный цикл агентов ------------------------------------------------


@check("смена модели на живом агенте")
def check_patch_model():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = client.post(
            "/api/agents", json={"agent": {"model": "stub/old", "label": "м"}}
        ).json()["agents"][0]["id"]
        patched = client.patch(f"/api/agents/{agent_id}", json={"model": "stub/new"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["model"] == "stub/new"
        assert client.patch(f"/api/agents/{agent_id}", json={"label": "x"}).status_code == 400
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "привет"})
    assert _stub.CALLS[0]["model"] == "stub/new", _stub.CALLS[0]["model"]
    return "PATCH меняет модель, следующий вызов уходит в новую"


@check("удаление агента каскадом по детям")
def check_kill_cascade():
    with TestClient(main.app) as client:
        parent = client.post(
            "/api/agents", json={"agent": {"model": "stub/m", "label": "родитель"}}
        ).json()["agents"][0]["id"]
        kids = client.post(
            "/api/agents",
            json={
                "parent_id": parent,
                "agents": [{"model": "stub/m", "label": f"ребёнок {i}"} for i in range(3)],
            },
        ).json()["agents"]
        killed = client.delete(f"/api/agents/{parent}").json()["killed"]
        assert set(killed) == {parent} | {k["id"] for k in kids}, killed
        assert client.get(f"/api/agents/{parent}").status_code == 404
    return f"убито {len(killed)} агентов одним запросом"


@check("потолок реестра вытесняет самых старых простаивающих")
def check_eviction():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=10)
    made = registry.create_many(
        [AgentSpec(label=f"a{i}", model="stub/m", messages=[]) for i in range(10)]
    )
    # Освежаем последнего: вытеснить должны первых, а не его.
    made[-1].last_used_at = made[-1].last_used_at + 100
    registry.create_many([AgentSpec(label=f"b{i}", model="stub/m", messages=[]) for i in range(5)])
    assert len(registry) == 10, len(registry)
    assert registry.get(made[0].id) is None, "самый старый должен быть вытеснен"
    assert registry.get(made[-1].id) is not None, "свежий вытесняться не должен"
    assert registry.evicted == 5, registry.evicted
    return "потолок 10: 15 созданных, 5 вытеснено, свежий жив"


@check("стенограмма агента отдаётся ручкой")
def check_transcript():
    _stub.install(reply="и тебе привет")
    with TestClient(main.app) as client:
        agent_id = client.post(
            "/api/agents",
            json={"agent": {"model": "stub/m", "label": "с", "system": "СИС"}},
        ).json()["agents"][0]["id"]
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "привет"})
        body = client.get(f"/api/agents/{agent_id}").json()
    transcript = body["transcript"]
    assert transcript[0] == {"role": "system", "content": "СИС", "seed": True}, transcript[0]
    assert [t["role"] for t in transcript[1:]] == ["user", "assistant"], transcript
    assert transcript[-1]["content"] == "и тебе привет"
    return f"{len(transcript)} реплик, системный промпт помечен seed"


# --- 11. инфраструктура --------------------------------------------------------


@check("общий httpx-клиент и семафор на процесс")
def check_shared_client():
    import app.llm as llm

    async def scenario():
        first = llm.shared_client()
        second = llm.shared_client()
        assert first is second, "клиент должен быть один на процесс"
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

    source = open(os.path.join(ROOT, "app", "llm.py"), encoding="utf-8").read()
    assert "httpx.AsyncClient(" in source
    assert source.count("httpx.AsyncClient(") == 1, "клиент создаётся ровно в одном месте"
    return "клиент один, семафор один, LLM_MAX_CONCURRENCY читается"


@check("Session остался алиасом AgentSpec: day.py дней 1–5 импортируется")
def check_session_alias():
    from app.schema import AgentSpec as Spec
    from app.schema import Session

    assert Session is Spec
    old = Session(
        label="колонка",
        model="stub/m",
        messages=[{"role": "user", "content": "x"}],
        temperature=0.2,
        max_tokens=100,
        repeats=2,
        depends_on=None,
        note="n",
        extra_body={"provider": {"allow_fallbacks": False}},
    )
    assert old.history_limit is None and old.system == ""
    return "Session is AgentSpec, старая сигнатура конструктора работает"


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
    assert "привет из консоли" in out.getvalue()
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.id in {a.id for a in REGISTRY.list()}, "CLI-агент виден в реестре процесса"
    return "ответ напечатан, история записана, агент в реестре"


@check("сценарий дня: одна модель, разный history_limit")
def check_day():
    import day

    assert len(day.SCENARIOS) >= 1
    scenario = day.SCENARIOS[0]
    models = {s.model for s in scenario.sessions}
    limits = sorted(s.history_limit for s in scenario.sessions)
    assert len(models) == 1, models
    assert limits == [0, day.WITH_MEMORY], limits
    assert scenario.judge_questions, "у сценария дня должен быть судья"
    assert day.AGENTS and all(a.messages == [] for a in day.AGENTS)

    # Тот же первый вопрос обеим колонкам — иначе сравнивать нечего.
    prompts = {json.dumps(s.messages, ensure_ascii=False) for s in scenario.sessions}
    assert len(prompts) == 1, "первый вопрос у колонок должен совпадать"

    _stub.install(reply=lambda m, i: f"план {i}")
    with TestClient(main.app) as client:
        events = sse(client.get("/api/run/0").text)
    starts = [e for e in events if e["event"] == "session_start"]
    assert len(starts) == 2, len(starts)
    assert len(_stub.CALLS) == 3, len(_stub.CALLS)  # две колонки + судья
    for call in _stub.CALLS[:2]:
        assert [m["role"] for m in call["messages"]] == ["system", "user"], call["messages"]
    return f"колонки {[s['session'] for s in starts]}, окна {limits}"


CHECKS = [
    check_spawn_100,
    check_memory,
    check_no_feed,
    check_parallel,
    check_repeats,
    check_rollback,
    check_disconnect,
    check_disconnect_run,
    check_run_intact,
    check_judge_fresh,
    check_run_replaces_agents,
    check_command_run,
    check_command_parsing,
    check_patch_model,
    check_kill_cascade,
    check_eviction,
    check_transcript,
    check_shared_client,
    check_session_alias,
    check_cli,
    check_day,
]


def main_() -> int:
    for fn in CHECKS:
        fn()

    width = max(len(name) for name, _, _ in RESULTS)
    failed = 0
    print()
    for name, ok, detail in RESULTS:
        mark = "OK  " if ok else "FAIL"
        print(f"{mark}  {name.ljust(width)}  {detail}")
        failed += 0 if ok else 1
    print()
    print(f"{len(RESULTS) - failed} из {len(RESULTS)} проверок пройдено")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_())
