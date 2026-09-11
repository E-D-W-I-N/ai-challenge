"""Все проверки Дня 6 — без сети, без ключа, без живых вызовов к LLM.

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
import subprocess
import sys

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

RESULTS: list[tuple[str, bool, str]] = []

CHECKS: list = []
"""Проверки в порядке объявления. Наполняет декоратор `check`.

Списка руками нет намеренно: он дублировал объявления, и забыть в нём
строчку значило тихо не запустить проверку.
"""


def check(name):
    def wrap(fn):
        def run():
            _stub.reset()
            for agent in REGISTRY.list():
                REGISTRY.kill(agent.id)
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


# --- 1. агент как отдельная сущность: сто штук, реестр, консоль ---------------


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


@check("один процесс: у каждого агента свой конфиг, вытеснение щадит занятых")
def check_process():
    """Сто агентов в одном процессе — это сто **своих** конфигов, общий
    HTTP-клиент и потолок живых, ниже которого реестр не течёт."""
    from app.registry import DEFAULT_MAX_AGENTS, AgentRegistry

    shared = AgentSpec(
        label="общий",
        model="stub/model",
        stop=["\n"],
        response_format={"type": "json_object"},
        extra_body={"provider": {"allow_fallbacks": False}},
    )
    registry = AgentRegistry(max_agents=100)
    first, second = registry.create_many([shared, shared])

    # Все изменяемые поля конфига: их ровно три, и каждое обязано быть своим.
    first.spec.extra_body["provider"]["order"] = ["only-me"]
    first.spec.stop.append("ЕЩЁ")
    first.spec.response_format["type"] = "json_schema"
    assert "order" not in shared.extra_body["provider"], shared.extra_body
    assert "order" not in second.spec.extra_body["provider"], second.spec.extra_body
    assert shared.stop == ["\n"] and second.spec.stop == ["\n"], second.spec.stop
    assert shared.response_format == {"type": "json_object"}, shared.response_format
    assert second.spec.response_format == {"type": "json_object"}, second.spec.response_format

    # Вытеснение: уходят самые старые простаивающие, занятый не уходит никогда.
    registry = AgentRegistry(max_agents=5)
    old = registry.create_many([AgentSpec(label=f"старый {i}", model="stub/m") for i in range(3)])
    fresh = registry.create_many([AgentSpec(label=f"свежий {i}", model="stub/m") for i in range(2)])
    for agent in fresh:
        agent.last_used_at += 100
    registry.create_many([AgentSpec(label=f"новый {i}", model="stub/m") for i in range(3)])
    live = {a.id for a in registry.list()}
    assert len(registry) == 5, len(registry)
    assert not (live & {a.id for a in old}), "старые должны быть вытеснены"
    assert {a.id for a in fresh} <= live, "свежие вытесняться не должны"
    assert registry.evicted == 3, registry.evicted

    busy = AgentRegistry(max_agents=2)
    held = busy.create(AgentSpec(label="занят", model="stub/m"))
    held.reserve()
    busy.create_many([AgentSpec(label=f"н {i}", model="stub/m") for i in range(3)])
    assert held.id in {a.id for a in busy.list()}, "занятого вытеснять нельзя"
    held.release()

    # Потолок читается из окружения, а пачка при спавне ограничена.
    os.environ["AGENT_MAX_LIVE"] = "7"
    try:
        assert AgentRegistry().max_agents == 7, AgentRegistry().max_agents
    finally:
        os.environ.pop("AGENT_MAX_LIVE")
    assert AgentRegistry().max_agents == DEFAULT_MAX_AGENTS

    with TestClient(main.app) as client:
        too_many = client.post(
            "/api/agents",
            json={"agents": [{"model": "stub/m"} for _ in range(main.MAX_SPAWN_BATCH + 1)]},
        )
        assert too_many.status_code == 400, too_many.status_code
        assert str(main.MAX_SPAWN_BATCH) in too_many.json()["detail"], too_many.text
        assert client.post("/api/agents", json={"agents": [{"model": "stub/m"}] * 3}).status_code == 200

    # Клиент и семафор общие на процесс: иначе сотня агентов — это сотня пулов
    # соединений и сотня одновременных запросов к OpenRouter.
    import app.llm as llm

    async def scenario():
        first_client = llm.shared_client()
        assert first_client is llm.shared_client(), "клиент должен быть один на процесс"
        assert llm.call_slots() is llm.call_slots()
        await llm.aclose()
        assert llm.shared_client() is not first_client, "после закрытия создаётся новый"
        await llm.aclose()

    asyncio.run(scenario())
    os.environ["LLM_MAX_CONCURRENCY"] = "3"
    try:
        assert llm.max_concurrency() == 3, llm.max_concurrency()
    finally:
        os.environ.pop("LLM_MAX_CONCURRENCY")
    assert llm.max_concurrency() == llm.DEFAULT_MAX_CONCURRENCY
    return f"конфиг у каждого свой, вытеснено 3, пачка больше {main.MAX_SPAWN_BATCH} → 400, клиент один"


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
    assert "привет из консоли" in out.getvalue(), out.getvalue()
    assert [t.role for t in agent.history] == ["user", "assistant"], agent.history
    assert agent.id in {a.id for a in REGISTRY.list()}, "CLI-агент виден в реестре процесса"

    # Команды диалога печатают то, что у агента есть: /история и /агенты.
    import contextlib

    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        cli._print_history(agent)
        cli._print_agents()
    shown = printed.getvalue()
    assert "как дела?" in shown and "привет из консоли" in shown, shown
    assert agent.id in shown and agent.spec.label in shown, shown
    return "ответ напечатан, история записана и показана, агент в реестре"


@check("список чатов: пустой старт, порядок создания, имена, переименование, удаление")
def check_chat_list():
    """Порядок списка слева не стерёг никто: развернул сортировку реестра
    наоборот — всё оставалось зелёным, хотя список слева перевернулся бы.

    Клиентская половина — в `checks/browser_check.js`: кнопка «Новый чат»
    добавляет строку в конец, переименование и удаление живут в самой строке.
    """
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        # Чаты заводит пользователь: ни заготовок, ни чата «по умолчанию».
        assert client.get("/api/agents").json()["agents"] == []

        made = [client.post("/api/agents", json={}).json()["agents"][0] for _ in range(4)]
        # Кнопка «Новый чат» отдаёт чистый чат: промпт пишет пользователь,
        # и пустой `system` обязан пропасть из тела запроса целиком, а не
        # уехать в модель пустым системным сообщением.
        assert all(a["system"] == "" for a in made), [a["system"] for a in made]

        listed = client.get("/api/agents").json()["agents"]
        assert [a["id"] for a in listed] == [a["id"] for a in made], (
            [a["label"] for a in listed], [a["label"] for a in made]
        )

        # Имена по умолчанию нумеруются и не повторяются.
        for agent in made:
            assert re.fullmatch(r"Новый чат \d+", agent["label"]), agent["label"]
        numbers = [int(a["label"].split()[-1]) for a in made]
        assert numbers == sorted(numbers) and len(set(numbers)) == 4, numbers

        # Удаление из середины порядок остальных не трогает, а номер
        # удалённого второй раз не выдаётся.
        client.delete(f"/api/agents/{made[1]['id']}")
        after = [a["id"] for a in client.get("/api/agents").json()["agents"]]
        assert after == [made[0]["id"], made[2]["id"], made[3]["id"]], after
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        assert int(fresh["label"].split()[-1]) > numbers[-1], fresh["label"]

        # Разговор в старом чате не поднимает его наверх: список не по свежести,
        # а первое сообщение не переименовывает чат — автоимени нет.
        client.post(f"/api/agents/{made[0]['id']}/messages", json={"text": "расскажи про кэш"})
        assert not any(m["role"] == "system" for m in _stub.CALLS[-1]["messages"]), _stub.CALLS[-1]
        talked = [a["id"] for a in client.get("/api/agents").json()["agents"]]
        assert talked == after + [fresh["id"]], talked
        assert client.get(f"/api/agents/{made[0]['id']}").json()["label"] == made[0]["label"]

        # Переименование и удаление: то, что делают кнопки в строке списка.
        renamed = client.patch(f"/api/agents/{made[0]['id']}", json={"label": "Стало"})
        assert renamed.status_code == 200 and renamed.json()["label"] == "Стало"
        assert client.patch(f"/api/agents/{made[0]['id']}", json={"label": "  "}).status_code == 400
        for agent in client.get("/api/agents").json()["agents"]:
            assert client.delete(f"/api/agents/{agent['id']}").status_code == 200
        assert client.get("/api/agents").json()["agents"] == []
        assert client.get(f"/api/agents/{made[0]['id']}").status_code == 404
    return "порядок создания, нумерованные имена, переименование, удаление до пустого списка"


# --- 2. память агента ---------------------------------------------------------


@check("в модель уезжает вся история, а клиент шлёт только текст")
def check_whole_history():
    """На месте выпиленного окна памяти: стеречь надо обратное — что в промпт
    попадают **все** реплики и что рост истории ничего из неё не выбрасывает.
    Пороги взяты заведомо выше прежних отсечек: 20 сообщений в окне
    по умолчанию и 400 хранимых.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")

    turns = 25
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИС")
        for i in range(turns):
            response = client.post(
                f"/api/agents/{agent_id}/messages", json={"text": f"вопрос {i}"}
            )
            assert response.status_code == 200, response.text

        # Ленту клиент не шлёт: историю хранит агент, в теле только текст.
        bad = client.post(
            f"/api/agents/{agent_id}/messages",
            json={"text": "привет", "messages": [{"role": "user", "content": "привет"}]},
        )
        assert bad.status_code == 400, bad.text
        assert "только text" in bad.json()["detail"], bad.text
        assert client.post(f"/api/agents/{agent_id}/messages", json={"text": " "}).status_code == 400

    sent = _stub.CALLS[-1]["messages"]
    # Системный промпт + вся переписка (по две реплики на обмен) + новый вопрос.
    assert [m["role"] for m in sent] == (
        ["system"] + ["user", "assistant"] * (turns - 1) + ["user"]
    ), [m["role"] for m in sent]
    assert sent[1]["content"] == "вопрос 0", sent[1]
    assert sent[-1]["content"] == f"вопрос {turns - 1}", sent[-1]
    assert [m["content"] for m in sent[1:-1:2]] == [f"вопрос {i}" for i in range(turns - 1)]
    assert len(REGISTRY.require(agent_id).history) == 2 * turns

    # Рост истории: 500 реплик — больше прежнего потолка хранимого.
    long_chat = agent_module.Agent(AgentSpec(label="длинный", model="stub/model", system="СИС"))
    for i in range(500):
        long_chat.remember("user", f"реплика {i}")
    prompt = long_chat.build_prompt("последний вопрос", spec=long_chat.spec)
    assert len(prompt) == 502, len(prompt)
    assert prompt[1]["content"] == "реплика 0", prompt[1]
    return f"{len(sent)} сообщений в промпте после {turns} обменов, 500 реплик хранятся целиком"


