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

from checks import _daysrc, _stub  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_stub.install_offline()

import app.agent as agent_module  # noqa: E402
import app.main as main  # noqa: E402
import day  # noqa: E402
from app.registry import REGISTRY  # noqa: E402
from app.schema import AgentSpec  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def wrap(fn):
        def run():
            _stub.reset()
            REGISTRY.kill_all()
            main.ensure_roster()
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


# --- 2. агенты дней 1–5 -------------------------------------------------------

# Имя агента ростера → колонка того дня. Порядок в списке значим: он и есть
# соответствие «колонка ↔ агент», по нему идёт сверка.
PAST_DAYS = {
    "origin/day-01": ["День 1 · Ответ"],
    "origin/day-02": [
        "День 2 · A. Свободный ответ (формат)",
        "День 2 · B. + response_format",
        "День 2 · A. Свободный ответ (длина)",
        "День 2 · B. + max_tokens",
        "День 2 · A. Свободный ответ (стоп)",
        "День 2 · B. + stop",
    ],
    "origin/day-03": [
        "День 3 · Прямо (vs пошагово)",
        "День 3 · Пошагово",
        "День 3 · Прямо (промпт себе)",
        "День 3 · Модель пишет промпт",
        "День 3 · Ответ по своему промпту",
        "День 3 · Аналитик данных",
        "День 3 · Курьер-практик",
        "День 3 · Юрист по рекламе",
    ],
    "origin/day-04": ["День 4 · t = 0.0", "День 4 · t = 0.7", "День 4 · t = 1.2"],
    "origin/day-05": [
        "День 5 · Слабая · llama-3.1-8b",
        "День 5 · Средняя · mistral-small-3.2-24b",
        "День 5 · Сильная · gemini-3.1-flash-lite",
    ],
}

TRANSFERRED = ("model", "temperature", "max_tokens", "stop", "response_format", "extra_body")


@check("конфиги дней 1–5 перенесены дословно: сверка с day.py каждой ветки")
def check_past_days_transfer():
    roster = {spec.label: spec for spec in day.AGENTS}
    checked = 0
    for branch, labels in PAST_DAYS.items():
        columns = _daysrc.columns(branch)
        assert len(columns) == len(labels), (
            f"{branch}: колонок {len(columns)}, а имён в переносе {len(labels)} — "
            "колонка потерялась или добавилась лишняя"
        )
        for column, label in zip(columns, labels):
            spec = roster.get(label)
            assert spec is not None, f"в ростере нет агента «{label}»"
            for field in TRANSFERRED:
                assert getattr(spec, field) == getattr(column, field), (
                    f"{label} :: {field}: у нас {getattr(spec, field)!r}, "
                    f"в {branch} {getattr(column, field)!r}"
                )
            system, draft = _daysrc.split_messages(column.messages)
            assert spec.system == system, f"{label} :: системный промпт разошёлся"
            assert spec.draft == draft, f"{label} :: вопрос дня разошёлся"
            checked += 1
    return f"{checked} колонок из пяти веток, по {len(TRANSFERRED) + 2} поля — совпало всё"


@check("агенты дней 1–5 подняты на старте процесса и сгруппированы по дням")
def check_roster_live():
    with TestClient(main.app) as client:
        data = client.get("/api/agents").json()
    live = {a["label"]: a for a in data["agents"]}
    for labels in PAST_DAYS.values():
        for label in labels:
            assert label in live, f"агент «{label}» не поднялся"
            assert live[label]["group"], f"у «{label}» нет группы — он потеряется в списке"
    # Порядок групп задаёт day.py, а не сортировка: «День 10» не должен
    # оказаться между первым и вторым.
    assert data["groups"] == list(dict.fromkeys(s.group for s in day.AGENTS if s.group))
    assert data["groups"][0] == "День 1" and data["groups"][-1] == "День 6", data["groups"]
    return f"{len(live)} агентов, группы: {', '.join(data['groups'])}"


