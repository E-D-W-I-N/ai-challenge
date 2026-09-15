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


def _service_kind(messages) -> str | None:
    """Чем был этот вызов: `summary` — сжатие, `facts` — ведение рабочей
    памяти, `None` — обычный обмен. У каждого служебного вызова свой
    системный промпт, по нему и различаем.

    Раньше здесь был вопрос «это сжатие?» — с рабочей памятью служебных
    вызовов стало два, и вопрос обязан был стать «какой это вызов?»: иначе
    ведение считалось бы обменом и счётчики вызовов поплыли бы в чужих
    проверках, где к модели ходят ровно столько раз, сколько задано.
    """
    if not messages:
        return None
    return {
        agent_module.COMPRESS_SYSTEM: "summary",
        agent_module.WORKING_SYSTEM: "facts",
    }.get(messages[0].get("content"))


def _service_calls(kind: str | None = None) -> list[dict]:
    """Служебные вызовы: все или только одного рода."""
    return [
        call
        for call in _stub.CALLS
        if _service_kind(call["messages"]) is not None
        and (kind is None or _service_kind(call["messages"]) == kind)
    ]


def _working_edit(messages, line: str) -> str:
    """Правка рабочей памяти в том виде, в каком её просят у модели: первый
    раз — новая запись, дальше — правка **той же** записи по её номеру.

    Номер берётся из листинга, который вызову и показали: так отвечала бы
    модель, и так проверяется то, ради чего память перестала быть снимком —
    список не переписывается целиком, правка адресуется записи.
    """
    listing = messages[-1].get("content", "")
    first = re.search(r"^(\d+) ", listing, re.M)
    return (f"{first.group(1)} " if first else "+ ") + line


def _service_aware(messages, index):
    """Ответ заглушки, по которому видно, чем был вызов: сводка узнаётся
    в промпте следующего обмена по слову СВОДКА, ведение памяти — правкой
    единственной записи, в которой стоит номер вызова."""
    kind = _service_kind(messages)
    if kind == "summary":
        return f"СВОДКА {index}"
    if kind == "facts":
        return _working_edit(messages, f"решение: вызов {index}")
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

    # Консоль — вторая видимая поверхность, и единица на ней та же, что в
    # панели: сообщения, а не пары. Печатается в тот же `out`, что и ответ.
    cli._print_sessions(out=out)
    cli._print_agents(out=out)
    printed = out.getvalue()
    named = [line for line in printed.splitlines() if agent.id in line]
    assert len(named) == 2, f"агент назван не в двух списках, а в {len(named)}: {named}"
    for line in named:
        # Вопрос и ответ — два сообщения. «сообщений 1» значило бы, что консоль
        # считает пары; «реплик 2» — что она зовёт ту же величину другим
        # словом, чем экран, а слово во всём продукте одно.
        assert "сообщений 2" in line, line
        assert "реплик" not in line, line
    return (
        "ответ напечатан, история записана, агент в реестре; "
        "консоль называет 2 сообщения в обоих списках"
    )


# --- История: помнится и уезжает в модель целиком ------------------------------