@check("два параллельных обмена с агентом: 409, история не перемешана")
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


@check("история пишется по итогу обмена: провала в ней нет, «Стоп» помечен")
def check_history_commit():
    """Ответа не случилось вовсе — не записано ничего, вопрос возвращается
    клиенту. Оборванный ответ — другое дело: он уже оплачен, и следующий
    вопрос должен видеть, чем кончилось, поэтому записан и помечен.

    Кнопку «Стоп» не стерёг никто: ручка cancel, флаг `cancelled` и текст
    «генерация отменена» выкидывались целиком, не уронив ни одной проверки.
    """
    _stub.install(fail=True)

    async def failed():
        agent = REGISTRY.create(AgentSpec(label="падение", model="stub/model"))
        return agent, await drain(agent.ask("вопрос, который не доедет"))

    agent, events = asyncio.run(failed())
    done = [e for e in events if e["type"] == "done"][0]
    assert agent.history == [], agent.history
    assert done["committed"] is False, done
    assert done["question"] == "вопрос, который не доедет", done

    _stub.install(reply="а" * 200, chunks=20, delay=0.02)

    async def stopped():
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=main.app)
        async with AsyncClient(transport=transport, base_url="http://bench") as client:
            created = await client.post(
                "/api/agents", json={"agent": {"model": "stub/model", "label": "стоп"}}
            )
            agent_id = created.json()["agents"][0]["id"]
            talking = asyncio.create_task(
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос"})
            )
            await asyncio.sleep(0.1)
            cancelled = await client.post(f"/api/agents/{agent_id}/cancel")
            return cancelled, await talking, REGISTRY.require(agent_id)

    cancelled, response, agent = asyncio.run(stopped())
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["was_busy"] is True, cancelled.text

    done = next(e for e in sse(response.text) if e["event"] == "done")
    assert done["cancelled"] is True, done
    assert done["error"] == "генерация отменена", done

    roles = [t.role for t in agent.history]
    assert roles == ["user", "assistant"], roles
    answer = agent.history[-1]
    assert answer.error == "генерация отменена", answer.error
    assert 0 < len(answer.content) < 200, len(answer.content)
    return f"провал не записан, «Стоп» оборвал на {len(answer.content)} символах и пометил ответ"