@check("вопрос дня лежит черновиком и сам не отправляется")
def check_draft_not_sent():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        listed = client.get("/api/agents").json()["agents"]
        agent_id = next(a["id"] for a in listed if a["label"] == "День 1 · Ответ")
        full = client.get(f"/api/agents/{agent_id}").json()
        assert full["draft"].startswith("Объясни, почему первый токен"), full["draft"][:60]
        # Открытие агента не делает ни одного вызова к модели.
        assert not _stub.CALLS, f"открытие агента сходило в модель {len(_stub.CALLS)} раз"
        assert [t["role"] for t in full["transcript"]] == ["system"], full["transcript"]

        client.post(f"/api/agents/{agent_id}/messages", json={"text": "свой вопрос"})
    sent = _stub.CALLS[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"], sent
    assert sent[1]["content"] == "свой вопрос", sent[1]
    return "черновик виден в конфиге, в модель уходит только отправленное руками"


# --- 3. сценариев больше нет --------------------------------------------------


@check("сценарии выпилены: ни ручек, ни кода, ни следов в клиенте")
def check_scenarios_gone():
    with TestClient(main.app) as client:
        for path in ("/api/scenarios", "/api/run/0"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/scenarios/0/agents").status_code == 404

    assert not os.path.exists(os.path.join(ROOT, "app", "commands.py")), "app/commands.py жив"
    # app/schema.py в этот список не входит намеренно: там `Session` объясняет
    # в документации, какие поля прошлых дней он отбрасывает, и без слов
    # «repeats» и «depends_on» объяснить это нельзя. Кода сценариев там нет —
    # за этим следит отдельная проверка совместимости `Session`.
    server = read("app/main.py") + read("app/agent.py")
    for word in ("Scenario", "judge", "depends_on", "repeats", "прогон"):
        assert word not in server, f"в серверном коде остался {word}"

    # Родительские связи жили ради субагентов прогона. Спавнить детей больше
    # некому, и держать каскад, достижимый только из проверок, незачем.
    registry = read("app/registry.py")
    for word in ("parent_id", "kill_children", "children"):
        assert word not in registry, f"в реестре остался {word} — его никто не выставляет"
    with TestClient(main.app) as client:
        agent = client.get("/api/agents").json()["agents"][0]
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


@check("панель правит живого агента: модель, промпт, имя, окно памяти")
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
        assert client.patch(f"/api/agents/{agent_id}", json={"group": "День 9"}).status_code == 400

    call = _stub.CALLS[-1]
    assert call["model"] == "stub/new", call["model"]
    assert call["messages"][0]["content"] == "новый промпт", call["messages"][0]
    return "PATCH меняет модель, промпт, имя и окно; group снаружи не правится"


# --- 6. лента: рассуждение и перегенерация ------------------------------------


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

    Сценарий короткий и сам напрашивается: у агента Дня 4 провайдер закреплён
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
            return [(t["role"], t["content"]) for t in body if not t.get("seed")]

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


# --- 7. чаты и ростер ---------------------------------------------------------


@check("«Очистить все чаты» не уносит агентов дней 1–5")
def check_reset_keeps_roster():
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        chat = client.post("/api/agents", json={}).json()["agents"][0]
        client.post(f"/api/agents/{chat['id']}/messages", json={"text": "привет"})
        before = client.get("/api/agents").json()
        assert any(a["id"] == chat["id"] for a in before["agents"])

        reset = client.post("/api/agents/reset").json()
        assert chat["id"] in reset["killed"], reset["killed"]
        after = client.get("/api/agents").json()

    labels = {a["label"] for a in after["agents"]}
    for group_labels in PAST_DAYS.values():
        for label in group_labels:
            assert label in labels, f"«{label}» пропал после очистки"
    assert not [a for a in after["agents"] if not a["group"]], "чаты пользователя должны уйти"
    assert len(after["agents"]) == len(day.AGENTS), (len(after["agents"]), len(day.AGENTS))
    return f"чат удалён, {len(day.AGENTS)} агентов ростера на месте"


@check("«Новый чат» создаётся пустым телом и попадает в чаты, а не в ростер")
def check_new_chat():
    with TestClient(main.app) as client:
        created = client.post("/api/agents", json={})
        assert created.status_code == 200, created.text
        agent = created.json()["agents"][0]
        assert agent["label"] == "Новый чат" and agent["group"] == "", agent
        # Группу извне не подсунуть: иначе чат притворился бы агентом дня
        # и пережил бы «Очистить все чаты».
        sneaky = client.post(
            "/api/agents", json={"agent": {"model": "stub/m", "label": "х", "group": "День 1"}}
        )
        assert sneaky.status_code == 200, sneaky.text
        assert sneaky.json()["agents"][0]["group"] == "", sneaky.json()["agents"][0]
    return "новый чат без группы, подсунуть группу снаружи нельзя"


# --- 8. ключ и сеть -----------------------------------------------------------


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
            agent_id = client.get("/api/agents").json()["agents"][0]["id"]
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
    assert first.spec.messages[0] is not shared.messages[0]
    assert first.spec.stop is not shared.stop

    first.spec.extra_body["provider"]["order"] = ["only-me"]
    first.spec.messages[0]["content"] = "ДРУГОЕ"
    assert "order" not in shared.extra_body["provider"], shared.extra_body
    assert "order" not in second.spec.extra_body["provider"], second.spec.extra_body
    assert shared.messages[0]["content"] == "СИС", shared.messages
    return "правка у одного агента не задела ни день, ни соседа"


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


@check("Session собирает колонку прошлых дней и терпит их поля")
def check_session_compat():
    from app.schema import AgentSpec as Spec
    from app.schema import Session

    # Ровно то, как объявлял колонку day.py Дня 4: с repeats и extra_body.
    spec = Session(
        label="t = 1.2",
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
        temperature=1.2,
        max_tokens=80,
        repeats=5,
        extra_body={"provider": {"order": ["openai"]}},
        note="n",
    )
    assert isinstance(spec, Spec), type(spec)
    assert spec.temperature == 1.2 and spec.max_tokens == 80
    assert spec.extra_body == {"provider": {"order": ["openai"]}}
    assert not hasattr(spec, "repeats"), "серии выпилены, поля быть не должно"

    # И как объявлял колонку Дня 3 — с depends_on.
    dependent = Session(
        label="Ответ по своему промпту",
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "y"}],
        depends_on="Модель пишет промпт",
    )
    assert isinstance(dependent, Spec) and not hasattr(dependent, "depends_on")
    return "repeats и depends_on принимаются и молча отбрасываются"


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


CHECKS = [
    check_spawn_100,
    check_past_days_transfer,
    check_roster_live,
    check_draft_not_sent,
    check_scenarios_gone,
    check_memory,
    check_history_window,
    check_parallel,
    check_rollback,
    check_disconnect,
    check_new_params,
    check_patch_panel,
    check_reasoning,
    check_first_token,
    check_regenerate,
    check_regenerate_failure,
    check_reset_keeps_roster,
    check_new_chat,
    check_no_key_leak,
    check_no_cdn,
    check_browser,
    check_spec_deep_copy,
    check_eviction,
    check_batch_limit,
    check_shared_client,
    check_no_feed,
    check_session_compat,
    check_cli,
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
