"""Все проверки Дней 6 и 7 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Главные критерии вынесены в отдельные скрипты и запускаются отсюда же первыми
пунктами: `checks/spawn_100.py` — сотня агентов в одном процессе (День 6),
`checks/restart.py` — диалог, переживший перезапуск программы (День 7).

База каждой проверке достаётся своя: `_stub.install_offline()` уводит
AGENT_DB_PATH во временный каталог **до** импорта app.*, а обёртка `check`
чистит её между проверками.
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
import app.store as store_module  # noqa: E402
from app.registry import REGISTRY  # noqa: E402
from app.schema import AgentSpec, Scenario  # noqa: E402
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


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


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


# --- 2б. history_limit=0 стирает и стартовый вопрос ---------------------------


@check("history_limit=0 забывает стартовый вопрос, а не только свой ответ")
def check_blank_forgets_seed():
    """Находка ревью Дня 6: seed_messages подклеивались всегда.

    В `messages` колонки лежит сам первый вопрос со всей вводной. Пока он ехал
    в промпт на каждом ходу, колонка «без памяти» знала город, даты и бюджет
    и переспрашивать бы не стала — демонстрация на живом прогоне не
    воспроизвелась бы.

    Сценарий здесь свой, а не из day.py: проверяется семантика окна, а она
    не должна зависеть от того, про что день. Свой сценарий Дня 7 проверяет
    check_day.
    """
    _stub.install(reply=lambda m, i: f"план{i}")
    pair = Scenario(
        title="окно памяти",
        description="",
        sessions=[
            AgentSpec(
                label=name,
                model="stub/model",
                messages=[
                    {"role": "system", "content": "СИС"},
                    {"role": "user", "content": "Меня зовут Нина, еду в Казань, вегетарианка."},
                ],
                history_limit=limit,
            )
            for name, limit in (("С памятью", 20), ("Без памяти", 0))
        ],
    )
    with _scenarios([pair]):
        with TestClient(main.app) as client:
            parent = client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "Ассистент"}}
            ).json()["agents"][0]["id"]
            client.post(f"/api/agents/{parent}/messages", json={"text": "/прогон 1"})
            kids = client.get(f"/api/agents?parent={parent}&children_only=true").json()["agents"]
            blank = next(k["id"] for k in kids if k["label"] == "Без памяти")
            remembers = next(k["id"] for k in kids if k["label"] == "С памятью")
            _stub.reset()
            question = "Что мне взять из одежды?"
            client.post(f"/api/agents/{blank}/messages", json={"text": question})
            client.post(f"/api/agents/{remembers}/messages", json={"text": question})

    without, with_memory = (c["messages"] for c in _stub.CALLS)

    assert [m["role"] for m in without] == ["system", "user"], without
    assert without[-1]["content"] == question, without[-1]
    joined = " ".join(m["content"] for m in without)
    # «план1» — ответ колонки на первый вопрос: агент без памяти не должен
    # видеть ни вводную, ни то, что сам на неё ответил.
    for leak in ("Казань", "Нина", "вегетарианка", "план1"):
        assert leak not in joined, f"агент без памяти всё ещё видит «{leak}»"

    assert [m["role"] for m in with_memory] == ["system", "user", "assistant", "user"], with_memory
    assert "Казань" in with_memory[1]["content"], with_memory[1]
    assert with_memory[2]["content"] == "план0", with_memory[2]
    return "без памяти: [system, вопрос]; с памятью: вводная и ответ на месте"


@check("прогон шлёт ровно spec.messages: дни 1–5 не заметили расщепления")
def check_seed_split_keeps_run():
    _stub.install(reply=lambda m, i: f"т{i}")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            sse(client.get("/api/run/0").text)

    donor = next(c for c in _stub.CALLS if c["label"] == "Донор")
    assert donor["messages"] == LADDER.sessions[0].messages, donor["messages"]

    # Ручной вопрос колонке после прогона: та же лента, что и до Дня 6.
    _stub.reset()
    _stub.install(reply="ответ")
    agent = REGISTRY.create(
        AgentSpec(
            label="день 1-5",
            model="stub/m",
            messages=[
                {"role": "system", "content": "СИС"},
                {"role": "user", "content": "ЗАДАЧА"},
            ],
        )
    )

    async def scenario():
        [e async for e in agent.ask()]
        [e async for e in agent.ask("уточни")]

    asyncio.run(scenario())
    run_prompt, follow_up = (c["messages"] for c in _stub.CALLS)
    assert run_prompt == agent.spec.messages, run_prompt
    assert [m["content"] for m in follow_up] == ["СИС", "ЗАДАЧА", "ответ", "уточни"], follow_up

    # До «Старта» вопрос сценария всё ещё в промпте: колонка показывает его
    # в ленте, и промпт обязан сходиться с тем, что видно на экране.
    _stub.reset()
    _stub.install(reply="ответ")
    fresh = REGISTRY.create(
        AgentSpec(
            label="до старта",
            model="stub/m",
            messages=[
                {"role": "system", "content": "СИС"},
                {"role": "user", "content": "ЗАДАЧА"},
            ],
        )
    )
    asyncio.run(_drain(fresh.ask("вопрос до старта")))
    assert [m["content"] for m in _stub.CALLS[0]["messages"]] == [
        "СИС",
        "ЗАДАЧА",
        "вопрос до старта",
    ], _stub.CALLS[0]["messages"]
    return "прогон байт в байт прежний, ручной вопрос тоже, до «Старта» вводная едет"


# --- 2в. выбор в дропдауне переживает «Старт» ---------------------------------


@check("выбранная в дропдауне модель переживает «Старт» и второй «Старт»")
def check_override_survives_start():
    """Находка ревью: PATCH правил предспавненного агента, а прогон его убивал."""
    _stub.install(reply=lambda m, i: f"т{i}")
    scenario = Scenario(
        title="дропдаун",
        description="",
        sessions=[
            AgentSpec(label="A", model="сценарный/model", messages=[{"role": "user", "content": "a"}])
        ],
    )
    with _scenarios([scenario]):
        with TestClient(main.app) as client:
            parent = client.post(
                "/api/agents", json={"agent": {"model": "stub/chat", "label": "Ассистент"}}
            ).json()["agents"][0]["id"]
            column = client.post(f"/api/scenarios/0/agents?parent={parent}").json()["agents"][0]["id"]
            patched = client.patch(
                f"/api/agents/{column}", json={"model": "выбранная/пользователем", "temperature": 0.9}
            )
            assert patched.json()["model"] == "выбранная/пользователем", patched.text

            _stub.reset()
            first = sse(client.post(f"/api/agents/{parent}/messages", json={"text": "/прогон 1"}).text)
            after_first = [c["model"] for c in _stub.CALLS]

            _stub.reset()
            sse(client.post(f"/api/agents/{parent}/messages", json={"text": "/прогон 1"}).text)
            after_second = [c["model"] for c in _stub.CALLS]

            # Явный overrides в запросе сильнее перенесённого.
            _stub.reset()
            sse(client.get('/api/run/0?overrides={"A":{"model":"из/запроса"}}').text)
            explicit = [c["model"] for c in _stub.CALLS]

    assert after_first == ["выбранная/пользователем"], after_first
    assert after_second == ["выбранная/пользователем"], after_second
    assert explicit == ["из/запроса"], explicit

    # Клиент перерисовывает колонки по run_start — там тоже должна быть
    # выбранная модель, иначе дропдаун откатится на экране.
    run_start = next(e for e in first if e["event"] == "run_start")
    assert run_start["sessions"][0]["model"] == "выбранная/пользователем", run_start["sessions"][0]
    assert run_start["sessions"][0]["temperature"] == 0.9, run_start["sessions"][0]
    return "первый и второй «Старт» идут на выбранной модели, overrides из запроса сильнее"


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
    assert live_after_second == live_after_first, (live_after_first, live_after_second)
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


@check("залипшая бронь: разрыв до первого события снимает бронь")
def check_reservation_released_on_early_abort():
    """Находка ревью: release() стоял в finally генератора событий.

    Если клиент отвалился на первом же опросе, задача с генератором
    отменяется, не начав выполняться, — его finally не срабатывает никогда.
    Бронь залипала бы навсегда: агент вечно отвечал бы 409 и никогда бы
    не вытеснился по потолку, потому что числится занятым.
    """
    _stub.install(reply="ок", chunks=10, delay=0.02)

    class Gone:
        """Клиент, которого уже нет к моменту первого опроса."""

        async def is_disconnected(self) -> bool:
            return True

    async def scenario():
        agent = REGISTRY.create(AgentSpec(label="бронь", model="stub/model", messages=[]))
        agent.reserve()
        frames = [
            frame
            async for frame in main._pump(
                lambda: main._chat_events(agent, "вопрос"), Gone(), agent.release
            )
        ]
        await asyncio.sleep(0.05)
        return agent, frames

    agent, frames = asyncio.run(scenario())
    assert frames == [], frames
    assert agent.busy is False, "бронь залипла: агент навсегда отвечает 409"

    # После снятия брони агент снова разговаривает и снова вытесняется.
    _stub.reset()
    _stub.install(reply="и снова ок")
    asyncio.run(_drain(agent.ask("второй вопрос")))
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    return "разрыв на первом опросе: 0 кадров, бронь снята, агент снова отвечает"


@check("прогон без родителя не оставляет ни колонок, ни судьи")
def check_orphan_run_cleanup():
    _stub.install(reply="ок")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            sse(client.get("/api/run/0").text)
            after_first = client.get("/api/health").json()["agents_live"]
            sse(client.get("/api/run/0").text)
            after_second = client.get("/api/health").json()["agents_live"]
    # 3 колонки + судья, и ни одним больше: второй прогон убирает весь первый.
    assert after_first == 4, after_first
    assert after_second == after_first, (after_first, after_second)
    judges = [a for a in REGISTRY.list() if a.spec.label == "Судья"]
    assert len(judges) == 1, [a.id for a in judges]
    return f"после первого прогона {after_first}, после второго {after_second}, судья один"


@check("конфиг агента копируется вглубь: сто агентов не делят один extra_body")
def check_spec_deep_copy():
    from app.registry import AgentRegistry

    shared = AgentSpec(
        label="общий",
        model="stub/model",
        messages=[{"role": "system", "content": "СИС"}],
        stop=["\n"],
        response_format={"type": "json_object"},
        extra_body={"provider": {"allow_fallbacks": False}},
    )
    registry = AgentRegistry(max_agents=100)
    first, second = registry.create_many([shared, shared])

    assert first.spec.extra_body is not shared.extra_body
    assert first.spec.extra_body["provider"] is not shared.extra_body["provider"]
    assert first.spec.extra_body is not second.spec.extra_body
    assert first.spec.messages is not shared.messages
    assert first.spec.messages[0] is not shared.messages[0]
    assert first.spec.stop is not shared.stop
    assert first.spec.response_format is not shared.response_format

    first.spec.extra_body["provider"]["order"] = ["only-me"]
    first.spec.messages[0]["content"] = "ДРУГОЕ"
    first.spec.stop.append("СТОП")
    assert "order" not in shared.extra_body["provider"], shared.extra_body
    assert "order" not in second.spec.extra_body["provider"], second.spec.extra_body
    assert shared.messages[0]["content"] == "СИС", shared.messages
    assert shared.stop == ["\n"], shared.stop
    return "правка у одного агента не задела ни день, ни соседа"


@check("вытеснение уносит детей вместе с родителем")
def check_eviction_cascade():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=6)
    parent = registry.create(AgentSpec(label="родитель", model="stub/m", messages=[]))
    kids = registry.create_many(
        [AgentSpec(label=f"ребёнок {i}", model="stub/m", messages=[]) for i in range(3)],
        parent_id=parent.id,
    )
    fresh = registry.create_many(
        [AgentSpec(label=f"новый {i}", model="stub/m", messages=[]) for i in range(2)]
    )
    for agent in fresh:
        agent.last_used_at += 100

    registry.create_many([AgentSpec(label=f"ещё {i}", model="stub/m", messages=[]) for i in range(2)])

    assert registry.get(parent.id) is None, "родитель должен быть вытеснен"
    orphans = [k.id for k in kids if registry.get(k.id) is not None]
    assert not orphans, f"дети остались сиротами: {orphans}"
    assert all(registry.get(a.id) is not None for a in fresh), "свежие вытесняться не должны"
    assert len(registry) <= registry.max_agents, len(registry)
    return f"родитель и {len(kids)} ребёнка ушли одним каскадом, сирот нет"


@check("занятого агента и его родителя вытеснение не трогает")
def check_eviction_skips_busy():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=3)
    parent = registry.create(AgentSpec(label="родитель", model="stub/m", messages=[]))
    child = registry.create(AgentSpec(label="ребёнок", model="stub/m", messages=[]), parent_id=parent.id)
    child.reserve()
    registry.create_many([AgentSpec(label=f"новый {i}", model="stub/m", messages=[]) for i in range(3)])
    assert registry.get(child.id) is not None, "занятого вытеснять нельзя"
    assert registry.get(parent.id) is not None, "родителя занятого — тоже"
    child.release()
    return "занятый ребёнок и его родитель пережили вытеснение"


@check("потолки: размер пачки и repeats")
def check_limits():
    with TestClient(main.app) as client:
        too_many = client.post(
            "/api/agents",
            json={"agents": [{"model": "stub/m"} for _ in range(main.MAX_SPAWN_BATCH + 1)]},
        )
        assert too_many.status_code == 400, too_many.status_code
        assert str(main.MAX_SPAWN_BATCH) in too_many.json()["detail"], too_many.text

        ok = client.post("/api/agents", json={"agents": [{"model": "stub/m"} for _ in range(3)]})
        assert ok.status_code == 200, ok.text

        greedy = client.post(
            "/api/agents", json={"agent": {"model": "stub/m", "repeats": 1_000_000}}
        )
        assert greedy.status_code == 400, greedy.status_code
        assert str(main.MAX_REPEATS) in greedy.json()["detail"], greedy.text

        assert client.post(
            "/api/agents", json={"agent": {"model": "stub/m", "repeats": main.MAX_REPEATS}}
        ).status_code == 200
    return f"пачка > {main.MAX_SPAWN_BATCH} → 400, repeats > {main.MAX_REPEATS} → 400"


@check("судью можно переспросить: он помнит собственный вердикт")
def check_judge_remembers_verdict():
    _stub.install(reply=lambda m, i: f"вердикт{i}" if i == 3 else f"ответ{i}")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            events = sse(client.get("/api/run/0").text)
            judge_id = next(e for e in events if e["event"] == "judge_start")["agent"]
            verdict = next(e for e in events if e["event"] == "judge_done")["text"]
            _stub.reset()
            again = client.post(
                f"/api/agents/{judge_id}/messages", json={"text": "почему ты так решил?"}
            )
            assert again.status_code == 200, again.text

    sent = _stub.CALLS[0]["messages"]
    roles = [m["role"] for m in sent]
    assert roles == ["system", "user", "assistant", "user"], roles
    assert sent[2]["content"] == verdict, (sent[2]["content"], verdict)
    assert "Донор" in sent[1]["content"], sent[1]["content"][:120]
    assert sent[-1]["content"] == "почему ты так решил?", sent[-1]
    return "в переспросе едут данные колонок и собственный вердикт судьи"


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


@check("сценарии дня: перезапуск в одной колонке, изоляция сессий в двух")
def check_day():
    """День 7 сменил постановку, и сценарии сменились вместе с ней.

    Первый сценарий — сама постановка: одна колонка, один агент, разговор,
    который надо продолжить после рестарта; судьи там нет и быть не должно.
    Второй — то, на чём спотыкаются: две сессии, и правая не знает того, что
    сказали левой.
    """
    import day

    assert len(day.SCENARIOS) == 2, len(day.SCENARIOS)
    restart, isolation = day.SCENARIOS

    assert restart.layout == "single", restart.layout
    assert len(restart.sessions) == 1, restart.sessions
    assert restart.sessions[0].history_limit == day.MEMORY_WINDOW
    assert not restart.judge_questions, "судить сразу после первого хода нечего"
    # Постановка дня должна быть видна в описании: перезапуск, а не просто память.
    for word in ("перезапуск", "SQLite"):
        assert word.casefold() in restart.description.casefold(), word

    assert isolation.layout == "split" and len(isolation.sessions) == 2
    assert len({s.model for s in isolation.sessions}) == 1, "разница должна быть в сессиях"
    assert isolation.judge_questions, "у сценария про изоляцию должен быть судья"
    left, right = isolation.sessions
    assert "Нина" in json.dumps(left.messages, ensure_ascii=False)
    assert "Нина" not in json.dumps(right.messages, ensure_ascii=False), (
        "правая колонка не должна знать имени — она про чужой разговор"
    )
    assert day.AGENTS and all(a.messages == [] for a in day.AGENTS)

    _stub.install(reply=lambda m, i: f"план {i}")
    with TestClient(main.app) as client:
        first = sse(client.get("/api/run/0").text)
        _stub.reset()
        second = sse(client.get("/api/run/1").text)

    assert len([e for e in first if e["event"] == "session_start"]) == 1
    assert not [e for e in first if e["event"] == "judge_start"], "судьи в первом сценарии нет"

    starts = [e for e in second if e["event"] == "session_start"]
    assert len(starts) == 2, len(starts)
    assert len(_stub.CALLS) == 3, len(_stub.CALLS)  # две колонки + судья
    column_prompts = _stub.CALLS[:2]
    for call in column_prompts:
        assert [m["role"] for m in call["messages"]] == ["system", "user"], call["messages"]
    right_prompt = " ".join(m["content"] for m in column_prompts[1]["messages"])
    assert "Нина" not in right_prompt and "Казань" not in right_prompt, right_prompt
    return f"сценарий 1 — одна колонка без судьи; сценарий 2 — {[s['session'] for s in starts]}"


# --- 12. День 7: память между запусками ---------------------------------------


def _temp_db(name: str) -> str:
    """Свежий файл базы под одну проверку. Каталога заранее нет — его создаёт Store."""
    import tempfile

    return os.path.join(tempfile.mkdtemp(prefix=f"check-{name}-"), "nested", "agents.db")


class _Turn:
    """Минимальная реплика: хранилищу от неё нужны пять полей, и только они."""

    def __init__(self, role, content, error=None, at=1.0):
        self.role, self.content, self.error, self.at = role, content, error, at


@check("настоящий перезапуск: файл базы закрыт и открыт заново")
def check_store_reopen():
    path = _temp_db("reopen")
    assert not os.path.isdir(os.path.dirname(path)), "каталога быть не должно — его создаёт init()"

    first = Store(path).init()
    first.save_session(
        "ag_00042",
        parent_id=None,
        label="Нина",
        config={"model": "stub/m", "history_limit": 7},
        seed=[{"role": "system", "content": "СИС"}],
        overrides={"model": "выбранная/пользователем"},
        seed_used=True,
        created_at=100.0,
    )
    first.save_history(
        "ag_00042",
        [_Turn("user", "меня зовут Нина"), _Turn("assistant", "привет, Нина")],
    )
    first.close()

    # Новый объект на том же файле — это и есть перезапуск, а не «тот же
    # объект в памяти»: соединение закрыто, кеш sqlite ушёл вместе с ним.
    second = Store(path).init()
    saved = second.load_session("ag_00042")
    assert saved is not None, "сессия не пережила закрытие файла"
    assert saved["config"]["history_limit"] == 7, saved["config"]
    assert saved["overrides"] == {"model": "выбранная/пользователем"}, saved["overrides"]
    assert saved["seed_used"] is True and saved["created_at"] == 100.0
    assert saved["seed"] == [{"role": "system", "content": "СИС"}], saved["seed"]
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


@check("восстановление истории — в конструкторе агента, а не отдельным вызовом")
def check_restore_in_constructor():
    from app.agent import Agent

    path = _temp_db("ctor")
    store = Store(path).init()
    spec = AgentSpec(label="ctor", model="stub/m", messages=[], system="СИС")

    born = Agent(spec, store=store)
    born.remember_exchange("меня зовут Нина", "привет, Нина")
    born.remember_exchange("мне 33", "запомнил")
    store.close()

    # Тот же id, новый объект, новое открытие файла — история обязана приехать
    # сама, без единого вызова restore().
    again = Store(path).init()
    revived = Agent(spec, agent_id=born.id, store=again)
    assert [t.content for t in revived.history] == [
        "меня зовут Нина",
        "привет, Нина",
        "мне 33",
        "запомнил",
    ], revived.history
    assert not hasattr(Agent, "restore"), "отдельного restore() быть не должно — его забудут"

    _stub.install(reply="ок")
    asyncio.run(_drain(revived.ask("как меня зовут?")))
    sent = [m["content"] for m in _stub.CALLS[0]["messages"]]
    assert sent == ["СИС", "меня зовут Нина", "привет, Нина", "мне 33", "запомнил", "как меня зовут?"], sent
    again.close()
    return "агент поднят конструктором: 4 реплики из базы уехали в следующий запрос"


@check("изоляция сессий: у сообщений есть session_id, ленты не сливаются")
def check_session_isolation():
    _stub.install(reply=lambda m, i: f"ответ{i}")
    with TestClient(main.app) as client:
        first, second = (
            client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": f"чат {n}"}}
            ).json()["agents"][0]["id"]
            for n in (1, 2)
        )
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
    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(messages)")}
    assert "session_id" in columns, columns
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
    agent = Agent(AgentSpec(label="seq", model="stub/m", messages=[]), store=store)

    # 1) Несостоявшийся обмен: в базе не должно появиться вопроса без ответа.
    _stub.install(fail=True)
    asyncio.run(_drain(agent.ask("вопрос, на который не ответили")))
    assert store.message_rows(agent.id) == [], store.message_rows(agent.id)

    # 2) Обычные обмены и кап хранимого.
    _stub.reset()
    _stub.install(reply="ок")
    for i in range(3):
        asyncio.run(_drain(agent.ask(f"вопрос {i}")))
    seqs = [row[0] for row in store.message_rows(agent.id)]
    assert seqs == list(range(6)), seqs

    agent.history = agent.history[-2:]  # так выглядит история после кап-а окна
    agent.persist()
    seqs = [row[0] for row in store.message_rows(agent.id)]
    assert seqs == [0, 1], f"после укорачивания номера должны идти от нуля: {seqs}"

    # 3) Жёсткий потолок хранимого держится и в базе.
    agent.history = [_Turn("user", f"т{i}") for i in range(MAX_STORED_MESSAGES + 10)]
    agent._trim()
    agent.persist()
    rows = store.message_rows(agent.id)
    assert len(rows) == MAX_STORED_MESSAGES, len(rows)
    assert [r[0] for r in rows] == list(range(MAX_STORED_MESSAGES))
    store.close()
    return f"нет ответа — нет записи; после укорачивания seq = 0..N, потолок {MAX_STORED_MESSAGES}"


@check("частичный ответ пишется в базу с пометкой ошибки, парой с вопросом")
def check_partial_persisted():
    from app.agent import Agent

    path = _temp_db("partial")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="partial", model="stub/m", messages=[]), store=store)

    async def broken(session, *, prompt_override=None, context_length=None):
        yield {"type": "delta", "text": "начал отвеч", "metrics": None}
        raise RuntimeError("провод оборвался")

    agent_module.stream_completion = broken
    asyncio.run(_drain(agent.ask("вопрос")))
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


@check("вытеснение — выгрузка, а не удаление: сессия остаётся в базе")
def check_eviction_keeps_session():
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=2)
    old = registry.create(AgentSpec(label="старый", model="stub/m", messages=[]))
    old.remember_exchange("меня зовут Нина", "привет")
    old.spec.temperature = 0.9
    old.overrides["temperature"] = 0.9
    old.save_config()
    old_id = old.id

    registry.create_many(
        [AgentSpec(label=f"новый {i}", model="stub/m", messages=[]) for i in range(3)]
    )
    assert registry.get(old_id) is None, "старый должен быть выгружен из памяти"
    assert registry.evicted >= 1, registry.evicted
    assert registry.store.load_session(old_id) is not None, "выгрузка не должна удалять сессию"

    revived = registry.require(old_id)
    assert revived is not old, "поднят новый объект, а не тот же самый"
    assert [t.content for t in revived.history] == ["меня зовут Нина", "привет"], revived.history
    assert revived.spec.temperature == 0.9, revived.spec.temperature
    assert revived.overrides == {"temperature": 0.9}, revived.overrides
    assert len(registry) <= registry.max_agents, len(registry)

    # А удаление удаляет — и из памяти, и из базы.
    registry.kill(revived.id)
    assert registry.store.load_session(old_id) is None, "kill обязан стереть и строку в базе"
    return "выгруженная сессия поднялась с историей и конфигом; kill стёр её из базы"


@check("прогон пишет все сессии разом, parent_id переживает перезапуск")
def check_run_sessions_persist():
    _stub.install(reply=lambda m, i: f"вывод-{i}")
    with _scenarios([LADDER]):
        with TestClient(main.app) as client:
            parent = client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "Ассистент"}}
            ).json()["agents"][0]["id"]
            client.post(f"/api/agents/{parent}/messages", json={"text": "/прогон 1"})

    path = str(REGISTRY.store.path)
    # Новое открытие того же файла: так стенд увидит базу после рестарта.
    after = Store(path).init()
    rows = {row["id"]: row for row in after.list_sessions()}
    children = [row for row in rows.values() if row["parent_id"] == parent]
    labels = {row["label"] for row in children}
    assert {"Донор", "Потребитель", "Серия", "Судья"} <= labels, labels
    assert all(row["history_len"] >= 2 for row in children), [
        (r["label"], r["history_len"]) for r in children
    ]
    # Сводка родителя тоже в базе — иначе после рестарта прогона как не было.
    assert rows[parent]["history_len"] == 2, rows[parent]["history_len"]

    # Подстановка depends_on сохранена: после рестарта колонка помнит итоговый
    # промпт, а не шаблон.
    consumer = next(row for row in children if row["label"] == "Потребитель")
    seed = json.dumps(consumer["seed"], ensure_ascii=False)
    assert "{{depends_on}}" not in seed, seed
    after.close()
    return f"{len(children)} субсессии с parent_id, сводка родителя и подстановка в базе"


@check("параллельная запись: восемь сессий пишут одновременно, ничего не теряется")
def check_parallel_writes():
    from app.agent import Agent

    path = _temp_db("parallel")
    store = Store(path).init()
    _stub.install(reply=lambda m, i: f"ответ-{i}", chunks=6, delay=0.002)

    agents = [
        Agent(AgentSpec(label=f"колонка {i}", model="stub/m", messages=[]), store=store)
        for i in range(8)
    ]

    async def all_at_once():
        await asyncio.gather(*(_drain(a.ask(f"вопрос {i}")) for i, a in enumerate(agents)))

    asyncio.run(all_at_once())
    store.close()

    reopened = Store(path).init()
    for i, agent in enumerate(agents):
        rows = reopened.message_rows(agent.id)
        assert [r[0] for r in rows] == [0, 1], (agent.id, rows)
        assert rows[0][2] == f"вопрос {i}", rows
        assert rows[1][1] == "assistant", rows
    total = reopened.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert total == 16, total
    reopened.close()
    return "8 сессий писали одновременно, в базе 16 реплик, порядок в каждой свой"


@check("ключа OpenRouter в базе нет ни в одной колонке — включая ещё не придуманные")
def check_no_key_in_db():
    """Ключ подставляется во все текстовые пути, а не только в те, где есть redact().

    Прошлая версия проверки клала ключ ровно туда, где редакция была написана
    руками, — и потому не заметила бы, что `label` и `error` пишутся сырыми.
    Теперь ключ едет во всё, куда вообще попадает текст, а ищется он не в тех
    колонках, которые мы вспомнили, а во всех значениях всех строк обеих таблиц
    и, сверх того, во всех файлах базы побайтно.
    """
    from app.agent import Agent

    key = "sk-or-v1-ТЕСТОВЫЙ-КЛЮЧ-КОТОРЫЙ-НЕ-ДОЛЖЕН-УТЕЧЬ"
    saved_key = os.environ.get("OPENROUTER_API_KEY")
    os.environ["OPENROUTER_API_KEY"] = key
    path = _temp_db("secret")
    store = Store(path).init()
    try:
        agent = Agent(
            AgentSpec(
                # label и note раньше уезжали в базу сырыми: label отдельной
                # колонкой, note — внутри конфига.
                label=f"утечка {key}",
                model="stub/m",
                messages=[{"role": "system", "content": f"ключ: {key}"}],
                extra_body={"headers": {"Authorization": f"Bearer {key}"}},
                system=key,
                note=key,
            ),
            store=store,
        )
        agent.overrides["model"] = key
        agent.save_config()
        # В error приезжает тело ответа OpenRouter (app/llm.py) — путь не выдуман.
        agent.remember_exchange(
            f"вот мой ключ {key}", "не надо мне его слать", error=f"HTTP 401: {key}"
        )

        # А это — про колонки, которых ещё нет: любая запись идёт через
        # транзакцию, и параметр чистится независимо от того, вспомнил ли
        # автор про redact() в этом конкретном методе.
        with store.tx() as conn:
            conn.execute("UPDATE sessions SET label = ? WHERE id = ?", (key, agent.id))

        # Ищем не в тех колонках, что вспомнили, а во всех значениях обеих таблиц.
        leaked = []
        for table in ("sessions", "messages"):
            for row in store.conn.execute(f"SELECT * FROM {table}"):
                for name in row.keys():
                    value = row[name]
                    if isinstance(value, str) and key in value:
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

    # И сам ключ читается только из окружения: в конфиг агента он не попадает.
    source = open(os.path.join(ROOT, "app", "store.py"), encoding="utf-8").read()
    assert "api_key" in source, "хранилище обязано знать про ключ, чтобы его вырезать"
    return f"ключ не найден ни в одной колонке и ни в одном файле базы ({', '.join(files)})"


# --- 13. два процесса на одной базе -------------------------------------------


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


@check("id выдаёт база: занятый номер не выдаётся второй раз")
def check_id_claimed_in_store():
    """То же, что checks/two_processes.py, но без процессов — на одном файле.

    Счётчик id живёт в памяти процесса, поэтому «второй процесс» здесь
    изображается сдвигом счётчика назад: так же выглядит давно поднятый стенд,
    мимо которого консоль успела занять следующий номер.
    """
    from app.agent import Agent
    import app.agent as agent_mod

    path = _temp_db("claim")
    store = Store(path).init()

    stranger = Agent(AgentSpec(label="чужая", model="stub/m", messages=[]), store=store)
    stranger.remember_exchange("СЕКРЕТ соседа", "ответ соседа")
    taken = stranger.id

    # Откатываем счётчик ровно на этот номер: так и выглядит давно поднятый
    # стенд, мимо которого консоль успела занять следующий id.
    agent_mod._last_id = int(taken.removeprefix("ag_")) - 1
    assert agent_mod.new_agent_id() == taken, "счётчик должен целиться в занятый номер"
    agent_mod._last_id = int(taken.removeprefix("ag_")) - 1

    mine = Agent(AgentSpec(label="моя", model="stub/m", messages=[]), store=store)
    assert mine.id != taken, f"выдан занятый id {taken}"
    mine.remember_exchange("мой вопрос", "мой ответ")

    survived = [row[2] for row in store.message_rows(taken)]
    assert survived == ["СЕКРЕТ соседа", "ответ соседа"], survived
    assert store.load_session(taken)["label"] == "чужая", "чужой конфиг перезаписан"
    store.close()
    return f"{taken} остался за соседом, свежий агент получил {mine.id}"


@check("выгруженный объект больше не пишет в сессию")
def check_detached_does_not_clobber():
    """До Дня 7 такого класса не было: вытеснение удаляло агента совсем.

    Теперь сессия переживает выгрузку, и на неё может смотреть два объекта:
    поднятый из базы и тот, чью ссылку кто-то придержал. Писать вправе только
    первый — иначе `persist()` старого затрёт реплики нового.
    """
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=1)
    first = registry.create(AgentSpec(label="долгая", model="stub/m", messages=[]))
    first.remember_exchange("вопрос 1", "ответ 1")

    registry.create(AgentSpec(label="вытесняющая", model="stub/m", messages=[]))
    assert registry.get(first.id) is None, "первый должен быть выгружен"
    assert first.detached is True and first.store is None, "выгруженный обязан отцепиться"

    second = registry.require(first.id)
    assert second is not first, "поднят тот же объект — проверка ничего не проверяет"
    second.remember_exchange("вопрос 2", "ответ 2")

    # Старая ссылка продолжает работать в памяти, но в базу не лезет.
    first.remember_exchange("мусор", "мусор")
    assert [t.content for t in first.history][-2:] == ["мусор", "мусор"]

    stored = [row[2] for row in registry.store.message_rows(first.id)]
    assert stored == ["вопрос 1", "ответ 1", "вопрос 2", "ответ 2"], stored
    return "выгруженный объект пишет только в память, база остаётся за поднятым"


@check("колонка занята всю дорожку прогона, включая ожидание depends_on")
def check_waiting_column_is_busy():
    """Ждущий субагент не держит lock — и потолок реестра вытеснил бы его
    прямо из-под идущего прогона, оставив на одной сессии два объекта."""
    _stub.install(reply="ок", chunks=4, delay=0.02)
    seen: dict = {}

    async def scenario():
        stream = main._run_events(0, {}, None)
        async for event in stream:
            if event.get("event") == "session_waiting" and "busy" not in seen:
                waiting = REGISTRY.require(event["agent"])
                seen["busy"] = waiting.busy
                seen["evictable"] = REGISTRY._evictable(waiting)

    with _scenarios([LADDER]):
        asyncio.run(scenario())

    assert seen.get("busy") is True, "ждущая колонка числится свободной"
    assert seen.get("evictable") is False, "ждущую колонку вытеснение всё ещё трогает"
    # После прогона бронь снята — иначе колонка навсегда отвечала бы 409.
    assert all(not a.busy for a in REGISTRY.list()), [a.id for a in REGISTRY.list() if a.busy]
    return "колонка на ожидании depends_on занята и вытеснению недоступна"


@check("context_length переживает выгрузку и перезапуск")
def check_context_length_restored():
    path = _temp_db("ctxlen")
    store = Store(path).init()
    from app.registry import AgentRegistry

    registry = AgentRegistry(max_agents=10, store=store)
    agent = registry.create(
        AgentSpec(label="контекст", model="stub/m", messages=[]), context_length=128_000
    )
    registry._unload(agent.id)
    revived = registry.require(agent.id)
    assert revived.context_length == 128_000, revived.context_length

    store.close()
    reopened = Store(path).init()
    assert reopened.load_session(agent.id)["context_length"] == 128_000
    reopened.close()
    return "после выгрузки и после переоткрытия файла context_fill_pct снова считается"


@check("ручка сессий показывает действующее окно памяти, а не null")
def check_sessions_effective_window():
    from app.agent import DEFAULT_HISTORY_LIMIT

    with TestClient(main.app) as client:
        default_id = client.post(
            "/api/agents", json={"agent": {"model": "stub/m", "label": "по умолчанию"}}
        ).json()["agents"][0]["id"]
        blank_id = client.post(
            "/api/agents",
            json={"agent": {"model": "stub/m", "label": "без памяти", "history_limit": 0}},
        ).json()["agents"][0]["id"]
        rows = {s["id"]: s for s in client.get("/api/sessions").json()["sessions"]}

    assert rows[default_id]["history_limit"] == DEFAULT_HISTORY_LIMIT, rows[default_id]
    assert rows[blank_id]["history_limit"] == 0, rows[blank_id]
    return f"дефолт показан как {DEFAULT_HISTORY_LIMIT}, а не null; ноль остался нулём"


@check(".gitignore ловит базу и её WAL-файлы")
def check_gitignore_db():
    patterns = [
        line.strip()
        for line in open(os.path.join(ROOT, ".gitignore"), encoding="utf-8")
        if line.strip() and not line.startswith("#")
    ]
    for needed in ("*.db", "*.db-wal", "*.db-shm"):
        assert needed in patterns, f"{needed} не в .gitignore: база уедет в публичный репозиторий"

    # И проверяем не только текст, но и сам git: он единственный судья.
    probe = ["data/agents.db", "data/agents.db-wal", "data/agents.db-shm", "agents.db"]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *probe],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    ignored = set(result.stdout.split())
    assert set(probe) <= ignored, f"git не игнорирует: {sorted(set(probe) - ignored)}"
    return "git игнорирует *.db, *.db-wal, *.db-shm и каталог data/"


@check("ручка сессий: список, открытие вытесненной, удаление из базы")
def check_sessions_api():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = client.post(
            "/api/agents", json={"agent": {"model": "stub/m", "label": "сессия"}}
        ).json()["agents"][0]["id"]
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "привет"})

        listing = client.get("/api/sessions").json()
        row = next(s for s in listing["sessions"] if s["id"] == agent_id)
        assert row["live"] is True and row["history_len"] == 2, row
        assert listing["stored"] >= 1 and "evicted" in listing

        # Выгружаем сессию из памяти — ручка обязана поднять её из базы.
        REGISTRY._unload(agent_id)
        assert REGISTRY.get(agent_id) is None
        stored = next(
            s for s in client.get("/api/sessions").json()["sessions"] if s["id"] == agent_id
        )
        assert stored["live"] is False, stored

        body = client.get(f"/api/agents/{agent_id}").json()
        assert [t["content"] for t in body["transcript"] if not t.get("seed")] == ["привет", "ок"]
        assert client.get("/api/health").json()["sessions_stored"] >= 1

        # Удаление — из обоих слоёв разом.
        client.delete(f"/api/agents/{agent_id}")
        assert client.get(f"/api/agents/{agent_id}").status_code == 404
        assert all(
            s["id"] != agent_id for s in client.get("/api/sessions").json()["sessions"]
        )
    return "список сессий отдаётся, выгруженная поднимается из базы, DELETE стирает её"


@check("CLI продолжает сохранённую сессию: --session и /сессии")
def check_cli_session():
    import io

    from app import cli

    _stub.install(reply="запомнил")
    first = cli.build_agent(cli._parse_args(["--model", "stub/m", "--label", "консоль"]))
    asyncio.run(cli.ask(first, "меня зовут Нина", io.StringIO()))

    # Выгружаем из памяти: для CLI второго запуска в памяти нет вообще ничего.
    REGISTRY._unload(first.id)
    assert REGISTRY.get(first.id) is None

    second = cli.build_agent(cli._parse_args(["--session", first.id]))
    assert second.id == first.id and [t.content for t in second.history] == [
        "меня зовут Нина",
        "запомнил",
    ], second.history

    # repl читает вопрос со stdin — подменяем его пустым, иначе проверка
    # повисла бы на вводе. Нужна только шапка: она и есть то, что видит
    # человек во втором запуске.
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

    missing = cli._parse_args(["--session", "ag_99999"])
    try:
        cli.build_agent(missing)
        raise AssertionError("несуществующая сессия должна честно падать")
    except SystemExit:
        pass
    return "--session поднимает сессию из базы, /сессии её показывает"


@check("вводная колонки едет в промпт до «Старта», а после — только историей")
def check_seed_until_started():
    """Хвост Дня 6: вводная исчезала из промпта после первого ручного обмена.

    Колонка продолжала показывать стартовый вопрос в ленте, а в модель он
    больше не уезжал — промпт расходился с экраном. Признак теперь не «история
    пуста», а «стартовый вопрос уже стал ходом».
    """
    _stub.install(reply=lambda m, i: f"о{i}")
    column = AgentSpec(
        label="колонка",
        model="stub/m",
        messages=[
            {"role": "system", "content": "СИС"},
            {"role": "user", "content": "ЗАДАЧА"},
        ],
    )
    agent = REGISTRY.create(column)
    # Два ручных обмена до «Старта»: вводная обязана ехать в обоих.
    asyncio.run(_drain(agent.ask("первый вопрос")))
    asyncio.run(_drain(agent.ask("второй вопрос")))
    before = [[m["content"] for m in c["messages"]] for c in _stub.CALLS]
    assert before[0] == ["СИС", "ЗАДАЧА", "первый вопрос"], before[0]
    assert before[1] == ["СИС", "ЗАДАЧА", "первый вопрос", "о0", "второй вопрос"], before[1]
    assert agent.seed_used is False

    # «Старт»: вопрос коммитится в историю обычным ходом.
    _stub.reset()
    asyncio.run(_drain(agent.ask()))
    assert agent.seed_used is True
    assert REGISTRY.store.load_session(agent.id)["seed_used"] is True, "флаг обязан пережить рестарт"

    _stub.reset()
    asyncio.run(_drain(agent.ask("после старта")))
    after = [m["content"] for m in _stub.CALLS[0]["messages"]]
    assert after.count("ЗАДАЧА") == 1, f"вводная задвоилась: {after}"
    return "до «Старта» вводная в каждом промпте, после — ровно один раз, историей"


CHECKS = [
    check_spawn_100,
    check_memory,
    check_blank_forgets_seed,
    check_seed_split_keeps_run,
    check_override_survives_start,
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
    check_reservation_released_on_early_abort,
    check_orphan_run_cleanup,
    check_spec_deep_copy,
    check_eviction_cascade,
    check_eviction_skips_busy,
    check_limits,
    check_judge_remembers_verdict,
    check_shared_client,
    check_session_alias,
    check_cli,
    check_day,
    # --- День 7 ---
    check_store_reopen,
    check_restart_process,
    check_restore_in_constructor,
    check_session_isolation,
    check_seq_renumbered,
    check_partial_persisted,
    check_eviction_keeps_session,
    check_run_sessions_persist,
    check_parallel_writes,
    check_no_key_in_db,
    check_gitignore_db,
    check_sessions_api,
    check_cli_session,
    check_seed_until_started,
    # --- по итогам ревью ---
    check_two_processes,
    check_id_claimed_in_store,
    check_detached_does_not_clobber,
    check_waiting_column_is_busy,
    check_context_length_restored,
    check_sessions_effective_window,
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
