"""Ядро проверок Дней 6, 7 и 8 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Каждая проверка стережёт одно обещание продукта: пункт задания одного из трёх
дней или сквозное свойство. Отдельные скрипты (`spawn_100.py`, `restart.py`,
`two_processes.py`) запускаются отсюда же.
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
    return "ответ напечатан, история записана, агент в реестре"


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
        payload = call["payload"]
        provider = payload.get("provider")
        assert provider and provider.get("require_parameters") is True, (
            f"вызов ушёл без provider.require_parameters: {provider!r}"
        )
        assert {"id": "context-compression", "enabled": False} in (payload.get("plugins") or []), (
            f"вызов ушёл со сжатием контекста на усмотрение провайдера: {payload.get('plugins')!r}"
        )
        assert payload.get("usage") == {"include": True}, (
            f"вызов ушёл без просьбы о usage: {payload.get('usage')!r}"
        )
    last = _stub.CALLS[-1]["payload"]
    assert last["provider"]["order"] == ["openai"], last["provider"]
    assert last["plugins"][-1] == {"id": "web"}, last["plugins"]

    # И то же самое напрямую, без веб-слоя: правила живут в build_payload,
    # а не в ручке, поэтому CLI и любой другой вызывающий получают их тоже.
    from app.llm import build_payload

    payload = build_payload(AgentSpec(label="без веба", model="stub/m"))
    assert payload["provider"]["require_parameters"] is True, payload["provider"]
    assert payload["plugins"] == [{"id": "context-compression", "enabled": False}], payload
    assert payload["usage"] == {"include": True}, payload
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
    saved_client, saved_key = llm.shared_client, llm.api_key
    llm.shared_client = lambda: _FakeClient(_FakeResponse(lines, gap_after=gap))
    llm.api_key = lambda: "sk-or-проверочный"
    try:
        spec = AgentSpec(label="разбор", model="stub/thinking")
        events = asyncio.run(drain(llm.stream_completion(spec, prompt_override=[], **kwargs)))
    finally:
        llm.shared_client, llm.api_key = saved_client, saved_key
    return next(e for e in events if e["type"] == "done")["metrics"]


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
        agent_id = new_agent(client)
        for i in range(3):
            client.post(f"/api/agents/{agent_id}/messages", json={"text": f"вопрос {i}"})
        body = client.get(f"/api/agents/{agent_id}").json()

    total = body["usage_total"]
    assert total is not None, "сводки нет вовсе"
    assert total["prompt_tokens"] == 11 + 120 + 1300, total
    assert total["completion_tokens"] == 5 + 40 + 7, total
    assert total["total_tokens"] == 16 + 160 + 1307, total
    assert round(total["cost_usd"], 8) == round(0.000011 + 0.000120 + 0.001300, 8), total
    assert body["exchanges"] == 3, body["exchanges"]

    # Сумма — не пересказ последнего обмена: слагаемые лежат в стенограмме,
    # и клиент рисует по ним строку под каждым ответом.
    answers = [t for t in body["transcript"] if t["role"] == "assistant"]
    assert [a["metrics"]["total_tokens"] for a in answers] == [16, 160, 1307], answers

    # Перегенерация обмен заменяет, а не добавляет: сумма не удваивается.
    _stub.install(reply="снова", usage=lambda i: _usage(1000, 100, 1100, 0.001))
    with TestClient(main.app) as client:
        client.post(f"/api/agents/{agent_id}/regenerate")
        after = client.get(f"/api/agents/{agent_id}").json()
    assert after["exchanges"] == 3, after["exchanges"]
    assert after["usage_total"]["total_tokens"] == 16 + 160 + 1100, after["usage_total"]
    return (
        f"вход {total['prompt_tokens']}, выход {total['completion_tokens']}, "
        f"всего {total['total_tokens']} за {body['exchanges']} обмена — сумма сошлась"
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
    # Сумм нет, а разговор был: плитка «Сообщений» считает ответы, а не слагаемые.
    assert quiet.as_dict()["exchanges"] == 2, quiet.as_dict()["exchanges"]

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
    assert mixed.exchanges() == 3, mixed.exchanges()

    # Вопросы пользователя в сумму не идут, даже если метрики к ним прицепили.
    sneaky = Agent(spec)
    sneaky.remember("user", "вопрос", metrics=_usage(999, 999, 999, 9.0))
    assert sneaky.usage_summary() is None, sneaky.usage_summary()
    return "ответ без чисел пропущен, чат без чисел даёт None, вопросы не считаются"


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
    store.close()

    again = Store(path).init()
    try:
        revived = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=again)
        after = revived.usage_summary()
    finally:
        again.close()

    assert before == after, (before, after)
    assert after["total_tokens"] == 420, after
    assert revived.exchanges() == 2, revived.exchanges()
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
    assert [r[2] for r in store.message_rows(first)] == ["меня зовут Нина", "ответ0"]
    assert [r[2] for r in store.message_rows(second)] == ["как меня зовут?", "ответ0"]

    # Схема не даёт записать реплику без сессии: ключ составной, и это
    # единственная защита от «все чаты в одной ленте» после перезапуска.
    keys = [row[1] for row in store.conn.execute("PRAGMA table_info(messages)") if row[5]]
    assert keys == ["session_id", "seq"], keys
    indexes = {row[1] for row in store.conn.execute("PRAGMA index_list(messages)")}
    assert "messages_by_session" in indexes, indexes
    return "две сессии — две ленты; PK (session_id, seq), индекс по session_id есть"


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