@check("обрыв клиента гасит вызов, снимает бронь и возвращает снятую пару")
def check_disconnect():
    """`on_close` висит на потоке, а не внутри обмена: отвались клиент
    до первого события — задача с генератором отменяется, не начав
    выполняться, и `finally` внутри неё не сработает никогда. Там же висит
    возврат пары, снятой перегенерацией: иначе вопрос пропал бы вместе
    с прошлым ответом, и восстановить его было бы неоткуда.
    """
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

    # То же место у перегенерации: пара снята до вызова, клиента уже нет.
    _stub.reset()
    _stub.install(reply="новый ответ", chunks=10, delay=0.02)

    async def regenerating():
        agent = REGISTRY.create(AgentSpec(label="обрыв", model="stub/model"))
        agent.remember("user", "мой вопрос")
        agent.remember("assistant", "живой ответ")
        agent.reserve()
        taken = agent.take_last_exchange()
        assert taken is not None and agent.history == [], "пара обязана сняться до вызова"
        frames = [
            frame
            async for frame in main._pump(
                lambda: main._regenerate_events(agent, taken),
                Gone(0),
                # Ровно то, что вешает на поток сама ручка перегенерации.
                main._restore_and_release(agent, taken),
            )
        ]
        await asyncio.sleep(0.05)
        return agent, frames

    agent, regenerated = asyncio.run(regenerating())
    assert regenerated == [], regenerated
    assert [(t.role, t.content) for t in agent.history] == [
        ("user", "мой вопрос"), ("assistant", "живой ответ")
    ], agent.history
    assert agent.busy is False, "бронь должна сниматься и здесь"
    assert not _stub.CALLS, "до модели дело дойти не должно было"
    return f"поток закрыт после {len(frames)} кадров, бронь снята, снятая пара на месте"