@check("обрезка бывает только выбранная и всегда названная: full, window, facts, summary")
def check_cut_only_where_chosen():
    """Инвариант Дня 10, переформулированный из инварианта Дня 9.

    День 9 держал «молчаливой обрезки нет ни в одном состоянии»: история либо
    уезжала целиком, либо заменялась сводкой, которая покрывала выброшенное.
    Скользящее окно нарушает это по самому заданию — «остальное отбрасывайте».
    Значит инвариант не выбрасывается, а сужается до честного:

        Обрезка бывает только там, где её выбрал пользователь, и всегда
        видна: под ответом написано, сколько сообщений не уехало.

    Отсюда и проверяется, по стратегии на раздел: **что уезжает** и **как
    названо то, что не уехало**. Под ответом это число берётся из метрик
    обмена (`summarized` у сводки, `dropped` у окна) — слова, которыми клиент
    его называет, проверяет `checks/browser_check.js` настоящим `app.js`.

    Сама история при этом полная всегда, при любой стратегии: её не трогает
    ни одна из них, иначе сломалась бы перегенерация.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")

    # --- full: не срезается ничего, и это умолчание ---------------------------
    #
    # 1. Живой маршрут без стратегии вовсе: 25 обменов — больше прежнего окна.
    turns = 25
    with TestClient(main.app) as client:
        agent_id = new_agent(client, system="СИС")
        assert client.get(f"/api/agents/{agent_id}").json()["strategy"] == "full", "умолчание не full"
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
    long_chat = agent_module.Agent(AgentSpec(label="длинный", model="stub/model", system="СИС"))
    for i in range(500):
        long_chat.remember("user", f"реплика {i}")
    assert len(long_chat.history) == 500, "история подрезана при росте"
    assert long_chat.history[0].content == "реплика 0", "у истории отъели начало"

    prompt = long_chat.build_prompt("последний вопрос")
    assert len(prompt) == 502, len(prompt)
    assert prompt[1]["content"] == "реплика 0", prompt[1]

    # 3. Числа заданы, а стратегия так и осталась `full` — не срезается **всё
    # равно ничего**. Это ровно тот случай, в котором живут чаты Дня 9 после
    # обновления: ключа `strategy` в их конфиге нет, и поднимаются они с
    # `full`. Срежь их окно или сводка молча — обрезку выбрал бы не
    # пользователь, а версия сервера.
    _stub.reset()
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        numbers_only = new_agent(client, keep_last=KEEP, compress_every=EVERY)
        for i in range(9):
            client.post(f"/api/agents/{numbers_only}/messages", json={"text": f"вопрос {i}"})
        assert not _service_calls(), "стратегия `full`, а сжатие запустилось"
        assert len(_stub.CALLS[-1]["messages"]) == 17, len(_stub.CALLS[-1]["messages"])
        assert _stub.CALLS[-1]["messages"][0]["content"] == "вопрос 0", _stub.CALLS[-1]["messages"][0]

    # --- window: уезжает хвост, отброшенное названо числом --------------------
    _stub.reset()
    with TestClient(main.app) as client:
        win_id = new_agent(client, strategy="window", keep_last=KEEP)
        for i in range(9):
            client.post(f"/api/agents/{win_id}/messages", json={"text": f"вопрос {i}"})

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
    bare = agent_module.Agent(AgentSpec(label="ноль", model="stub/model", strategy="window",
                                        keep_last=0, system="СИС"))
    for i in range(6):
        bare.remember("user" if i % 2 == 0 else "assistant", f"реплика {i}", persist=False)
    assert bare.context_cut() == (6, None), bare.context_cut()
    assert [m["content"] for m in bare.build_prompt("вопрос")] == ["СИС", "вопрос"], (
        bare.build_prompt("вопрос")
    )

    # --- facts: рабочая память вместо начала, и только при своей стратегии ---
    #
    # Что именно уезжает при `facts` и как названо заменённое — у своей
    # проверки; здесь — половина про выбор: при чужой стратегии врезка
    # не подставляется, а без своего числа не собирается вовсе.
    _stub.reset()
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        facts_id = new_agent(client, strategy="facts", keep_last=KEEP)
        for i in range(9):
            client.post(f"/api/agents/{facts_id}/messages", json={"text": f"вопрос {i}"})
        with_facts = REGISTRY.require(facts_id)
        assert with_facts.working["items"], "память не собралась — проверять нечего"
        assert len(_stub.CALLS[-1]["messages"]) == KEEP + 2, len(_stub.CALLS[-1]["messages"])

        # `full` — история возвращается целиком, и выписка не подставляется,
        # хотя она есть и в памяти, и в базе. Подставься она здесь,
        # пользователь получил бы не ту стратегию, что выбрал.
        client.patch(f"/api/agents/{facts_id}", json={"strategy": "full"})
        client.post(f"/api/agents/{facts_id}/messages", json={"text": "после выключения"})
        back = _stub.CALLS[-1]["messages"]
        assert len(back) == 18 + 1, len(back)
        assert back[0]["content"] == "вопрос 0", back[0]
        assert not any("[факты о разговоре]" in m["content"] for m in back), back[0]
        assert with_facts.context_cut() == (0, None), with_facts.context_cut()
        assert with_facts.working["items"], "записи выброшены переключателем"

        # `window` на том же чате: записи в базе есть, а в промпт не идут —
        # у окна врезки нет, начало просто отброшено.
        client.patch(f"/api/agents/{facts_id}", json={"strategy": "window"})
        client.post(f"/api/agents/{facts_id}/messages", json={"text": "с окном"})
        win_back = _stub.CALLS[-1]["messages"]
        assert len(win_back) == KEEP + 1, len(win_back)
        assert not any("[факты о разговоре]" in m["content"] for m in win_back), win_back[0]
        assert with_facts.history[-1].metrics["dropped"] == 20 - KEEP, with_facts.history[-1].metrics
        assert "facts" not in with_facts.history[-1].metrics, "окно назвалось фактами"

        # И ни на `full`, ни на `window` за памятью к модели не ходят: вызов
        # на ведение оплачен так же, как обмен, а воспользоваться им при
        # чужой стратегии некому.
        assert len(_service_calls("facts")) == 9, len(_service_calls("facts"))

    # Стратегия выбрана, а числа нет — вести память не для кого: резать нечем,
    # уедет вся история, и врезке в промпте места нет. Платить за вызов,
    # которым никто не воспользуется, нельзя.
    _stub.reset()
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        bare_facts = new_agent(client, strategy="facts")
        for i in range(4):
            client.post(f"/api/agents/{bare_facts}/messages", json={"text": f"вопрос {i}"})
        assert not _service_calls(), "память ведётся без своего числа"
        assert len(_stub.CALLS[-1]["messages"]) == 7, len(_stub.CALLS[-1]["messages"])
        assert REGISTRY.require(bare_facts).context_cut() == (0, None), "пустое число режет историю"

    # --- summary: сводка вместо начала, механика Дня 9 нетронутой -------------
    #
    # 4. Порог ещё не набран — история уезжает целиком: **без сводки история
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
        for i in range(5):
            client.post(f"/api/agents/{folded_id}/messages", json={"text": f"вопрос {i}"})

        early = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in early] == ["user", "assistant"] * 4 + ["user"], early
        assert early[0]["content"] == "вопрос 0", early[0]
        assert not _service_calls(), "сжатие запустилось до порога"

        # 5. Дошли до порога: 9-й обмен сам уезжает уже сжатым.
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
        # Равенство, которое отличает сводку от окна: свёрнутое плюс хвост —
        # вся история на момент сборки промпта, ни одна реплика не пропала
        # без замены. Это 16 реплик восьми обменов: девятый в неё ещё не
        # записан — он как раз и уехал сжатым.
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

        # Формат ответа и стоп-строки на время сжатия сняты: чат с
        # {"type": "json_object"} вернул бы вместо пересказа объект, а
        # стоп-строка оборвала бы пересказ на середине. У самого обмена
        # они на месте — снимаются только у вызова на сжатие.
        folding_body = _service_calls("summary")[-1]["payload"]
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
        # Слово у сводки своё: она начало **заменила**, и прочитать его можно
        # в промпте запроса. Окно бы его отбросило совсем.
        assert folded.history[-1].metrics["summarized"] == 10, folded.history[-1].metrics
        assert "dropped" not in folded.history[-1].metrics, "сводка назвалась окном"
        assert folded.history[-3].metrics.get("summarized") is None, "пометка досталась обмену до сжатия"

        # 7. Второе сворачивание идёт инкрементально: прошлая сводка плюс
        # только новое, а не пересказ разговора с начала.
        for i in range(9, 14):
            client.post(f"/api/agents/{folded_id}/messages", json={"text": f"вопрос {i}"})
        assert len(folded.summaries) == 2, folded.summaries
        again = _service_calls("summary")[-1]["messages"][1]["content"]
        assert "СВОДКА" in again, "прошлая сводка в сжатие не попала — начало разговора потеряно"
        assert "вопрос 0" not in again, "сжатие пересказывает историю с начала заново"
        assert "вопрос 5" in again, again[:200]
        assert folded.summary_cover() == 20, folded.summary_cover()
        assert len(folded.history) == 28, len(folded.history)

        # 8. Переключатель обратим в обе стороны, и сводки в базе это переживают.
        # `full` — история возвращается в модель **целиком**, сводка не
        # подставляется, но и не выбрасывается.
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

        # `window` на том же чате: сводка в базе есть, но в промпт не идёт —
        # у окна врезки нет, и начало просто отброшено. Подставься сводка
        # здесь — пользователь получил бы не ту стратегию, что выбрал.
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
        assert all(name in bad.text for name in ("full", "window", "facts", "summary")), bad.text
        assert folded.spec.strategy == "summary", folded.spec.strategy

    # 9. Граница сворачивания не рвёт пару: история идёт парами, обе реплики
    # пишутся разом, и свёрнутый вопрос без своего ответа сделал бы хвост
    # бессмысленным. При нечётном окне граница округляется вниз до чётного.
    odd = agent_module.Agent(
        AgentSpec(label="нечёт", model="stub/model", strategy="summary",
                  keep_last=5, compress_every=EVERY)
    )
    for i in range(20):
        odd.remember("user" if i % 2 == 0 else "assistant", f"реплика {i}", persist=False)
    asyncio.run(odd.compress(odd.spec))
    odd_cover = odd.summary_cover()
    assert odd_cover == 14, odd_cover
    assert odd_cover % 2 == 0, f"граница разорвала пару: свёрнуто {odd_cover} реплик"
    assert odd.history[odd_cover].role == "user", odd.history[odd_cover].role

    # 10. Перегенерация снимает пару **с конца**, а сводка покрывает начало:
    # на коротком чате с нулевым окном они встречаются, и `upto` оказывается
    # больше истории. Зажатый длиной, он остаётся правдой; незажатый заявил
    # бы, что свёрнуто реплик больше, чем в чате было.
    short = agent_module.Agent(
        AgentSpec(label="перегенерация", model="stub/model", strategy="summary",
                  keep_last=0, compress_every=EVERY)
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

    # 11. Умолчание — «не резать», и держится это не на честном слове.
    # Кнопка «Новый чат» идёт мимо разбора полей, прямо от умолчаний
    # датакласса (`replace(NEW_CHAT_SPEC, ...)`), и консоль собирает
    # `AgentSpec` руками. Стань окно или сводка умолчанием — резали бы разом
    # все новые чаты и вся консоль, а разбор полей об этом и не узнал бы.
    _stub.reset()
    with TestClient(main.app) as client:
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        assert fresh["strategy"] == "full", fresh["strategy"]
        assert fresh["keep_last"] is None, fresh["keep_last"]
        assert fresh["compress_every"] is None, fresh["compress_every"]
        for i in range(9):
            client.post(f"/api/agents/{fresh['id']}/messages", json={"text": f"вопрос {i}"})
    assert not _service_calls(), "чат из умолчаний сворачивает историю"
    assert len(_stub.CALLS[-1]["messages"]) == 17, len(_stub.CALLS[-1]["messages"])

    return (
        f"full — вся история из {2 * turns} реплик и 500 хранимых целиком; "
        f"window — {KEEP + 1} сообщений в промпте, отброшено 10 и названо числом; "
        f"facts — выписка и хвост в {KEEP} реплик, при чужой стратегии не подставляется; "
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
    _stub.install(reply=_service_aware)
    question = "вопрос {i}: " + "довольно длинный текст вопроса, " * 20

    # События каждого обмена — по ним видно, что наружу сказано про сжатие:
    # оно идёт **до** ответа, и клиенту надо узнать о паузе, пока она идёт.
    events: dict[str, list[list[dict]]] = {}

    with TestClient(main.app) as client:
        plain = new_agent(client, label="без сжатия")
        folded = new_agent(
            client, label="со сжатием", strategy="summary", keep_last=KEEP, compress_every=EVERY
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
    slot = agent.context_slot(with_system)
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
    folding_frame = next(e for e in events[folded][8] if e["event"] == "compressing")
    assert folding_frame.get("strategy") == "summary", folding_frame

    # И сводку в промпте клиенту показывает сервер, а не разбор текста:
    # `summary_at` — её место в `resolved_messages`. Порядок сборки промпта
    # живёт в `build_prompt`, и вторая его копия в браузере разошлась бы молча.
    def start_of(agent_id, index):
        return next(e for e in events[agent_id][index] if e["event"] == "start")

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

    _stub.install(reply=_service_aware)
    path = _temp_db("summary-restart")
    store = Store(path).init()
    spec = AgentSpec(
        label="сжатый", model="stub/model", strategy="summary",
        keep_last=KEEP, compress_every=EVERY,
    )
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


@check("рабочая память: записи с устойчивыми номерами, правка по одной, чужое не трогается")
def check_working_memory():
    """Рабочая память — **список записей**, а не снимок, и вся проверка про
    разницу между этими двумя словами.

    Снимок можно было переписывать целиком: он был производным от истории и
    целиком принадлежал служебному вызову. Список — нет: у записи есть номер,
    по нему её правят и удаляют, и рядом с записями агента лежат записи
    человека, которых служебный вызов не вправе касаться. Отсюда четыре
    части: промпт (врезка встаёт той же ролью и на то же место, что у
    выписки Дня 10), ручки (род обязателен, номер устойчив, чужая запись
    переживает извлечение), разбор (правка адресуется записи, кривая строка
    пропускается, провал не роняет обмен) и файл (номера и авторство
    переживают перезапуск, а три пути очистки — нет).

    Вызов на ведение памяти идёт **на каждом обмене**: задание требует
    обновлять её после каждого сообщения пользователя. Это удваивает число
    обращений к модели — и так и задумано.
    """
    from app.agent import Agent

    # --- 1. Живой маршрут: врезка вместо начала, хвост как есть -------------
    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        agent_id = new_agent(
            client,
            strategy="facts",
            keep_last=KEEP,
            system="СИС",
            stop=["СТОП"],
            response_format={"type": "json_object"},
        )
        for i in range(9):
            response = client.post(f"/api/agents/{agent_id}/messages", json={"text": f"вопрос {i}"})
            assert response.status_code == 200, response.text

        agent = REGISTRY.require(agent_id)
        sent = _stub.CALLS[-1]["messages"]

        # Кадр о служебном вызове приходит до промпта и **называет вызов**:
        # кадр один на оба рода, и без этого поля клиент не знал бы, что
        # писать в строке состояния, — молчал бы и про память, и про
        # сворачивание. Тем же полем подписана врезка в просмотре промпта.
        frames = sse(response.text)
        kinds = [e["event"] for e in frames]
        assert kinds.index("compressing") < kinds.index("start"), kinds
        pause = next(e for e in frames if e["event"] == "compressing")
        assert pause.get("strategy") == "facts", pause
        start = next(e for e in frames if e["event"] == "start")
        assert start.get("strategy") == "facts", start.get("strategy")
        assert start["summary_at"] == 1, start["summary_at"]
        assert "[факты о разговоре]" in start["resolved_messages"][start["summary_at"]]["content"], (
            start["resolved_messages"][start["summary_at"]]
        )

        # Вызов на ведение памяти — на каждом обмене, и он единственный
        # служебный: сжатия у этой стратегии нет вовсе.
        assert len(_service_calls("facts")) == 9, len(_service_calls("facts"))
        assert _service_calls("summary") == [], "память зачем-то сворачивает историю"
        assert len(_stub.CALLS) == 18, len(_stub.CALLS)

        # Девять вызовов — **одна** запись: каждый следующий правил ту же
        # по номеру, а не заводил вторую такую же. Ровно это и отличает
        # список записей от снимка: снимок переписывался целиком и не мог
        # не совпасть сам с собой, а список обязан адресовать правку.
        assert len(agent.working["items"]) == 1, agent.working["items"]
        kept = agent.working["items"][0]
        assert kept["author"] == "agent", kept
        assert kept["kind"] == "decision" and kept["content"] == "вызов 16", kept

        # Промпт: системный промпт, врезка, хвост в KEEP реплик, вопрос.
        assert [m["role"] for m in sent] == (
            ["system", "user"] + ["user", "assistant"] * 3 + ["user"]
        ), [m["role"] for m in sent]
        assert sent[0]["content"] == "СИС", sent[0]
        # Врезка едет ролью `user` с подписью, а не системной репликой:
        # системный промпт живёт ровно в одном месте — `spec.system`, — и
        # второй системной репликой чат перестал бы быть тем, что настроили.
        # Вид записи подписан по-русски — той же картой, какой подписана
        # долговременная память и какой отвечает сам служебный вызов.
        assert sent[1] == {
            "role": "user",
            "content": (
                "[факты о разговоре]\n"
                "решение: вызов 16\n"
                "[дальше — последние сообщения как есть]"
            ),
        }, sent[1]
        assert len([m for m in sent if m["role"] == "system"]) == 1, sent
        assert sent[2]["content"] == "вопрос 5", sent[2]
        assert sent[-1]["content"] == "вопрос 8", sent[-1]

        # Заменённое названо числом, и слово у памяти своё: врезка начало
        # **заменила**, прочитать её можно в промпте запроса; окно бы его
        # отбросило совсем, и назвать оба одинаково значило бы соврать.
        cut, insert = agent.context_cut()
        assert insert is not None and "[факты о разговоре]" in insert["content"], insert
        assert cut == 12, cut
        # Слот врезки — то самое число, что уехало в `start`: промолчи он,
        # клиент не удержал бы промпт, и кнопка «Показать промпт запроса»
        # у этого чата не появилась бы никогда. А на ней держится слово
        # «вместо»: замену **видно**, в отличие от отброшенного окном.
        assert agent.context_slot() == 1, agent.context_slot()
        assert agent.history[-1].metrics["facts"] == 10, agent.history[-1].metrics
        assert "dropped" not in agent.history[-1].metrics, "память назвалась окном"
        assert "summarized" not in agent.history[-1].metrics, "память назвалась сводкой"

        # Ведение идёт инкрементально: нынешние записи плюс только новое.
        # Перечитывай оно разговор с начала — а вызов на каждом обмене, — и
        # платить пришлось бы за весь чат столько раз, сколько в нём сообщений.
        # Записи показаны **с номерами**: без них правка не адресуется.
        asked = _service_calls("facts")[-1]["messages"]
        assert asked[0]["content"] == agent_module.WORKING_SYSTEM, asked[0]
        assert f"{kept['seq']} решение: " in asked[1]["content"], asked[1]
        assert "вопрос 0" not in asked[1]["content"], "ведение перечитывает разговор с начала"
        assert "вопрос 7" in asked[1]["content"] and "вопрос 8" in asked[1]["content"], asked[1]

        # Тело служебного вызова: формат ответа и стоп-строки сняты — объект
        # вместо правок не разобрался бы, а стоп-строка оборвала бы их на
        # середине. Модель та же: вторая развалила бы счёт на две цены. И три
        # правила тела на нём тоже — особенно выключенное провайдерское
        # сжатие: оно молча выбросило бы середину того, что мы отдали читать.
        service_body = _service_calls("facts")[-1]["payload"]
        assert "response_format" not in service_body, service_body.get("response_format")
        assert "stop" not in service_body, service_body.get("stop")
        assert service_body["model"] == _stub.CALLS[-1]["payload"]["model"], service_body["model"]
        assert _stub.CALLS[-1]["payload"]["response_format"] == {"type": "json_object"}
        assert service_body["provider"]["require_parameters"] is True, service_body["provider"]
        assert {"id": "context-compression", "enabled": False} in (service_body.get("plugins") or []), (
            f"память собирал провайдер со своим сжатием: {service_body.get('plugins')!r}"
        )
        assert service_body["usage"] == {"include": True}, service_body.get("usage")

        # История цела: память живёт в сборке промпта, а не в ленте чата.
        # На этом держится перегенерация — она ждёт хвост и сверяет длину.
        assert len(agent.history) == 18, len(agent.history)
        assert agent.history[0].content == "вопрос 0", agent.history[0]
        assert agent.take_last_exchange() is not None, "перегенерация не сняла пару"

        # --- ручки чата: род обязателен, номер выдаёт база ------------------
        #
        # Разбор тела — по образцу долговременной памяти, и род здесь так же
        # без умолчания: «явно выбирать, что и куда сохраняется» перестало бы
        # быть работой человека, подставь сервер род за него.
        url = f"/api/agents/{agent_id}/working"
        listed = client.get(url).json()
        assert listed["total"] == 1 and listed["upto"] == 16, listed
        assert listed["records"][0]["seq"] == kept["seq"], listed["records"]

        bad = client.post(url, json={"content": "без рода"})
        assert bad.status_code == 400 and "goal" in bad.text, bad.text
        assert client.post(url, json={"kind": "цель", "content": "подписью"}).status_code == 400
        assert client.post(url, json={"kind": "profile", "content": "чужой слой"}).status_code == 400
        assert client.post(url, json={"kind": "goal", "content": "   "}).status_code == 400
        # Автора и номер выдаёт сервер: тело, которое их присылает, просит
        # не то, что ручка делает.
        assert client.post(
            url, json={"kind": "goal", "content": "х", "author": "agent"}
        ).status_code == 400

        human = client.post(url, json={"kind": "limit", "content": "  бюджет 100к  "})
        assert human.status_code == 200, human.text
        human = human.json()
        assert human["author"] == "human", human
        assert human["content"] == "бюджет 100к", human
        assert human["seq"] > kept["seq"], (human, kept)

        edited = client.patch(f"{url}/{human['seq']}", json={"content": "бюджет 200к"})
        assert edited.status_code == 200, edited.text
        assert edited.json()["seq"] == human["seq"], edited.json()
        assert edited.json()["kind"] == "limit", edited.json()
        assert client.patch(f"{url}/{human['seq']}", json={}).status_code == 400
        assert client.patch(f"{url}/{human['seq']}", json={"kind": "х"}).status_code == 400
        assert client.patch(f"{url}/9999", json={"content": "нет такой"}).status_code == 404
        assert client.delete(f"{url}/9999").status_code == 404

        # Род — не ключ: две записи одного рода живут рядом, ключ у списка
        # это номер. Снимок Дня 10 собирался словарём по ключу, и вторая
        # «цель» вытесняла первую молча; здесь их две, и это единственное
        # место, где врезка в промпте отличается от вчерашней.
        twin = client.post(url, json={"kind": "limit", "content": "срок до мая"})
        assert twin.status_code == 200, twin.text
        twin = twin.json()
        assert twin["seq"] > human["seq"], (twin, human)
        assert [(r["kind"], r["content"]) for r in client.get(url).json()["records"]] == [
            ("decision", "вызов 16"), ("limit", "бюджет 200к"), ("limit", "срок до мая"),
        ], client.get(url).json()["records"]
        assert client.delete(f"{url}/{twin['seq']}").status_code == 200

        # --- и главный инвариант: чужую запись извлечение не трогает --------
        #
        # Служебному вызову тут прямо велено её переписать и удалить —
        # и оба раза мимо. Проверяется кодом, а не обещанием в промпте:
        # инструкция модели — просьба, а не гарантия, и первая же правка
        # руками жила бы до ближайшего обмена.
        def hostile(messages, index):
            if _service_kind(messages) != "facts":
                return f"ответ {index}"
            return f"{human['seq']} цель: подменённая чужая запись\n- {human['seq']}"

        _stub.install(reply=hostile)
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "вопрос после правки"})
        survived = client.get(url).json()["records"]
        assert [r["seq"] for r in survived] == [kept["seq"], human["seq"]], survived
        assert survived[1] == {**human, "content": "бюджет 200к", "at": survived[1]["at"]}, survived[1]
        assert survived[1]["author"] == "human", survived[1]

        # И запись человека уезжает в промпт наравне с агентской — слой один,
        # и врезка не различает, кто её наполнил.
        block = _stub.CALLS[-1]["messages"][1]["content"]
        assert "ограничение: бюджет 200к" in block, block
        assert "подменённая" not in block, block

    # --- 2. Разбор правок: адресуется запись, номера устойчивы --------------
    #
    # Хранилище здесь настоящее: номера выдаёт база (AUTOINCREMENT), и
    # «номер удалённой записи не достаётся следующей» проверяется там, где
    # он и обещан, — первичным ключом, а не счётчиком в памяти процесса.
    mode = {"reply": "мусор"}

    def replies(messages, index):
        if _service_kind(messages) != "facts":
            return f"ответ {index}"
        if mode["reply"] == "мусор":
            return (
                "Вот что я понял:\n"
                "+ цель: собрать ТЗ\n"
                "строка без двоеточия\n"
                "+ ограничение:   \n"
                "+ : без вида\n"
                "+ срок: апрель\n"
                "цель: строка без маркера\n"
                "1. цель: пункт нумерованного списка\n"
                "- 9999\n"
                "+ открытый вопрос: когда релиз"
            )
        if mode["reply"] == "правка":
            return "1 цель: собрать ТЗ и смету"
        if mode["reply"] == "добавка":
            return "+ цель: собрать ТЗ и смету"
        if mode["reply"] == "удаление":
            return "- 2\n+ решение: платим картой"
        return "никаких правок здесь нет\nи здесь тоже"

    _stub.install(reply=replies)
    store = Store(":memory:").init()
    tolerant = Agent(
        AgentSpec(label="разбор", model="stub/model", strategy="facts", keep_last=2), store=store
    )
    asyncio.run(drain(tolerant.ask("первый вопрос")))

    # Кривые строки пропущены, ровные разобраны: заголовок «Вот что я понял:»
    # ушёл в мусор, а не унёс с собой соседей; пустое содержимое, неназванный
    # и незнакомый вид — туда же, рода по умолчанию нет ни здесь, ни у ручки.
    # А строка **без маркера** — не правка вовсе: повтор всего списка (самое
    # естественное, что сделает модель, если ей позволить) завёл бы вторые
    # копии всех записей на каждом обмене. «1. цель» — пункт нумерованного
    # списка, а не правка первой записи: принять его значило бы молча
    # переписать чужую.
    assert [(r["seq"], r["kind"], r["content"]) for r in tolerant.working["items"]] == [
        (1, "goal", "собрать ТЗ"),
        (2, "question", "когда релиз"),
    ], tolerant.working["items"]
    assert store.list_working(tolerant.id) == tolerant.working["items"], store.list_working(tolerant.id)

    # Правка адресована первой записи: у неё сменилось содержимое, номер
    # остался её, а соседнюю не сдвинуло. Перенумеруй запись правка — и
    # вторая вкладка, показывающая список с прошлой минуты, удалила бы не ту.
    mode["reply"] = "правка"
    asyncio.run(drain(tolerant.ask("второй вопрос")))
    assert [(r["seq"], r["content"]) for r in tolerant.working["items"]] == [
        (1, "собрать ТЗ и смету"),
        (2, "когда релиз"),
    ], tolerant.working["items"]

    # Удаление плюс новая запись: номер удалённой не достаётся следующей.
    # Обычный `INTEGER PRIMARY KEY` снял бы номер с последней и отдал его
    # новой — и правка «по номеру два» попала бы не в ту запись.
    mode["reply"] = "удаление"
    asyncio.run(drain(tolerant.ask("третий вопрос")))
    assert [(r["seq"], r["kind"], r["content"]) for r in tolerant.working["items"]] == [
        (1, "goal", "собрать ТЗ и смету"),
        (3, "decision", "платим картой"),
    ], tolerant.working["items"]
    # И то же самое из базы: номера там те же, что в памяти процесса. Перенумеруй
    # их хоть запись, хоть чтение — дыра на месте второй записи затянулась бы,
    # и правка «по номеру три» после подъёма чата попала бы не в ту запись.
    assert store.list_working(tolerant.id) == tolerant.working["items"], store.list_working(tolerant.id)

    # Запись человека переживает извлечение: ни правка, ни удаление её
    # не берут — а соседнюю, свою, тот же вызов правит как ни в чём не бывало.
    mine = tolerant.add_working_record("limit", "бюджет 100к")
    assert mine["author"] == "human", mine
    mode["reply"] = "чужое"
    _stub.install(reply=lambda m, i: (
        f"ответ {i}" if _service_kind(m) != "facts"
        else f"{mine['seq']} цель: подмена\n- {mine['seq']}\n1 цель: собрать ТЗ, смету и сроки"
    ))
    asyncio.run(drain(tolerant.ask("четвёртый вопрос")))
    assert [(r["seq"], r["content"], r["author"]) for r in tolerant.working["items"]] == [
        (1, "собрать ТЗ, смету и сроки", "agent"),
        (3, "платим картой", "agent"),
        (mine["seq"], "бюджет 100к", "human"),
    ], tolerant.working["items"]

    # Правка руками метит запись человеком, даже если завёл её вызов: с этой
    # минуты переписывать её обратно извлечение не вправе.
    tolerant.edit_working_record(3, content="платим только картой")
    _stub.install(reply=lambda m, i: (
        f"ответ {i}" if _service_kind(m) != "facts" else "3 решение: платим как угодно"
    ))
    asyncio.run(drain(tolerant.ask("пятый вопрос")))
    assert [(r["seq"], r["content"], r["author"]) for r in tolerant.working["items"]][1] == (
        3, "платим только картой", "human"
    ), tolerant.working["items"]

    # Обратное направление — вторая половина того же инварианта: агент чужого
    # не трогает, а человек трогает **любое**. Запись 1 завёл служебный вызов,
    # и удаляется она руками без разговоров: иначе ошибку агента нечем было бы
    # исправить, а список рос бы записями, которые никто не вправе убрать.
    assert tolerant.drop_working_record(1) is True, "человек не смог удалить запись агента"
    assert [(r["seq"], r["author"]) for r in tolerant.working["items"]] == [
        (3, "human"), (mine["seq"], "human")
    ], tolerant.working["items"]
    assert store.list_working(tolerant.id) == tolerant.working["items"], store.list_working(tolerant.id)

    # --- 3. Провал ведения обмен не роняет и не даёт срезать непрочитанное --
    _stub.install(reply=replies)
    mode["reply"] = "пусто"
    before = [dict(r) for r in tolerant.working["items"]]
    upto_before = tolerant.working["upto"]
    events = asyncio.run(drain(tolerant.ask("шестой вопрос")))
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["committed"] and done["text"], done
    assert tolerant.working["items"] == before, tolerant.working["items"]
    assert tolerant.working["upto"] == upto_before, tolerant.working["upto"]

    # И то же самое, когда вызов падает исключением, а не пустым ответом.
    real_stream = agent_module.stream_completion

    def falling(spec, *, prompt_override=None, context_length=None):
        if _service_kind(prompt_override) == "facts":
            raise RuntimeError("ведение памяти упало")
        return real_stream(spec, prompt_override=prompt_override, context_length=context_length)

    agent_module.stream_completion = falling
    try:
        events = asyncio.run(drain(tolerant.ask("седьмой вопрос")))
    finally:
        agent_module.stream_completion = real_stream
    done = [e for e in events if e["type"] == "done"][-1]
    assert done["committed"] and done["text"], done
    assert not [e for e in events if e["type"] == "error"], events
    assert tolerant.working["items"] == before, tolerant.working["items"]
    assert len(tolerant.history) == 14, len(tolerant.history)

    # И главное про провал: память его пережила, а история за это время
    # выросла. Срез, считанный по одному хвосту, вырос бы вместе с ней и унёс
    # реплики, которых память **никогда не видела**, — молча и под подписью
    # «факты вместо N сообщений». Поэтому срез зажат ещё и по `upto`:
    # срезано ровно то, что память прочитала.
    assert upto_before == 6, upto_before
    assert tolerant.context_cut()[0] == 6, tolerant.context_cut()
    # Врезка плюс восемь непрочитанных реплик плюс вопрос: хвост уехал длиннее
    # просимых двух — это и есть цена провала, дорогая, но честная.
    assert len(tolerant.build_prompt("после провалов")) == 1 + 8 + 1, tolerant.build_prompt(
        "после провалов"
    )

    # А когда вызов проходит, срез считается по хвосту, как и просили: зажим
    # бережёт от молчаливой потери, а не отменяет стратегию. Вызов здесь
    # **заводит** запись, а не правит: своих записей у него не осталось —
    # одну забрал человек правкой, другую удалил, — и это ровно тот случай,
    # ради которого правка и добавление разные маркеры.
    mode["reply"] = "добавка"
    asyncio.run(drain(tolerant.ask("восьмой вопрос")))
    # Номер у новой записи свой: удалённая первая его не вернула.
    assert [(r["seq"], r["author"]) for r in tolerant.working["items"]] == [
        (3, "human"), (mine["seq"], "human"), (mine["seq"] + 1, "agent")
    ], tolerant.working["items"]
    assert tolerant.working["upto"] == 14, tolerant.working["upto"]
    assert tolerant.context_cut()[0] == 14, tolerant.context_cut()[0]
    assert len(tolerant.history) == 16, len(tolerant.history)
    store.close()

    # --- 4. Файл: перезапуск переживают, чистку — нет -----------------------
    def remembering(messages, index):
        if _service_kind(messages) != "facts":
            return f"ответ {index}"
        # Память помнит последний вопрос, который видела: по ней и видно,
        # что именно уехало в вызов и чья это память.
        return _working_edit(
            messages, "цель: " + messages[1]["content"].strip().splitlines()[-1]
        )

    _stub.install(reply=remembering)
    path = _temp_db("working-restart")
    store = Store(path).init()
    spec = AgentSpec(label="с памятью", model="stub/model", strategy="facts", keep_last=KEEP)
    agent = Agent(spec, store=store)
    for i in range(9):
        asyncio.run(drain(agent.ask(f"вопрос {i}")))
    mine = agent.add_working_record("question", "успеем ли к маю")
    before = [dict(r) for r in agent.working["items"]]
    agent_id = agent.id
    assert [(r["kind"], r["content"], r["author"]) for r in before] == [
        ("goal", "Пользователь: вопрос 8", "agent"),
        ("question", "успеем ли к маю", "human"),
    ], before
    assert agent.working["upto"] == 16, agent.working["upto"]
    store.close()

    # Настоящее переоткрытие файла, а не тот же объект в памяти.
    again = Store(path).init()
    try:
        revived = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=again)
        # Номера и авторство пережили перезапуск: без них правка «по номеру»
        # после подъёма чата попала бы не в ту запись, а чужая запись стала бы
        # своей и первое же извлечение её переписало.
        assert revived.working["items"] == before, (before, revived.working["items"])
        # Вместе с записями поднялось и то, докуда прочитана история: без
        # этого числа ведение после перезапуска перечитало бы разговор
        # с начала — а окно успело бы срезать его раньше.
        assert revived.working["upto"] == 16, revived.working["upto"]
        assert revived.working["metrics"], "метрики ведения не пережили перезапуск"
        assert len(revived.history) == 18, len(revived.history)
        assert "[факты о разговоре]" in revived.build_prompt("ещё")[0]["content"], "врезка не встала"

        # Записей нет среди реплик: они в своей таблице, а не в ленте. Лежи они
        # строкой в `messages`, их стирал бы каждый следующий обмен —
        # `save_history` начинается с `DELETE FROM messages`.
        contents = [r[2] for r in again.message_rows(agent_id)]
        assert not any("[факты о разговоре]" in c for c in contents), contents

        _stub.reset()
        asyncio.run(drain(revived.ask("вопрос после перезапуска")))
        asked = _service_calls("facts")[-1]["messages"][1]["content"]
        assert "вопрос 0" not in asked, "после перезапуска ведение читает историю заново"
        assert "вопрос после перезапуска" in asked, asked
        saved = again.list_working(agent_id)
        assert [(r["content"], r["author"]) for r in saved] == [
            ("Пользователь: вопрос после перезапуска", "agent"),
            ("успеем ли к маю", "human"),
        ], saved
        assert [r["seq"] for r in saved] == [r["seq"] for r in before], (saved, before)
        assert len(revived.history) == 20, len(revived.history)

        # Чистка чата уносит и рабочую память: каскада в схеме нет. Зажима
        # по длине истории у неё нет вовсе — врезка встаёт в промпт, пока
        # в памяти есть хоть одна запись, — и забытый разговор оставил бы
        # свои цели и решения следующему.
        revived.forget()
        assert again.list_working(agent_id) == [], again.list_working(agent_id)
        assert again.load_working_state(agent_id)["upto"] == 0, again.load_working_state(agent_id)
        assert revived.working["items"] == [], revived.working
        assert again.message_rows(agent_id) == [], again.message_rows(agent_id)
        for i in range(4):
            asyncio.run(drain(revived.ask(f"новый вопрос {i}")))
        block = revived.build_prompt("ещё")[0]["content"]
        assert "[факты о разговоре]" in block, block
        assert "вопрос 8" not in block, "забытый разговор оставил свою память следующему"

        # Удаление чата — второй такой путь: номера чатов идут по возрастанию,
        # а после очистки базы начинаются заново, и память удалённого
        # разговора досталась бы чату с тем же id.
        #
        # Обменов здесь **два**, и это не щедрость сценария. Ведение идёт
        # до записи истории, поэтому после первого обмена курсор ещё ноль —
        # и утверждение «после очистки ноль» держалось бы само собой, не
        # заметив оставленной строки `working_state` с непустыми метриками.
        # Курсор, за которым следят, обязан сперва отличаться от нуля.
        doomed = Agent(spec, store=again)
        for i in range(2):
            asyncio.run(drain(doomed.ask(f"вопрос удаляемого {i}")))
        assert again.list_working(doomed.id), "память не записалась — проверять нечего"
        assert again.load_working_state(doomed.id)["upto"] == 2, again.load_working_state(doomed.id)
        again.delete_session(doomed.id)
        assert again.list_working(doomed.id) == [], "удаление чата оставило его память"
        assert again.load_working_state(doomed.id)["upto"] == 0, "удаление чата оставило прочитанное"

        # Очистка базы — третий, и курсор у неё так же обязан быть не нулевым
        # до очистки: иначе стерегущее его утверждение вакуумно и здесь.
        keeper = Agent(spec, store=again)
        for i in range(2):
            asyncio.run(drain(keeper.ask(f"вопрос стираемого {i}")))
        assert again.list_working(keeper.id), "память не записалась — проверять нечего"
        assert again.load_working_state(keeper.id)["upto"] == 2, again.load_working_state(keeper.id)

        # И раз чат под рукой — заодно единственное место, где виден страж
        # чужого чата: номера сквозные на всю базу, и через агента чужой
        # номер не придёт никогда, а прямым вызовом хранилища — вот так.
        # Без `session_id` в `WHERE` правка ушла бы в соседний разговор.
        someone = again.list_working(keeper.id)[0]
        assert again.update_working(
            doomed.id, someone["seq"], kind="goal", content="взлом", author="agent"
        ) is None, "правка по чужому чату прошла"
        assert again.delete_working(doomed.id, someone["seq"]) is False, "удаление по чужому чату прошло"
        assert again.list_working(keeper.id)[0] == someone, again.list_working(keeper.id)

        again.clear()
        assert again.list_working(keeper.id) == [], "очистка базы оставила память"
        assert again.load_working_state(keeper.id)["upto"] == 0, "очистка базы оставила прочитанное"
    finally:
        again.close()

    return (
        f"врезка вместо 10 сообщений и хвост в {KEEP} реплик, слот врезки назван "
        "в кадре `start`; вызов на каждом из 9 обменов правит одну запись по номеру; "
        "кривые строки и строка без маркера пропущены, удалённый номер не выдан заново, "
        "запись человека пережила и правку, и удаление; провал обмен не уронил и не дал "
        "срезать непрочитанное; перезапуск номера и авторство пережили, три пути очистки — нет"
    )


@check("ветка уносит ровно просимое, живёт независимо и переживает перезапуск")
def check_branch_independent():
    """Ветвление Дня 10: «сохраните checkpoint, создайте 2 ветки от одного
    места, продолжите диалог в каждой независимо, переключайтесь между ними».

    Ветка — **отдельный чат**: своя строка в `sessions`, свой id от базы, своя
    история под своим `session_id`. Схему `messages` ветвление не тронуло
    вовсе. Отсюда и разделы проверки:

    * **унесено ровно просимое** — первые `at` сообщений и копия конфига,
      с номерами от нуля и без дыр;
    * **врезка едет только своя** — сводка и выписка заменяют начало истории,
      и заменять им можно ровно то начало, которое в ветке есть: ветка,
      унёсшая половину разговора, не вправе унести сводку про весь;
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
        parent = new_agent(
            client, label="родитель", system="СИС", temperature=0.5,
            extra_body={"provider": {"order": ["stub"]}},
        )
        for i in range(3):
            client.post(f"/api/agents/{parent}/messages", json={"text": f"вопрос {i}"})
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
    for i in range(9):
        asyncio.run(drain(folded.ask(f"вопрос {i}")))
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
    for i in range(9, 14):
        asyncio.run(drain(folded.ask(f"вопрос {i}")))
    assert len(folded.summaries) == 2 and folded.summary_cover() == 20, folded.summaries
    assert [item["upto"] for item in far.summaries] == [10], far.summaries
    assert far.summary_cover() == 10 and len(far.history) == 18, far.summary_cover()
    assert far.build_prompt("ещё") == far_prompt, "сворачивание родителя сменило промпт ветки"

    listing = agent_module.Agent(
        AgentSpec(label="память", model="stub/model", strategy="facts", keep_last=KEEP),
        store=store,
    )
    for i in range(5):
        asyncio.run(drain(listing.ask(f"вопрос {i}")))
    # Запись человека рядом с агентскими: без неё поле автора в сравнении
    # ниже ничего не стерегло бы — все записи родителя были бы агентскими,
    # и ветка, пометившая унесённое собой, прошла бы незамеченной. А цена
    # у этого прямая: первое же ведение в ветке переписало бы чужую запись.
    listing.add_working_record("limit", "своя запись")
    upto = listing.working["upto"]
    said = [(r["kind"], r["content"], r["author"]) for r in listing.working["items"]]
    assert [r["author"] for r in listing.working["items"]] == ["agent", "human"], said
    assert upto == 8 and said, listing.working
    # Та же граница и так же вплотную: `upto - 1` и ровно `upto`.
    thin = reg.fork(listing, upto - 1, label="на одно раньше границы памяти")
    brim = reg.fork(listing, upto, label="ровно по границе памяти")
    fat = reg.fork(listing, 10, label="ветка после границы памяти")
    # Память прочитала восемь реплик, а ветка унесла семь: в ней памяти
    # нет вовсе — иначе она рассказала бы про разговор, которого в ветке
    # не было, и под ответом стояло бы «факты вместо N сообщений» про это.
    assert thin.working == agent_module.empty_working(), thin.working
    assert store.list_working(thin.id) == [], store.list_working(thin.id)
    assert thin.context_cut() == (0, None), thin.context_cut()
    # А унёсшая ровно прочитанное — уносит память с её же границей, и режет
    # ею столько, сколько память прочитала, а не сколько хотелось бы.
    assert [(r["kind"], r["content"], r["author"]) for r in brim.working["items"]] == said, (
        brim.working["items"]
    )
    assert brim.working["upto"] == upto, brim.working["upto"]
    assert store.list_working(brim.id) == brim.working["items"], store.list_working(brim.id)
    assert brim.context_cut()[0] == upto - KEEP, brim.context_cut()
    # Номера у копий свои: номер принадлежит одному чату, и две записи под
    # одним номером в разных разговорах — ровно та путаница, из-за которой
    # он и стал сквозным на всю базу.
    assert {r["seq"] for r in brim.working["items"]}.isdisjoint(
        {r["seq"] for r in listing.working["items"]}
    ), (brim.working["items"], listing.working["items"])
    # А унёсшая всё прочитанное — уносит и память, и её границу.
    assert [(r["kind"], r["content"], r["author"]) for r in fat.working["items"]] == said, (
        fat.working["items"]
    )
    assert fat.working["upto"] == upto, fat.working["upto"]
    assert store.list_working(fat.id) == fat.working["items"]
    assert fat.context_cut()[1] is not None, "память не встала в промпт ветки"
    # Копия глубокая: правка у ветки не видна родителю.
    fat.add_working_record("limit", "чужое ограничение")
    assert len(listing.working["items"]) + 1 == len(fat.working["items"]), "память общая"

    branch_id, parent_id = far.id, folded.id
    store.close()

    # --- перезапуск, обмен после него и три исхода очистки -----------------
    again = Store(path).init()
    try:
        revived = agent_module.Agent(
            AgentSpec(label="пусто", model="x/y"), agent_id=branch_id, store=again
        )
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
        # откуда чат взялся. Ветка, забывшая историю, осталась веткой.
        revived.forget()
        assert again.message_rows(branch_id) == [], again.message_rows(branch_id)
        assert again.load_branch(branch_id) == {"parent_id": parent_id, "forked_at": 18}

        # А удаление самого чата — трогает: оставленная строка досталась бы
        # чату с тем же id, и свежий чат оказался бы веткой мёртвого.
        again.delete_session(branch_id)
        assert again.load_branch(branch_id) is None, "удаление чата оставило родство"

        # Очистка базы — второй такой путь, и после неё id начинаются заново.
        doomed = agent_module.Agent(AgentSpec(label="ещё", model="stub/model"), store=again)
        again.save_branch(doomed.id, parent_id="ag_00001", forked_at=2)
        assert again.load_branch(doomed.id) is not None, "родство не записалось"
        again.clear()
        assert again.load_branch(doomed.id) is None, "очистка базы оставила родство"
    finally:
        again.close()
    return (
        "две ветки от одного места унесли по 4 сообщения, лента родителя "
        "сразу после ветвления та же; ветка от ветки называет родителя, "
        "а не деда; пометка та же и у выгруженной; ветвление посреди ответа "
        "унесло ровно записанное; сводка с границей 10 уехала в ветку на 10 "
        "и не уехала в ветку на 9, выписка с границей 8 — в 8 и не в 7; "
        "перезапуск и удаление родителя ветка пережила, удаление чата "
        "и очистка базы уносят родство"
    )


