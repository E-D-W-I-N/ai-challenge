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


KEEP, EVERY = 6, 10
"""Окно памяти и порог, на которых проверяется сжатие. Порог выше самого
длинного сценария остальных проверок (3 обмена, 6 реплик): опусти его — и
поплывут счётчики вызовов к модели в чужих проверках, где сжатия быть не должно."""


def _is_folding(messages) -> bool:
    """Это вызов на сжатие, а не обмен: у него свой системный промпт."""
    return bool(messages) and messages[0].get("content") == agent_module.COMPRESS_SYSTEM


def _folding_calls() -> list[dict]:
    return [call for call in _stub.CALLS if _is_folding(call["messages"])]


def _folding_aware(messages, index):
    """Ответ заглушки, по которому видно, чем был вызов: сводка узнаётся
    в промпте следующего обмена по слову СВОДКА."""
    if _is_folding(messages):
        return f"СВОДКА {index}"
    return f"ответ {index}"


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


@check("история не теряется молча: без сводки уезжает вся, со сводкой — сводка и хвост")
def check_history_never_silently_cut():
    """Контракт Дня 9 одной фразой: **ни одна реплика не исчезает без замены**.

    Три состояния, и молчаливой обрезки нет ни в одном:

    * окно памяти не задано — в модель уезжает вся история, дословно как в
      Дне 8: ни хвоста, ни отсечки по росту (пороги здесь выше прежних
      отсечек — 20 в окне, 400 хранимых, — вернись любая, станет красно);
    * окно задано, но до порога ещё не дошло — тоже вся: **без сводки история
      не режется**;
    * сводка есть — уезжает сводка и ровно последние N реплик, а свёрнутое
      плюс хвост равно длине истории.

    Сама история при этом полная всегда: сжатие её не трогает, иначе сломалась
    бы перегенерация.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")

    # 1. Живой маршрут без сжатия: 25 обменов — больше прежнего окна.
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
    assert not _folding_calls(), "без окна памяти сжатие не запускается вовсе"

    agent = REGISTRY.require(agent_id)
    assert len(agent.history) == 2 * turns, len(agent.history)
    assert agent.summaries == [], agent.summaries

    # 2. Рост истории: 500 реплик — больше прежнего потолка хранимого.
    long_chat = agent_module.Agent(AgentSpec(label="длинный", model="stub/model", system="СИС"))
    for i in range(500):
        long_chat.remember("user", f"реплика {i}")
    assert len(long_chat.history) == 500, "история подрезана при росте"
    assert long_chat.history[0].content == "реплика 0", "у истории отъели начало"

    prompt = long_chat.build_prompt("последний вопрос")
    assert len(prompt) == 502, len(prompt)
    assert prompt[1]["content"] == "реплика 0", prompt[1]

    # 3. Окно задано, порог ещё не набран — история всё равно уезжает целиком.
    _stub.reset()
    _stub.install(reply=_folding_aware)
    with TestClient(main.app) as client:
        folded_id = new_agent(
            client,
            keep_last=KEEP,
            compress_every=EVERY,
            stop=["СТОП"],
            response_format={"type": "json_object"},
        )
        for i in range(5):
            client.post(f"/api/agents/{folded_id}/messages", json={"text": f"вопрос {i}"})

        early = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in early] == ["user", "assistant"] * 4 + ["user"], early
        assert early[0]["content"] == "вопрос 0", early[0]
        assert not _folding_calls(), "сжатие запустилось до порога"

        # 4. Дошли до порога: 9-й обмен сам уезжает уже сжатым.
        for i in range(5, 9):
            client.post(f"/api/agents/{folded_id}/messages", json={"text": f"вопрос {i}"})

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
        # Главное равенство дня: свёрнутое плюс хвост — вся история на момент
        # сборки промпта. Это 16 реплик восьми обменов: девятый в неё ещё
        # не записан — он как раз и уехал сжатым.
        assert covered + len(tail) == 16 == len(folded.history) - 2, (
            covered, len(tail), len(folded.history)
        )
        # Системного сообщения в голом чате нет и со сжатием тоже.
        assert not any(m["role"] == "system" for m in sent), sent

        # История не тронута: сжатие меняет промпт, а не память чата.
        assert [t.content for t in folded.history[:2]] == ["вопрос 0", "ответ 0"], folded.history[:2]

        # 5. Свёрнутое уехало в сжатие, а не пропало: вызов на сжатие видел
        # ровно те реплики, которых больше нет в промпте.
        folding = _folding_calls()[-1]["messages"]
        assert folding[0]["role"] == "system", folding[0]
        assert "вопрос 0" in folding[1]["content"] and "ответ 4" in folding[1]["content"], folding[1]
        assert "вопрос 5" not in folding[1]["content"], "в сжатие уехал хвост, который остаётся как есть"

        # Формат ответа и стоп-строки на время сжатия сняты: чат с
        # {"type": "json_object"} вернул бы вместо пересказа объект, а
        # стоп-строка оборвала бы пересказ на середине. У самого обмена
        # они на месте — снимаются только у вызова на сжатие.
        folding_body = _folding_calls()[-1]["payload"]
        assert "response_format" not in folding_body, folding_body.get("response_format")
        assert "stop" not in folding_body, folding_body.get("stop")
        assert _stub.CALLS[-1]["payload"]["response_format"] == {"type": "json_object"}
        assert _stub.CALLS[-1]["payload"]["stop"] == ["СТОП"]
        # А модель — та же самая, и это не мелочь: вторая модель развалила бы
        # счёт токенов на две цены, и «экономия» перестала бы быть сравнимой
        # с расходом чата. Снимается из того же `replace` — значит и стеречь
        # его надо целиком, а не по двум полям из трёх.
        assert folding_body["model"] == _stub.CALLS[-1]["payload"]["model"], (
            folding_body["model"],
            _stub.CALLS[-1]["payload"]["model"],
        )

        # Обмен, который уехал сжатым, говорит об этом своими метриками:
        # из них строка под ответом и берёт, сколько реплик уехало сводкой.
        assert folded.history[-1].metrics["summarized"] == 10, folded.history[-1].metrics
        assert folded.history[-3].metrics.get("summarized") is None, "пометка досталась обмену до сжатия"

        # 6. Второе сворачивание идёт инкрементально: прошлая сводка плюс
        # только новое, а не пересказ разговора с начала.
        for i in range(9, 14):
            client.post(f"/api/agents/{folded_id}/messages", json={"text": f"вопрос {i}"})
        assert len(folded.summaries) == 2, folded.summaries
        again = _folding_calls()[-1]["messages"][1]["content"]
        assert "СВОДКА" in again, "прошлая сводка в сжатие не попала — начало разговора потеряно"
        assert "вопрос 0" not in again, "сжатие пересказывает историю с начала заново"
        assert "вопрос 5" in again, again[:200]
        assert folded.summary_cover() == 20, folded.summary_cover()
        assert len(folded.history) == 28, len(folded.history)

        # 7. Обратимость, обе её половины разом. Сжатие выключают — история
        # обязана вернуться в модель **целиком**, а уже накопленные сводки
        # обязаны **уцелеть**: выключенное сжатие ничего не сжимает, но и
        # ничего не выбрасывает. Включают обратно — граница та же, и
        # пересказывать разговор заново не нужно.
        off = client.patch(f"/api/agents/{folded_id}", json={"keep_last": None})
        assert off.status_code == 200, off.text
        client.post(f"/api/agents/{folded_id}/messages", json={"text": "после выключения"})
        back = _stub.CALLS[-1]["messages"]
        assert len(back) == 28 + 1, len(back)
        assert back[0]["content"] == "вопрос 0", back[0]
        assert folded.summary_cover() == 0, folded.summary_cover()
        assert not any("пересказ начала разговора" in m["content"] for m in back), back[0]
        # Сводки не выброшены — их просто перестали подставлять.
        assert len(folded.summaries) == 2, folded.summaries
        assert folded.summaries[-1]["upto"] == 20, folded.summaries[-1]

        client.patch(f"/api/agents/{folded_id}", json={"keep_last": KEEP})
        assert folded.summary_cover() == 20, folded.summary_cover()
        assert len(folded.summaries) == 2, "включение обратно пересобрало сводки заново"

    # 7. Граница сворачивания не рвёт пару: история идёт парами, обе реплики
    # пишутся разом, и свёрнутый вопрос без своего ответа сделал бы хвост
    # бессмысленным. При нечётном окне граница округляется вниз до чётного.
    odd = agent_module.Agent(
        AgentSpec(label="нечёт", model="stub/model", keep_last=5, compress_every=EVERY)
    )
    for i in range(20):
        odd.remember("user" if i % 2 == 0 else "assistant", f"реплика {i}", persist=False)
    asyncio.run(odd.compress(odd.spec))
    odd_cover = odd.summary_cover()
    assert odd_cover == 14, odd_cover
    assert odd_cover % 2 == 0, f"граница разорвала пару: свёрнуто {odd_cover} реплик"
    assert odd.history[odd_cover].role == "user", odd.history[odd_cover].role

    # 8. Перегенерация снимает пару **с конца**, а сводка покрывает начало:
    # на коротком чате с нулевым окном они встречаются, и `upto` оказывается
    # больше истории. Зажатый длиной, он остаётся правдой; незажатый заявил
    # бы, что свёрнуто реплик больше, чем в чате было.
    short = agent_module.Agent(
        AgentSpec(label="перегенерация", model="stub/model", keep_last=0, compress_every=EVERY)
    )
    for i in range(10):
        short.remember("user" if i % 2 == 0 else "assistant", f"реплика {i}", persist=False)
    asyncio.run(short.compress(short.spec))
    assert short.summary_cover() == 10, short.summary_cover()
    assert short.take_last_exchange() is not None, "перегенерация не сняла пару"
    short_cover = short.summary_cover()
    assert short_cover == 8 == len(short.history), (short_cover, len(short.history))
    short_prompt = short.build_prompt("вопрос после перегенерации")
    assert short_cover + (len(short_prompt) - 2) == len(short.history), short_prompt

    # 9. Умолчание — «сжатия нет», и держится это не на честном слове.
    # Кнопка «Новый чат» идёт мимо разбора полей, прямо от умолчаний
    # датакласса (`replace(NEW_CHAT_SPEC, ...)`), и консоль собирает
    # `AgentSpec` руками. Стань окно и порог умолчаниями — сжимали бы разом
    # все новые чаты и вся консоль, а разбор полей об этом и не узнал бы.
    _stub.reset()
    with TestClient(main.app) as client:
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        assert fresh["keep_last"] is None, fresh["keep_last"]
        assert fresh["compress_every"] is None, fresh["compress_every"]
        for i in range(9):
            client.post(f"/api/agents/{fresh['id']}/messages", json={"text": f"вопрос {i}"})
    assert not _folding_calls(), "чат из умолчаний сворачивает историю"
    assert len(_stub.CALLS[-1]["messages"]) == 17, len(_stub.CALLS[-1]["messages"])

    return (
        f"{len(sent)} сообщений в промпте вместо 17 после 8 обменов со сводкой; "
        f"без окна памяти — вся история из {2 * turns} реплик и 500 хранимых целиком"
    )


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
    _stub.install(reply=_folding_aware)
    question = "вопрос {i}: " + "довольно длинный текст вопроса, " * 20

    with TestClient(main.app) as client:
        plain = new_agent(client, label="без сжатия")
        folded = new_agent(client, label="со сжатием", keep_last=KEEP, compress_every=EVERY)
        for i in range(12):
            for agent_id in (plain, folded):
                response = client.post(
                    f"/api/agents/{agent_id}/messages", json={"text": question.format(i=i)}
                )
                assert response.status_code == 200, response.text
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
    covered = agent.summary_cover()
    assert covered == 10, covered
    assert covered + (len(folded_in) - 2) == len(agent.history) - 2, (covered, len(folded_in))
    assert "пересказ начала разговора" in folded_in[0]["content"], folded_in[0]

    # Вызов на сжатие — такой же вызов к модели, и три правила тела на нём
    # тоже. Особенно третье: собери сводку провайдер со включённым
    # `context-compression`, и он молча выбросил бы середину того самого
    # куска, который мы отдали пересказать, — сводка вышла бы дырявой,
    # а узнать об этом было бы неоткуда.
    folding_body = _folding_calls()[-1]["payload"]
    provider = folding_body.get("provider")
    assert provider and provider.get("require_parameters") is True, (
        f"вызов на сжатие ушёл без provider.require_parameters: {provider!r}"
    )
    assert {"id": "context-compression", "enabled": False} in (folding_body.get("plugins") or []), (
        f"сводку собирал провайдер со своим сжатием: {folding_body.get('plugins')!r}"
    )
    assert folding_body.get("usage") == {"include": True}, (
        f"вызов на сжатие ушёл без просьбы о usage — его токены было бы не посчитать: "
        f"{folding_body.get('usage')!r}"
    )

    # Итог по чату — со стоимостью сжатия внутри, и всё равно меньше.
    plain_total = totals[plain]["usage_total"]["prompt_tokens"]
    folded_total = totals[folded]["usage_total"]["prompt_tokens"]
    assert folded_total < plain_total, (folded_total, plain_total)
    assert totals[folded]["exchanges"] == 12, totals[folded]["exchanges"]
    saved = 100 - round(100 * folded_total / plain_total)
    return (
        f"12 обменов: без сжатия {plain_total} входных токенов, со сжатием "
        f"{folded_total} (с вызовом на сжатие внутри) — на {saved}% меньше"
    )


@check("сводка живёт отдельно от истории: переживает перезапуск и не переживает очистку")
def check_summary_survives_restart():
    """Сводка лежит в своей таблице, а не строкой в `messages`: `save_history`
    на **каждой** записи делает `DELETE FROM messages` — лежи сводка там,
    её стирал бы каждый следующий обмен.

    Доказательство — настоящее переоткрытие файла и обмен уже после него:
    сводка та же, история полная, `seq` без дыр, и текста сводки в репликах нет.

    И обратная половина: отдельная таблица не чистится каскадом — внешних
    ключей в схеме нет. Значит, все три пути очистки — `forget()`, удаление
    чата и очистка базы — обязаны уносить сводку сами. Не унесут — id чатов
    выдаются по возрастанию и после очистки начинаются заново, и в свежий
    чат уедет пересказ мёртвого разговора.
    """
    from app.agent import Agent

    _stub.install(reply=_folding_aware)
    path = _temp_db("summary-restart")
    store = Store(path).init()
    spec = AgentSpec(label="сжатый", model="stub/model", keep_last=KEEP, compress_every=EVERY)
    agent = Agent(spec, store=store)
    for i in range(9):
        asyncio.run(drain(agent.ask(f"вопрос {i}")))
    before = [(s["upto"], s["content"]) for s in agent.summaries]
    agent_id = agent.id
    assert len(before) == 1 and before[0][0] == 10, before
    store.close()

    # Настоящее переоткрытие файла, а не тот же объект в памяти.
    again = Store(path).init()
    try:
        revived = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=again)
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

        # Чистка чата уносит и сводку: каскада в схеме нет.
        revived.forget()
        assert again.load_summaries(agent_id) == [], again.load_summaries(agent_id)
        assert again.message_rows(agent_id) == [], again.message_rows(agent_id)

        # И в памяти объекта тоже, а не только в файле. `summary_cover` зажат
        # длиной истории: оставь сводку в памяти — и на первых же репликах
        # нового разговора она снова стала бы действующей, накрыв собой его
        # начало. Поэтому смотрим не на пустую таблицу, а на отросшую заново
        # историю: она обязана уехать в модель целиком.
        for i in range(2):
            asyncio.run(drain(revived.ask(f"новый вопрос {i}")))
        assert revived.summaries == [], revived.summaries
        assert revived.summary_cover() == 0, revived.summary_cover()
        fresh_prompt = revived.build_prompt("ещё")
        assert len(fresh_prompt) == 5, len(fresh_prompt)
        assert fresh_prompt[0]["content"] == "новый вопрос 0", fresh_prompt[0]

        # Удаление чата — второй такой путь. Сводку удалённого чата
        # унаследовал бы чат с тем же id: номера идут по возрастанию,
        # а после очистки базы начинаются заново.
        doomed = Agent(spec, store=again)
        for i in range(9):
            asyncio.run(drain(doomed.ask(f"вопрос {i}")))
        assert again.load_summaries(doomed.id), "сводка не записалась — проверять нечего"
        again.delete_session(doomed.id)
        assert again.load_summaries(doomed.id) == [], (
            "удаление чата оставило его сводку в базе"
        )

        # Очистка базы — третий. `clear()` стирает и счётчик имён, поэтому
        # следующий чат получит тот же id, что и стёртый.
        kept = Agent(spec, store=again)
        for i in range(9):
            asyncio.run(drain(kept.ask(f"вопрос {i}")))
        assert again.load_summaries(kept.id), "сводка не записалась — проверять нечего"
        again.clear()
        assert again.load_summaries(kept.id) == [], "очистка базы оставила сводку"
    finally:
        again.close()
    return f"сводка на {before[0][0]} реплик пережила перезапуск и обмен после него; forget(), удаление чата и очистка базы её уносят"


@check("токены вызова на сжатие попадают в итог по чату")
def check_compression_tokens_counted():
    """Вызов на сжатие тоже уехал в модель и тоже оплачен. Экономия, не
    вычитающая его стоимость, — враньё, поэтому сводка входит в `usage_total`
    отдельным слагаемым: в истории её нет, и сама собой она туда не попадёт.

    Числа сжатия здесь заведомо больше всех остальных вместе взятых: потеряйся
    они, сумма разошлась бы на порядок, а не на округление.
    """
    folding_calls: set = set()

    def reply(messages, index):
        if _is_folding(messages):
            folding_calls.add(index)
            return f"СВОДКА {index}"
        return f"ответ {index}"

    def usage(index):
        if index in folding_calls:
            return _usage(50000, 400, 50400, 0.05)
        return _usage(1, 1, 2, 0.000001)

    _stub.install(reply=reply, usage=usage)
    with TestClient(main.app) as client:
        agent_id = new_agent(client, keep_last=KEEP, compress_every=EVERY)
        for i in range(9):
            client.post(f"/api/agents/{agent_id}/messages", json={"text": f"вопрос {i}"})
        body = client.get(f"/api/agents/{agent_id}").json()

    assert len(folding_calls) == 1, folding_calls
    total = body["usage_total"]
    assert total["prompt_tokens"] == 9 * 1 + 50000, total
    assert total["completion_tokens"] == 9 * 1 + 400, total
    assert total["total_tokens"] == 9 * 2 + 50400, total
    assert round(total["cost_usd"], 8) == round(9 * 0.000001 + 0.05, 8), total
    # Сжатие — не обмен: карточек в ленте от него не прибавилось.
    assert body["exchanges"] == 9, body["exchanges"]
    assert len([t for t in body["transcript"] if t["role"] == "assistant"]) == 9, "сводка попала в ленту"

    # И тот же счёт из файла базы: метрики сжатия лежат в `summaries`.
    agent = REGISTRY.require(agent_id)
    assert agent.summaries[0]["metrics"]["total_tokens"] == 50400, agent.summaries[0]["metrics"]
    return (
        f"итог {total['total_tokens']} токенов = {9 * 2} за девять обменов "
        f"плюс 50400 за сжатие; обменов по-прежнему {body['exchanges']}"
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
    for i in range(3):
        asyncio.run(drain(agent.ask(f"вопрос {i}")))
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
        # Сводку пишет модель, и ключ из реплики попадёт в пересказ так же
        # легко, как в саму реплику: таблица новая, а обещание то же.
        store.save_summaries(
            agent.id,
            [{"upto": 2, "content": f"пересказ, в котором засветился {key}",
              "metrics": {"заметка": key}}],
        )

        # А это — про колонки, которых ещё нет: любая запись идёт через
        # транзакцию, и параметр чистится независимо от того, вспомнил ли
        # автор про redact() в этом конкретном методе.
        with store.tx() as conn:
            conn.execute("UPDATE sessions SET label = ? WHERE id = ?", (key, agent.id))
            conn.execute("INSERT INTO meta (key, value) VALUES ('ловушка', ?)", (key,))

        leaked = []
        for table in ("sessions", "messages", "meta", "summaries"):
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
            conn.executemany(
                "INSERT INTO summaries (session_id, seq, upto, content, at) "
                "VALUES (?, ?, ?, ?, 0)",
                [("s", 0, 2, key)],
            )
        leaked = []
        for table in ("sessions", "messages", "meta", "summaries"):
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