@check("перегенерация заменяет последний ответ, а провал возвращает пару")
def check_regenerate():
    """Пара «вопрос — ответ» снимается с истории до вызова: модель видит тот же
    контекст, в ленте остаётся один ответ. Вызов упал, не отдав ни токена, —
    снятое возвращается на место."""
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

        empty = new_agent(client)
        assert client.post(f"/api/agents/{empty}/regenerate").status_code == 409

        # Вызов падает целиком, не отдав ни одного токена, — как HTTP 402.
        _stub.install(fail=True)
        response = client.post(f"/api/agents/{agent_id}/regenerate")
        assert response.status_code == 200, response.text
        events = sse(response.text)
        restored = client.get(f"/api/agents/{agent_id}").json()["transcript"]

    done = next(e for e in events if e["event"] == "done")
    assert done["committed"] is False and done["restored"] is True, done
    assert [(t["role"], t["content"]) for t in restored] == [
        (t["role"], t["content"]) for t in after
    ], restored

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
    return "ответ заменён, провал вернул пару, частичный ответ её заменил"


# --- 3. конфиг: панель, слепок, тело запроса ----------------------------------


@check("обмен идёт на одном конфиге: слепок в начале, промпт из конфига каждый раз")
def check_config_snapshot():
    """Конфиг читается в двух точках: промпт собирает `build_prompt`, тело —
    `build_payload`, и между ними стоит `yield` события `start`. Правка,
    попавшая в это окно, дала бы смешанный запрос — новую модель со старым
    системным промптом. Шагаем генератор руками: `__anext__` останавливает
    его ровно в окне, и правим конфиг оттуда.
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
    assert start["resolved_messages"] == call["messages"], start["resolved_messages"]

    # А следующий обмен идёт уже целиком на новом конфиге: промпт не
    # «применяется однажды», он читается из конфига каждый раз.
    _stub.reset()
    _stub.install(reply="ок")
    asyncio.run(drain(agent.ask("второй")))
    call = _stub.CALLS[0]
    assert call["payload"]["model"] == "новая/модель", call["payload"]["model"]
    assert [m["content"] for m in call["messages"] if m["role"] == "system"] == ["НОВЫЙ ПРОМПТ"]
    assert call["payload"]["temperature"] == 0.9, call["payload"]

    # Снятый промпт исчезает из тела целиком: пустая строка в роли `system` —
    # это не «промпта нет», это заданный пустой промпт.
    _stub.reset()
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="ИСХОДНЫЙ")
        client.patch(f"/api/agents/{agent_id}", json={"system": None})
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"})
        assert not any(m["role"] == "system" for m in _stub.CALLS[-1]["messages"]), _stub.CALLS[-1]

    # Править можно и во время генерации: живой конфиг обмен после слепка
    # не читает, поэтому текущий ответ правка исказить не может.

    async def scenario():
        from httpx import ASGITransport, AsyncClient

        _stub.reset()
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
    return "правка ни в окне сборки, ни во время генерации не смешала конфиги"


# Каждое поле панели вместе с тем, во что оно должно превратиться в теле
# запроса. `system` едет не в теле, а сообщением, и проверяется отдельно.
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


@check("тело вызова: панель доезжает вся, незаданное не уходит, ручки разбирают одинаково")
def check_payload():
    """`require_parameters` стерёг только текст предупреждения в панели, а не
    тело запроса: убери его из `build_payload` — и ни одна проверка не краснела,
    хотя на нём держится весь смысл панели. Без него OpenRouter вправе увести
    запрос к провайдеру, который молча проигнорирует temperature или stop.

    Незаданный параметр не отправляется вовсе — ни как null, ни как ноль:
    отправить 0 вместо «не отправлять» это другой запрос, а с
    provider.require_parameters ещё и другой список провайдеров.
    """
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        bare = new_agent(client, system="СТАРЫЙ ПРОМПТ")
        client.post(f"/api/agents/{bare}/messages", json={"text": "первый"})
        empty_payload = _stub.CALLS[-1]["payload"]
        for name in (*SAMPLING_FIELDS, "stop", "response_format"):
            assert name not in empty_payload, f"{name} уехал в тело, хотя задан не был"
        assert empty_payload["usage"] == {"include": True}, empty_payload
        assert empty_payload["stream"] is True, empty_payload

        # Правка панели применяется к следующему сообщению, а не к следующему
        # чату: смотрим не на ответ ручки, а на то, что ушло в модель.
        patched = client.patch(
            f"/api/agents/{bare}", json={"system": "НОВЫЙ ПРОМПТ", **PANEL_FIELDS}
        )
        assert patched.status_code == 200, patched.text
        client.post(f"/api/agents/{bare}/messages", json={"text": "второй"})
        call = _stub.CALLS[-1]
        sent, payload = call["messages"], call["payload"]
        assert sent[0] == {"role": "system", "content": "НОВЫЙ ПРОМПТ"}, sent[0]
        assert call["model"] == "новая/модель", call["model"]
        for name, value in PANEL_FIELDS.items():
            if name != "model":
                assert payload.get(name) == value, (name, payload.get(name), value)
        # Правка панели меняет конфиг, а не переписку.
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"], sent
        assert sent[1]["content"] == "первый", sent[1]

        # Присланный null снимает параметр: он перестаёт уходить вовсе.
        client.patch(f"/api/agents/{bare}", json={"top_k": None, "stop": None, "response_format": None})
        client.post(f"/api/agents/{bare}/messages", json={"text": "третий"})
        payload = _stub.CALLS[-1]["payload"]
        for name in ("top_k", "stop", "response_format"):
            assert name not in payload, payload
        assert payload["top_p"] == 0.11, payload

        # Ноль — это заданный ноль, а не «не задано».
        zero = new_agent(client, presence_penalty=0.0, temperature=0.0)
        client.post(f"/api/agents/{zero}/messages", json={"text": "?"})
        payload = _stub.CALLS[-1]["payload"]
        assert payload["presence_penalty"] == 0.0 and payload["temperature"] == 0.0, payload

        # Закреплённый поставщик дополняет provider, а не затирает его:
        # extra_body мержится поверх, и require_parameters обязан уцелеть.
        pinned = new_agent(client, extra_body={"provider": {"order": ["openai"]}})
        client.post(f"/api/agents/{pinned}/messages", json={"text": "?"})
        client.post(f"/api/agents/{pinned}/regenerate")
        assert _stub.CALLS[-1]["payload"]["provider"]["order"] == ["openai"], _stub.CALLS[-1]

    for call in _stub.CALLS:
        provider = call["payload"].get("provider")
        assert provider and provider.get("require_parameters") is True, (
            f"вызов ушёл без provider.require_parameters: {call['payload'].get('provider')!r}"
        )

    # И то же самое без веб-слоя: правило живёт в build_payload, а не в ручке.
    from app.llm import build_payload

    payload = build_payload(AgentSpec(label="без веба", model="stub/m"), [])
    assert payload["provider"]["require_parameters"] is True, payload["provider"]
    assert payload["usage"] == {"include": True}, payload
    through_handles = len(_stub.CALLS)

    # Разбирать конфиг две ручки обязаны одинаково. Дефект аудита: POST
    # сохранял стоп-строки ["", "  ", "КОНЕЦ"] как есть, а PATCH выбрасывал
    # пустые. Пустая стоп-строка не косметика — она остановила бы генерацию
    # сразу, а с provider.require_parameters ещё и сузила бы список
    # провайдеров. Проверяется не «PATCH чистит», а согласие двух ручек:
    # любое поле, заданное при создании и той же правкой, даёт один конфиг.
    cases = [
        {"stop": ["", "  ", "КОНЕЦ", " СТОП "]},
        {"stop": ["", "   "]},
        {"system": None},
        {"response_format": {"type": "json_object"}},
        {"temperature": 0.0, "top_p": 0, "max_tokens": 7},
    ]
    watched = ("stop", "system", "response_format", *SAMPLING_FIELDS)
    with TestClient(main.app) as client:
        for case in cases:
            created = client.post("/api/agents", json={"agent": {"model": "stub/model", **case}})
            assert created.status_code == 200, created.text
            born = created.json()["agents"][0]

            patched = client.patch(f"/api/agents/{new_agent(client)}", json=case)
            assert patched.status_code == 200, patched.text
            grown = patched.json()

            for field in watched:
                assert born[field] == grown[field], (
                    f"{case}: поле {field} после создания {born[field]!r}, "
                    f"после правки {grown[field]!r} — ручки разбирают его по-разному"
                )

        # Согласие в отказах тоже: кривой тип обе ручки обязаны отвергнуть.
        # `true` — не число: в Python True это int, и без отдельной проверки
        # «temperature: true» уехало бы к провайдеру единицей.
        for bad in (
            {"stop": "СТОП"},
            {"top_k": 0.5},
            {"max_tokens": 0},
            {"temperature": True},
            {"response_format": "json"},
        ):
            born = client.post("/api/agents", json={"agent": {"model": "stub/m", **bad}})
            grown = client.patch(f"/api/agents/{new_agent(client)}", json=bad)
            assert born.status_code == 400 and grown.status_code == 400, (
                bad, born.status_code, grown.status_code
            )
        # Чужое поле панель не правит вовсе.
        assert client.patch(f"/api/agents/{new_agent(client)}", json={"note": "х"}).status_code == 400

    # И то, ради чего чистка нужна: пустая стоп-строка не уезжает в модель.
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, stop=["", "  ", "КОНЕЦ"])
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"})
        assert _stub.CALLS[-1]["payload"]["stop"] == ["КОНЕЦ"], _stub.CALLS[-1]["payload"]
    return (
        f"{through_handles} вызовов с provider и usage, "
        f"{len(cases)} конфигов и 5 отказов: создание и правка сходятся"
    )


# --- 4. лента, метрики, ключ --------------------------------------------------


@check("стенограмма — ровно диалог, и рассуждение в ответ не входит")
def check_transcript():
    """Клиент рисует ленту **из ответа ручки**: после каждого обмена он
    перечитывает агента и перерисовывает всё заново. Значит контракт
    стенограммы — часть поведения: ровно реплики диалога, по одной на ход,
    и у каждой поля, которые клиент читает.

    Клиентская половина — в `checks/browser_check.js`, блоком «лента рисуется
    из стенограммы»: по реплике на узел, текст, имя модели, провайдер,
    рассуждение, ошибка.
    """
    _stub.install(reply="итоговый ответ", reasoning="я подумал вот так")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИСТЕМА")
        events = sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос"}).text)
        body = client.get(f"/api/agents/{agent_id}").json()

    transcript = body["transcript"]
    # Ровно диалог: системный промпт — это конфиг, а не реплика разговора,
    # и в ленте ему делать нечего. Он виден в панели, полем `system`.
    assert [t["role"] for t in transcript] == ["user", "assistant"], transcript
    assert body["system"] == "СИСТЕМА", body["system"]
    assert transcript[0]["content"] == "вопрос", transcript[0]
    # Поля, которые читает клиент. Убрать любое молча нельзя: карточка
    # перестанет показывать то, что показывала, а ошибку — вовсе проглотит.
    for turn in transcript:
        for field in ("role", "content", "error", "reasoning", "metrics"):
            assert field in turn, f"в реплике нет поля {field}: {turn}"
    answer = transcript[1]
    assert answer["content"] == "итоговый ответ", answer
    assert answer["reasoning"] == "я подумал вот так", answer
    assert answer["error"] is None, answer
    assert answer["metrics"] and answer["metrics"]["provider"] == "stub", answer["metrics"]
    # Длина стенограммы и history_len — про одно и то же: клиент по второму
    # обновляет строку списка, не перечитывая ленту.
    assert body["history_len"] == len(transcript), (body["history_len"], len(transcript))

    # Рассуждение приезжает отдельным событием и в ответ не входит.
    thought = next(e for e in events if e["event"] == "reasoning")
    assert thought["text"] == "я подумал вот так", thought
    done = next(e for e in events if e["event"] == "done")
    assert done["reasoning"] == "я подумал вот так" and done["text"] == "итоговый ответ", done

    # `ttft_ms` стоит на первом токене **ответа**: на думающей модели это
    # момент, когда она додумала. Первый токен вообще считается отдельно —
    # его и показывает плитка.
    metrics = done["metrics"]
    assert metrics["first_token_ms"] is not None, metrics
    assert metrics["first_token_ms"] <= metrics["ttft_ms"], metrics

    # Обратно в модель рассуждение не уходит: в контексте только ответ.
    _stub.reset()
    _stub.install(reply="второй ответ")
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "ещё"})
    sent = " ".join(m["content"] for m in _stub.CALLS[0]["messages"])
    assert "я подумал вот так" not in sent, sent
    assert "итоговый ответ" in sent, sent

    # Оборванный ответ помечен ошибкой, и она доезжает до стенограммы.
    with TestClient(main.app) as client:
        broken = new_agent(client)
        REGISTRY.require(broken).remember("assistant", "огрыз", error="оборвалось")
        failed = client.get(f"/api/agents/{broken}").json()["transcript"][-1]
    assert failed["error"] == "оборвалось", failed
    return f"{len(transcript)} реплики, поля на месте, рассуждение отдельно, ошибка доезжает"


@check("ключа нет наружу, без ключа вызова не будет, окружение сильнее .env")
def check_key():
    """Стенд без .env — обычное состояние свежего клона, и «ключ есть»
    отличается от «стенда нет» ровно файлом app/config.py. Настоящий .env
    не трогаем: читаем из временного каталога."""
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

    # Без ключа сообщение не уходит вовсе: 503 вместо вызова к модели.
    _stub.install(reply="ок")
    saved_has_key = main.has_key
    main.has_key = lambda: False
    try:
        with TestClient(main.app) as client:
            agent_id = new_agent(client)
            blocked = client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"})
            repeated = client.post(f"/api/agents/{agent_id}/regenerate")
            listing = client.get("/api/agents").json()
            agent = REGISTRY.require(agent_id)
    finally:
        main.has_key = saved_has_key
    assert blocked.status_code == 503 and repeated.status_code == 503, blocked.text
    assert listing["has_key"] is False, listing
    assert not _stub.CALLS, f"до модели дошло {len(_stub.CALLS)} вызовов"
    assert agent.busy is False, "бронь не должна залипнуть на отказе"

    import pathlib
    import tempfile

    names = ("OPENROUTER_API_KEY", "OPENROUTER_SITE_URL", "OPENROUTER_SITE_NAME")
    before = {name: os.environ.get(name) for name in names}
    saved_root = config.ROOT
    try:
        for name in names:
            os.environ.pop(name, None)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ".env"), "w", encoding="utf-8") as handle:
                handle.write(
                    "# комментарий\n\n"
                    'OPENROUTER_API_KEY="из-файла"\n'
                    "OPENROUTER_SITE_URL = http://стенд\n"
                    "мусор без равенства\n"
                )
            config.ROOT = pathlib.Path(tmp)
            config._load_dotenv()
            assert config.api_key() == "из-файла", config.api_key()
            assert os.environ["OPENROUTER_SITE_URL"] == "http://стенд"

            # Переменная окружения сильнее файла: иначе стенд не смог бы
            # подставить свой ключ, не переписав .env пользователя.
            os.environ["OPENROUTER_API_KEY"] = "из-окружения"
            config._load_dotenv()
            assert config.api_key() == "из-окружения", config.api_key()

        headers = config.attribution_headers()
        assert headers == {"HTTP-Referer": "http://стенд"}, headers
        os.environ["OPENROUTER_SITE_NAME"] = "стенд"
        assert config.attribution_headers()["X-Title"] == "стенд"

        # Пустой ключ — это отсутствие ключа, а не ключ из пробелов.
        os.environ["OPENROUTER_API_KEY"] = "   "
        assert config.api_key() is None and config.has_key() is False
    finally:
        config.ROOT = saved_root
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return "в клиенте ни слова, ручки не отдают, без ключа 503, окружение сильнее файла"


@check("каталог отдаёт данные, по которым панель предупреждает о параметрах")
def check_catalog():
    """С `provider.require_parameters=true` параметр, которого модель не
    заявляет, выкашивает провайдеров. Предупредить заранее можно только
    по каталогу — значит данные о моделях он обязан отдавать все."""
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
    assert plain["temperature_capped"] is False and plain["temperature_cap"] is None, plain

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

    free = catalog._normalize(
        {"id": "x/y:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 8000}
    )
    assert free["is_free"] is True, free

    async def catalog_stub():
        return [plain, capped, free]

    _stub.install(reply="ок")
    saved = catalog.fetch_models
    catalog.fetch_models = catalog_stub
    try:
        with TestClient(main.app) as client:
            models = client.get("/api/models").json()["models"]
            assert len(models) == 3, "каталог отдаётся целиком, включая :free"
            assert all("supported_parameters" in m for m in models), models[0]
            assert any(m["temperature_capped"] for m in models), models

            # Длина контекста из каталога доезжает до агента и до метрик —
            # по ней плитка «Контекст» показывает заполнение.
            agent_id = new_agent(client, model=plain["id"])
            assert REGISTRY.require(agent_id).context_length == 128000
            events = sse(
                client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"}).text
            )
            done = next(e for e in events if e["event"] == "done")
            assert done["metrics"]["context_length"] == 128000, done["metrics"]

            client.patch(f"/api/agents/{agent_id}", json={"model": free["id"]})
            assert REGISTRY.require(agent_id).context_length == 8000, "смена модели не обновила"
    finally:
        catalog.fetch_models = saved
    return "три поля на месте, каталог целиком, длина контекста доезжает до метрик"


@check("клиент: ничего из сети, лента прокручивается, поведение проверено вызовами")
def check_client():
    """Греп по исходнику прошёл бы и если экранирование переедет **после**
    разбора — самая опасная поверхность демо была бы прикрыта пустышкой.
    Поэтому клиентский код исполняется под node: payload на входе,
    утверждения про выход.
    """
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

    # Лента — колоночный флексбокс, и её элементы по умолчанию сжимаются:
    # без запрета карточки давятся в полоску вместо прокрутки.
    assert ".feed > * { flex: 0 0 auto; }" in css, "элементы ленты сжимаются"
    feed = css[css.index(".feed {") : css.index(".feed > *")]
    assert "min-height: 0" in feed and "overflow-y: auto" in feed, feed
    assert "scroll-behavior: smooth" not in feed, (
        "плавная прокрутка ленты дёргает её на каждом куске ответа"
    )
    assert ".composer { flex: 0 0 auto" in css, "композер должен быть нерастяжимым"

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
    return f"статика без сети, лента прокручивается, {result.stdout.strip().lower()}"


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