@check("долговременная память: слой глобальный, врезка по выключателю, чат её переживает")
def check_long_term_memory():
    """День 11: три слоя памяти, и третий из них — долговременный.

    Два слоя были и раньше, оба привязаны к чату: краткосрочная — сама
    история (`messages`), рабочая — состояние задачи и сводки
    (`working_memory`, `summaries`). Третий отличается всем сразу: таблица **без**
    `session_id`, наполняется **только руками**, и чат ему читатель, а не
    владелец — удаление чата и `forget()` память не трогают.

    Смотрим по порядку: пустая память неотличима от отсутствующей; ручки
    и их валидация (род записи выбирает человек); врезка в промпте и её
    место; **две врезки разом** — память плюс факты, где слот стратегии
    обязан сдвинуться; выключатель чата; хранение врозь; удаление по одной
    без перенумерации и без переиспользования номера; переживание `forget()`,
    удаления чата и переоткрытия файла; `clear()`, который круг замыкает.
    """
    from app.agent import Agent

    _stub.install(reply=_service_aware)
    with TestClient(main.app) as client:
        # --- 1. Пустая память неотличима от отсутствующей --------------------
        #
        # Выключатель по умолчанию включён, и на свежей базе это обязано
        # не значить ничего: врезки нет вовсе, а не пустая. Иначе каждая
        # проверка с точной последовательностью ролей поехала бы на сообщение.
        assert client.get("/api/memory").json() == {"total": 0, "records": []}, "память не пуста"
        plain = new_agent(client, system="СИС")
        assert client.get(f"/api/agents/{plain}").json()["memory"] == "on", "умолчание не `on`"
        empty = sse(client.post(f"/api/agents/{plain}/messages", json={"text": "первый"}).text)
        start = next(e for e in empty if e["event"] == "start")
        assert start["memory_at"] is None, start["memory_at"]
        assert [m["role"] for m in _stub.CALLS[-1]["messages"]] == ["system", "user"], _stub.CALLS[-1]

        # --- 2. Ручки: род записи выбирает человек, а не сервер ---------------
        #
        # `kind` обязателен и без умолчания. Подставь сервер «knowledge» на
        # пропущенный ключ — и «явно выбирать, что и куда сохраняется» стало
        # бы «сервер выбрал за тебя», то есть ровно тем, чего задание просит
        # избежать.
        bad = [
            {"content": "род не назван"},
            {"kind": None, "content": "род снят"},
            {"kind": "profil", "content": "род с опечаткой"},
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
        #
        # Роль `user` с подписью, а не `system`: системный промпт живёт ровно
        # в одном месте — `spec.system`, — и вторая системная реплика сделала
        # бы чат не тем, что настроили. Род подписан по-русски, одной картой
        # с интерфейсом.
        _stub.reset()
        client.post(f"/api/agents/{plain}/messages", json={"text": "второй"})
        sent = _stub.CALLS[-1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "user", "assistant", "user"], sent
        block = sent[1]["content"]
        assert block.startswith("[долговременная память]"), block
        assert block.endswith("[конец долговременной памяти]"), block
        assert "профиль: пишу на Kotlin" in block, block
        assert "решение: оплата только картой" in block, block
        assert "знание: релиз в мае" in block, block
        assert len([m for m in sent if m["role"] == "system"]) == 1, sent
        # Закрывающая скобка нейтральная: после памяти может встать врезка
        # стратегии, и «дальше — последние сообщения как есть» соврало бы.
        assert "как есть" not in block, block

        # Чат без системного промпта: память стоит нулевым сообщением, и ноль
        # здесь — не «врезки нет». На этом держится строгое сравнение у клиента.
        bare = new_agent(client, system="")
        frames = sse(client.post(f"/api/agents/{bare}/messages", json={"text": "голый"}).text)
        start = next(e for e in frames if e["event"] == "start")
        assert start["memory_at"] == 0, start["memory_at"]
        assert start["summary_at"] is None, start["summary_at"]
        assert start["resolved_messages"][0]["content"].startswith("[долговременная память]")

        # --- 4. Две врезки разом: память плюс факты, слот стратегии сдвинут ---
        #
        # Главное место дня. Память — слой не этого разговора, факты — выписка
        # про этот; они не отменяют друг друга и встают в промпт вдвоём.
        # Слот стратегии обязан сдвинуться на единицу: считай его по-старому —
        # и просмотр промпта подписал бы сводкой память, а фактами хвост.
        both = new_agent(client, system="СИС", strategy="facts", keep_last=2)
        for i in range(3):
            client.post(f"/api/agents/{both}/messages", json={"text": f"вопрос {i}"})
        _stub.reset()
        frames = sse(client.post(f"/api/agents/{both}/messages", json={"text": "вопрос 3"}).text)
        start = next(e for e in frames if e["event"] == "start")
        assert start["memory_at"] == 1, start["memory_at"]
        assert start["summary_at"] == 2, start["summary_at"]
        assert start["strategy"] == "facts", start["strategy"]
        prompt = start["resolved_messages"]
        assert prompt[start["memory_at"]]["content"].startswith("[долговременная память]"), prompt[1]
        assert prompt[start["summary_at"]]["content"].startswith("[факты о разговоре]"), prompt[2]
        assert [m["role"] for m in prompt] == ["system", "user", "user", "user", "assistant", "user"], prompt
        assert prompt[-1]["content"] == "вопрос 3", prompt[-1]

        # Служебные вызовы память не получают: у ведения рабочей памяти свой
        # системный промпт и своя работа — состояние **этого разговора**, а не
        # пересказ того, что мы и так знаем.
        extraction = _service_calls("facts")[-1]["messages"]
        assert not any("[долговременная память]" in m["content"] for m in extraction), extraction

        # И **оба** служебных вызова, а не один: у сжатия ровно та же
        # беда и та же цена — пересказ начала разговора начал бы
        # пересказывать долговременную память, и та вернулась бы в промпт
        # второй раз, уже сводкой, да ещё и искажённой. Смотреть на одно
        # извлечение значило бы держать обещание наполовину.
        folding = new_agent(client, system="СИС", strategy="summary", keep_last=2, compress_every=2)
        for i in range(3):
            client.post(f"/api/agents/{folding}/messages", json={"text": f"свернуть {i}"})
        compressed = _service_calls("summary")
        assert compressed, "сжатия не случилось — проверять нечего"
        assert not any(
            "[долговременная память]" in m["content"] for m in compressed[-1]["messages"]
        ), compressed[-1]["messages"]
        # А в самом обмене память на месте: служебный вызов её не получил
        # не потому, что её нет вовсе.
        assert any(
            "[долговременная память]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]

        # --- 5. Выключатель --------------------------------------------------
        #
        # Стенд щедрее сервера: его PATCH — `Object.assign`, он проглотит поле,
        # даже если `PATCHABLE` про него не знает. Поэтому спрашиваем сервер:
        # 200, чужое значение — 400 с перечислением, и следующий промпт другой.
        assert client.patch(f"/api/agents/{both}", json={"memory": "нет"}).status_code == 400
        off = client.patch(f"/api/agents/{both}", json={"memory": "off"})
        assert off.status_code == 200 and off.json()["memory"] == "off", off.text
        frames = sse(client.post(f"/api/agents/{both}/messages", json={"text": "без памяти"}).text)
        start = next(e for e in frames if e["event"] == "start")
        assert start["memory_at"] is None, start["memory_at"]
        # Слот стратегии вернулся на место: выключенная память сдвига не даёт.
        assert start["summary_at"] == 1, start["summary_at"]
        assert not any(
            "[долговременная память]" in m["content"] for m in start["resolved_messages"]
        ), start["resolved_messages"]
        assert client.patch(f"/api/agents/{both}", json={"memory": "on"}).status_code == 200

        # Выключатель на чат, а не на базу: соседний чат память по-прежнему
        # видит — иначе сравнить «с памятью» и «без» было бы не с чем.
        #
        # Заодно считаем чтения памяти: за обмен оно обязано быть **одно**.
        # Промпт и оба слота кадра `start` собираются в трёх разных точках,
        # и читай каждая из них хранилище сама — запись, добавленная соседней
        # вкладкой между этими чтениями, попала бы в промпт, но не в номера
        # врезок (или наоборот), и просмотр промпта подписал бы чужие роли.
        # Гонку здесь не воспроизвести, а вот саму цепочку «прочитали один
        # раз и передали дальше» видно счётчиком.
        _stub.reset()
        store = REGISTRY.store
        real_list, reads = store.list_memory, []

        def counted():
            reads.append(1)
            return real_list()

        store.list_memory = counted
        try:
            client.post(f"/api/agents/{plain}/messages", json={"text": "а у меня память есть"})
        finally:
            del store.list_memory
        assert len(reads) == 1, f"чтений памяти за обмен: {len(reads)}, а должно быть одно"
        assert any(
            "[долговременная память]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]

        # --- 6. Три слоя одной ручкой ----------------------------------------
        layers = client.get(f"/api/agents/{both}/memory").json()
        assert layers["short_term"]["messages"] == len(REGISTRY.require(both).history), layers
        assert layers["working"]["records"], layers["working"]
        assert layers["working"]["records"][0]["author"] == "agent", layers["working"]
        assert layers["working"]["upto"] > 0, layers["working"]
        assert layers["long_term"]["enabled"] is True, layers["long_term"]
        assert [r["seq"] for r in layers["long_term"]["records"]] == [
            r["seq"] for r in client.get("/api/memory").json()["records"]
        ], layers["long_term"]
        # Слой общий: у выключенного чата он тот же самый, разное только
        # положение выключателя.
        client.patch(f"/api/agents/{bare}", json={"memory": "off"})
        other = client.get(f"/api/agents/{bare}/memory").json()
        assert other["long_term"]["enabled"] is False, other["long_term"]
        assert other["long_term"]["records"] == layers["long_term"]["records"], other["long_term"]

        # --- 7. Хранится врозь ------------------------------------------------
        store = REGISTRY.store
        columns = {r["name"] for r in store.conn.execute("PRAGMA table_info(memory)")}
        assert columns == {"seq", "kind", "content", "at"}, columns
        assert "session_id" not in columns, "у глобального слоя завёлся владелец"
        for table in ("messages", "summaries", "working_memory"):
            for row in store.conn.execute(f"SELECT * FROM {table}"):
                text = " ".join(str(row[name]) for name in row.keys())
                assert "пишу на Kotlin" not in text, f"память утекла в {table}"

        # --- 8. Удаление по одной: номера не сдвигаются и не возвращаются -----
        #
        # Память — список записей с идентичностью, а не снимок: перенумеруй
        # её после удаления, и вторая вкладка, показывающая список с прошлой
        # минуты, удалила бы по старому номеру чужую запись.
        gone = client.delete(f"/api/memory/{decision['seq']}")
        assert gone.status_code == 200 and gone.json() == {"deleted": decision["seq"]}, gone.text
        assert client.delete(f"/api/memory/{decision['seq']}").status_code == 404
        left = [r["seq"] for r in client.get("/api/memory").json()["records"]]
        assert left == [profile["seq"], knowledge["seq"]], left

        # И отдельно — про **последний** номер: именно его обычная
        # `INTEGER PRIMARY KEY` выдала бы заново, сняв номер с удалённой
        # записи и отдав его новой. AUTOINCREMENT этого не делает, и разница
        # видна только здесь: удаление из середины номеров не двигает у любой
        # из двух схем.
        assert client.delete(f"/api/memory/{knowledge['seq']}").status_code == 200
        fresh = client.post("/api/memory", json={"kind": "knowledge", "content": "свежее"}).json()
        assert fresh["seq"] > knowledge["seq"], (fresh, knowledge)
        assert fresh["seq"] != decision["seq"], "номер удалённой записи выдан заново"

        # Ветвление память не копирует и копировать не должно: слой глобальный,
        # и ветка видит его через то же хранилище — не свою копию.
        branch = client.post(f"/api/agents/{plain}/fork", json={"at": 2}).json()["agents"][0]["id"]
        _stub.reset()
        client.post(f"/api/agents/{branch}/messages", json={"text": "вопрос ветки"})
        assert any(
            "профиль: пишу на Kotlin" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]

    # --- 9. Файл: чат уходит, память остаётся; clear() замыкает круг ---------
    _stub.install(reply="ок")
    path = _temp_db("memory")
    store = Store(path).init()
    kept = store.add_memory("profile", "пишу на Kotlin")
    spec = AgentSpec(label="с памятью", model="stub/model")
    agent = Agent(spec, store=store)
    asyncio.run(drain(agent.ask("вопрос")))
    assert "[долговременная память]" in agent.build_prompt("ещё")[0]["content"], "врезка не встала"

    # `forget()` забывает **разговор**: история, сводки и факты — его
    # содержимое, а долговременная память — нет. Ветка, забывшая историю,
    # осталась веткой того же родителя; чат, забывший разговор, по-прежнему
    # знает, на чём пишет собеседник.
    agent.forget()
    assert store.list_memory() == [kept], store.list_memory()
    assert "[долговременная память]" in agent.build_prompt("после forget")[0]["content"]

    # Удаление чата — тем более: слой не его, он в нём только читатель.
    doomed = Agent(spec, store=store)
    asyncio.run(drain(doomed.ask("вопрос удаляемого")))
    store.delete_session(doomed.id)
    assert store.list_memory() == [kept], store.list_memory()
    store.close()

    # Настоящее переоткрытие файла, а не тот же объект в памяти.
    again = Store(path).init()
    try:
        assert again.list_memory() == [kept], again.list_memory()
        revived = Agent(AgentSpec(label="пусто", model="x/y"), agent_id=agent.id, store=again)
        assert "[долговременная память]" in revived.build_prompt("после перезапуска")[0]["content"]

        # Агент без хранилища не падает — память у него просто пуста.
        homeless = Agent(AgentSpec(label="без базы", model="stub/model"))
        assert homeless.memory_items() == [], homeless.memory_items()
        assert homeless.memory_slot() is None, homeless.memory_slot()
        assert homeless.build_prompt("вопрос") == [{"role": "user", "content": "вопрос"}]

        # И единственный путь, который память стирает, — служебная очистка
        # базы. Забудь её там — и `kill_all()` перед каждой проверкой
        # оставлял бы врезку следующей, сдвигая ей роли в промпте.
        again.clear()
        assert again.list_memory() == [], again.list_memory()
        assert revived.memory_items() == [], revived.memory_items()
        assert revived.build_prompt("после очистки") == [
            {"role": "user", "content": "после очистки"}
        ], revived.build_prompt("после очистки")
    finally:
        again.close()

    return (
        "пустая память неотличима от отсутствующей; род записи без умолчания, "
        "шесть кривых тел дали 400; врезка ролью user с подписями, слот 1 "
        "с системным промптом и 0 без; память плюс факты — врезки две, слот "
        "стратегии сдвинут на 2; ни извлечение, ни сжатие памяти не получили; "
        "за обмен она прочитана ровно один раз; выключатель чата уносит врезку "
        "и возвращает слот на 1; удаление по одной номера не сдвинуло "
        "и не вернуло; forget(), удаление чата и переоткрытие файла память "
        "пережила, clear() — нет"
    )


@check("токены служебных вызовов попадают в итог по чату")
def check_service_tokens_counted():
    """Служебный вызов — сжатие или извлечение фактов — тоже уехал в модель и
    тоже оплачен. Экономия, не вычитающая его стоимость, — враньё, поэтому оба
    входят в `usage_total` отдельными слагаемыми: в истории их нет, и сами
    собой они туда не попадут.

    У фактов это вдвойне: вызов идёт на **каждом** обмене, и потерянная цена
    извлечения — не округление, а вторая половина счёта.

    Числа служебных вызовов здесь заведомо больше всех остальных вместе
    взятых: потеряйся они, сумма разошлась бы на порядок.
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
        agent_id = new_agent(client, strategy="summary", keep_last=KEEP, compress_every=EVERY)
        for i in range(9):
            client.post(f"/api/agents/{agent_id}/messages", json={"text": f"вопрос {i}"})
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

    # --- то же самое для рабочей памяти, и вызовов не один, а девять --------
    facts_calls: set = set()

    def facts_reply(messages, index):
        if _service_kind(messages) == "facts":
            facts_calls.add(index)
            return _working_edit(messages, f"решение: обмен {index}")
        return f"ответ {index}"

    def facts_usage(index):
        if index in facts_calls:
            return _usage(700, 30, 730, 0.002)
        return _usage(1, 1, 2, 0.000001)

    _stub.reset()
    _stub.install(reply=facts_reply, usage=facts_usage)
    with TestClient(main.app) as client:
        with_facts = new_agent(client, strategy="facts", keep_last=KEEP)
        for i in range(9):
            client.post(f"/api/agents/{with_facts}/messages", json={"text": f"вопрос {i}"})
        facts_body = client.get(f"/api/agents/{with_facts}").json()

    assert len(facts_calls) == 9, facts_calls
    facts_total = facts_body["usage_total"]
    assert facts_total["prompt_tokens"] == 9 * 1 + 9 * 700, facts_total
    assert facts_total["completion_tokens"] == 9 * 1 + 9 * 30, facts_total
    assert facts_total["total_tokens"] == 9 * 2 + 9 * 730, facts_total
    assert round(facts_total["cost_usd"], 8) == round(9 * 0.000001 + 9 * 0.002, 8), facts_total
    # Правки ложатся по записям, и держать числа отдельного вызова негде —
    # значит они накопленные: храни состояние только последний вызов, в итог
    # попала бы девятая часть того, что заплачено.
    agent = REGISTRY.require(with_facts)
    assert agent.working["metrics"]["total_tokens"] == 9 * 730, agent.working["metrics"]
    # Ведение памяти не растит счётчик сообщений: записи живут вне истории,
    # и ни в счётчике, ни карточкой в ленте их нет.
    assert facts_body["history_len"] == 18, facts_body["history_len"]
    assert len([t for t in facts_body["transcript"] if t["role"] == "assistant"]) == 9, (
        "выписка попала в ленту"
    )
    return (
        f"итог {total['total_tokens']} токенов = {9 * 2} за девять обменов "
        f"плюс 50400 за сжатие; у фактов {facts_total['total_tokens']} = {9 * 2} "
        f"плюс {9 * 730} за девять извлечений; сообщений по-прежнему {body['history_len']}"
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
        # И в родство — тем же явным путём. Своих текстов у него нет, но
        # `parent_id` это строка из запроса, и обещание про любой строковый
        # параметр любого запроса касается её наравне с репликой.
        store.save_branch(agent.id, parent_id=f"ag_{key}", forked_at=2)
        # И в рабочую память — тем же явным путём: записи ведёт модель, и ключ
        # из реплики попадёт в них так же легко, как в пересказ. Пишут её обе
        # стороны, поэтому и путей здесь три: своя запись, чужая и состояние
        # извлечения — таблицы новые, а обещание то же.
        store.add_working(agent.id, "goal", f"цель с ключом {key}", "agent")
        # Правка — по **второй** записи, а не по первой: перепиши она
        # засветившуюся, утечка добавления затёрлась бы чистой правкой,
        # и проверка стерегла бы один путь вместо двух.
        poked = store.add_working(agent.id, "question", "запись под правку", "agent")
        store.update_working(
            agent.id, poked["seq"], kind="limit", content=f"правка с ключом {key}",
            author="human",
        )
        store.save_working_state(agent.id, upto=2, metrics={"заметка": key})

        # И в долговременную память — тем же явным путём. Запись в неё делает
        # человек руками, а ключ он вставляет туда ровно так же, как в реплику:
        # перепутав окно. Слой глобальный и удаление чата его не чистит —
        # утёкший сюда ключ пережил бы и сам чат.
        store.add_memory("knowledge", f"ключ от панели: {key}")

        # А это — про колонки, которых ещё нет: любая запись идёт через
        # транзакцию, и параметр чистится независимо от того, вспомнил ли
        # автор про redact() в этом конкретном методе.
        with store.tx() as conn:
            conn.execute("UPDATE sessions SET label = ? WHERE id = ?", (key, agent.id))
            conn.execute("INSERT INTO meta (key, value) VALUES ('ловушка', ?)", (key,))

        leaked = []
        for table in (
            "sessions", "messages", "meta", "summaries", "facts", "branches", "memory",
            "working_memory", "working_state",
        ):
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
            conn.executemany(
                "INSERT INTO facts (session_id, seq, key, value, upto, at) "
                "VALUES (?, ?, ?, ?, 2, 0)",
                [("s", 0, key, key)],
            )
            conn.executemany(
                "INSERT INTO working_memory (session_id, kind, content, author, at) "
                "VALUES (?, 'goal', ?, ?, 0)",
                [("s", key, key)],
            )
            conn.execute(
                "INSERT INTO working_state (session_id, upto, metrics, at) "
                "VALUES (?, 2, ?, 0)",
                ("s", key),
            )
            conn.executemany(
                "INSERT INTO branches (session_id, parent_id, forked_at, at) "
                "VALUES (?, ?, 2, 0)",
                [("s", key)],
            )
            conn.execute(
                "INSERT INTO memory (kind, content, at) VALUES ('knowledge', ?, 0)", (key,)
            )
        leaked = []
        for table in (
            "sessions", "messages", "meta", "summaries", "facts", "branches", "memory",
            "working_memory", "working_state",
        ):
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
