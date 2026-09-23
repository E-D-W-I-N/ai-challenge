"""Ядро проверок Дней 6, 7 и 8 — без сети, без ключа, без живых вызовов к LLM.

    .venv/bin/python checks/run_checks.py

Каждая проверка стережёт одно обещание продукта: пункт задания одного из трёх
дней или сквозное свойство. Отдельные скрипты (`spawn_100.py`, `restart.py`,
`two_processes.py`) запускаются отсюда же.
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
from app.schema import (  # noqa: E402
    MOVE_COMMANDS,
    MOVES,
    RESUME,
    STAGE_EXPECTS,
    STAGE_LABELS,
    STAGE_RULES,
    STAGES,
    TRANSITIONS,
    AgentSpec,
)
from app.store import Store, StoreBusyError  # noqa: E402

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
    """Чем был этот вызов: `summary` — сжатие, `None` — обычный обмен.
    У служебного вызова свой системный промпт, по нему и различаем.

    Вопрос остался «какой это вызов?», хотя ответов снова два: вызовов
    было три вида, потом два, и счётчики чужих проверок каждый раз плыли.
    Спрашивать «это сжатие?» — значит заводить второй ответ заново при
    первом же новом вызове."""
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
    """Ответ, который зависит от **содержимого запроса**, а не от его номера.

    Заглушка не модель и моделью не притворяется. Но врезку памяти она читает
    ровно там же, где прочитала бы её модель, — в тексте промпта, — и этого
    довольно, чтобы «память влияет на ответы» стало утверждением о самом
    ответе, а не о составе запроса. Всё, чего проверка не вправе требовать
    от настоящей модели, — что ответ будет именно этими словами; чего она
    вправе требовать и здесь, и у живого прогона, — что ответ **разойдётся**.

    Читаются обе врезки: слои разные, а показывают они себя одинаково —
    вписанная запись доезжает до ответа.
    """
    knows = any(
        "пишу на Kotlin" in m.get("content", "")
        or "цель: пример на Kotlin" in m.get("content", "")
        for m in messages
    )
    return "Держи пример на Kotlin." if knows else "На каком языке показать пример?"


def _profile_aware(messages, index):
    """Ответ, который зависит от **профиля** в запросе, а не от его номера.

    Профиль заглушка читает там же, где прочитала бы его модель, — в блоке
    `[как отвечать]` системного сообщения, — и этого довольно, чтобы «разные
    профили дают разные ответы» стало утверждением об ответе, а не о составе
    запроса. Словами живой модели заглушка не притворяется: обещать она
    вправе только то, что разница доезжает до ответа, а не теряется
    по дороге. Довод тот же, что у `_memory_aware`.
    """
    head = messages[0].get("content", "") if messages else ""
    if "стиль: кратко, на ты" in head:
        return "Смотри: начни с макета."
    if "стиль: подробно, с примерами" in head:
        return "Давайте разберём по шагам, с примерами: начните с макета."
    return "С чего вам удобнее начать?"


def _invariant_aware(messages, index):
    """Ответ, который зависит от **инварианта** в запросе, а не от его номера.

    Инвариант заглушка читает там же, где прочитала бы его модель, — в блоке
    `[чего нельзя]` системного сообщения, — и этого довольно, чтобы «ассистент
    отказывается предлагать запрещённое» стало утверждением об ответе, а не
    о составе запроса. Довод тот же, что у `_memory_aware` и `_profile_aware`.

    Вид назван **исключительным** для слоя: «решение» лежит и в долговременной
    памяти, и в рабочей, и узнавай заглушка его — она отвечала бы так же
    на чужую запись, а проверка была бы зелена по неверной причине.
    """
    head = messages[0].get("content", "") if messages else ""
    if "ограничение стека: бэкенд только на Python" in head:
        return "Только Python: нарушается инвариант «ограничение стека»."
    return "Возьмите Java со Spring — обычный выбор под такую задачу."


def _stage_aware(messages, index):
    """Ответ, который зависит от **блока жизненного цикла** в запросе.

    Заглушка читает автомат там же, где прочитала бы его модель, — в блоке
    `[жизненный цикл задачи]` системного сообщения, — и отказ собирает
    из него же: этап берёт из строки «сейчас здесь», команду — из хода,
    ведущего к «выполнению». Этого довольно, чтобы «ассистент не делает
    работу чужого этапа» стало утверждением об **ответе**, а не о составе
    запроса. Довод тот же, что у `_memory_aware` и `_invariant_aware`.

    Блока нет — работа делается: знание машины и есть вся разница между
    двумя ответами.
    """
    head = messages[0].get("content", "") if messages else ""
    asked = messages[-1].get("content", "") if messages else ""
    here = next((line for line in head.splitlines() if " — сейчас здесь: " in line), "")
    stage, _, moves = here.partition(" — сейчас здесь: ")
    if "напиши код" not in asked or stage == "выполнение":
        return f"ответ {index}"
    if not here:
        return "Держи код: def main(): ..."
    way = next((m for m in moves.split("; ") if m.endswith("→ выполнение")), "")
    command = way.partition("(")[2].partition(")")[0]
    return (
        f"Кода сейчас не будет: это работа этапа «выполнение», "
        f"а мы на этапе «{stage}». "
        + (f"Перейдите {command}." if command else "Отсюда туда хода нет.")
    )


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
#
# Помощники ниже **ничего не утверждают** — утверждение, уехавшее из проверки
# в помощник, стало бы самопроверкой стенда, а их в этом наборе нет. И сюда же
# не уезжает ничего, что проверка **измеряет**: число обменов, счётчики вызовов
# к заглушке и сами сравнения остаются на виду в проверке.
#
# Своего помощника на «подменить и вернуть» здесь нет намеренно: это делают
# `patch.object` и `patch.dict` из стандартной библиотеки. Возврат у подмены
# двоякий — атрибут модуля кладут обратно, а подменённый метод класса надо
# снять, — и ошибиться в этом руками проще, чем взять готовое.


def _frames(client, agent_id: str, text: str) -> list[dict]:
    """Обмен через веб-слой, разобранный в список кадров SSE."""
    return sse(client.post(f"/api/agents/{agent_id}/messages", json={"text": text}).text)


def _frame(frames, event: str) -> dict:
    """Первый кадр названного события."""
    return next(e for e in frames if e["event"] == event)


def _talk(client, agent_id: str, turns, text: str = "вопрос {i}") -> list:
    """Обмены подряд через веб-слой; отдаёт ответы ручки. Сколько их —
    остаётся у проверки: это её счёт, а не подробность сцены. Число значит
    «столько с нуля», готовый `range` — «как просили»: продолжение чата
    нумеруется дальше, и номер уезжает в текст вопроса."""
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
    Закрывается и при падении: незакрытое соединение держит WAL, и соседка
    на том же файле получила бы чужую блокировку вместо своего падения."""
    store = Store(path).init()
    try:
        yield store
    finally:
        store.close()


@contextlib.contextmanager
def _restarted(store, agent_id: str):
    """Перезапуск программы целиком: закрыть файл, открыть заново и поднять
    из него чат. Конфиг у поднятого заведомо пустой — всё, что он о себе
    знает, приехало из базы."""
    path = store.path
    store.close()
    with _reopened(path) as fresh:
        yield fresh, agent_module.Agent(
            AgentSpec(label="пусто", model="x/y"), agent_id=agent_id, store=fresh
        )


ALL_TABLES = (
    "sessions", "messages", "meta", "summaries", "branches", "memory",
    "working_memory", "task_state", "profile", "invariants",
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
    """Три правила тела запроса — списком нарушенных, словами. Проверяются
    они на путях **обмена** и на обоих служебных вызовах и раньше стояли
    дословной копией в каждом из трёх мест; копии расходятся молча — поправят
    два места из трёх, и третий путь уедет к провайдеру без правила, ничего
    не уронив. Утверждение осталось в проверках, здесь только его текст."""
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
    "invariants":      (False, False, True),
}
"""Чего после какого пути очистки не остаётся. Каждый `True` — место, где
`DELETE` обязан стоять руками: каскада нет, внешние ключи не объявлены.

`False` — не пробелы в таблице, а вторая её половина, и такая же
обязательная. Состояние задачи уносится со всех трёх: правило этапа уезжает **системным**
сообщением, и забытый разговор оставил бы следующему чужое «не продолжай».
Родство переживает `forget()`: это не содержимое разговора,
а то, откуда чат взялся, и ветка, забывшая историю, осталась веткой того же
родителя. Долговременная память переживает и `forget()`, и удаление чата:
чат ей не владелец, а читатель, — и стирает её ровно один путь, `clear()`,
потому что он не «ещё одна таблица чата», а вся база разом. Профиль — тем же
рядом и по тому же доводу: он один на всю базу и про человека, а не про чат,
и забытый разговор своего собеседника не меняет. Инварианты — третьим тем же
рядом: архитектура продукта не меняется от того, в каком чате о ней спросили,
и забытый разговор её не отменяет. `clear()` все три всё-таки стирает, и это
критично вдвойне: и профиль, и инварианты заводят **системное** сообщение,
и утёкшее в чужую проверку сдвинуло бы там номера всех врезок разом. Проверка,
потребовавшая бы чистки везде, сломала бы задуманное так же, как забытый
`DELETE`.
"""


def _filled_chat(store, label: str):
    """Чат, у которого непусты **все семь** слоёв сразу: сводка, рабочая
    память, состояние задачи, родство, долговременная память, профиль
    и инварианты.

    Обменов три: порог сжатия при `keep_last=2` и `compress_every=2`
    набирается только на третьем. Запись рабочей памяти и задачу кладёт
    человек — других путей в эти слои нет. Родство пишется прямо
    в хранилище: ветвить настоящего родителя ради одной строки незачем,
    а `parent_id` тут и не разглядывается.
    """
    chat = agent_module.Agent(
        AgentSpec(label=label, model="stub/model", strategy="summary",
                  keep_last=2, compress_every=2),
        store=store,
    )
    _ask(chat, 3, label + " {i}")
    chat.add_working_record("goal", f"цель чата {label}")
    chat.start_task(f"задача чата {label}")
    store.save_branch(chat.id, parent_id="ag_00001", forked_at=2)
    store.add_memory("knowledge", f"запись рядом с чатом {label}")
    store.save_profile({"style": f"кратко, рядом с чатом {label}"})
    store.add_invariant("stack", f"инвариант рядом с чатом {label}", ["Java"])
    return chat


def _leftovers(store, session_id: str) -> dict:
    """Что осталось от чата в каждом слое: пустое значение — слой унесён."""
    return {
        "summaries": store.load_summaries(session_id),
        "working": store.list_working(session_id),
        "task": store.load_task(session_id),
        "branches": store.load_branch(session_id),
        "long_term": store.list_memory(),
        "profile": store.load_profile(),
        "invariants": store.list_invariants(),
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


@check("обрезка бывает только выбранная и всегда названная: full, window, summary")
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

    Вариантов три, и все три про одно: сколько реплик уедет дословно. Врезки
    памяти отсюда ушли — их ведёт агент при любом варианте, и разбирается
    с ними своя проверка. Поэтому у чатов здесь память **выключена**: они
    про обрезку, и второе обращение к модели за обмен сказало бы про неё
    ровно ничего. Кроме одного раздела — того, где окно встречается
    с прочитанным.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")

    # --- full: не срезается ничего, и это умолчание ---------------------------
    #
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

    # 3. Числа заданы, а стратегия так и осталась `full` — не срезается **всё
    # равно ничего**. Это ровно тот случай, в котором живут чаты Дня 9 после
    # обновления: ключа `strategy` в их конфиге нет, и поднимаются они с
    # `full`. Срежь их окно или сводка молча — обрезку выбрал бы не
    # пользователь, а версия сервера.
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
    #
    # Зажима «не дальше прочитанного» здесь больше нет, и это не упрощение
    # задним числом: он стерёг реплику, которую окно выбросило бы раньше,
    # чем её прочитал служебный вызов на ведение памяти. Вызова не стало —
    # в рабочую память пишет только человек, — и стеречь стало нечего:
    # записи в ней не зависят от того, докуда дошёл разговор.
    #
    # Зато видно главное различие слоёв: окно **отбрасывает** начало
    # разговора, а вписанная руками цель уезжает в модель всё равно.
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
        _talk(client, folded_id, range(9, 14))
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
        assert all(name in bad.text for name in ("full", "window", "summary")), bad.text
        assert "facts" not in bad.text, "«Факты» остались стратегией"
        assert folded.spec.strategy == "summary", folded.spec.strategy

    # 9. Граница сворачивания не рвёт пару: история идёт парами, обе реплики
    # пишутся разом, и свёрнутый вопрос без своего ответа сделал бы хвост
    # бессмысленным. При нечётном окне граница округляется вниз до чётного.
    odd = _fill(
        _bare("нечёт", strategy="summary", keep_last=5, compress_every=EVERY), 20
    )
    asyncio.run(odd.compress(odd.spec))
    odd_cover = odd.summary_cover()
    assert odd_cover == 14, odd_cover
    assert odd_cover % 2 == 0, f"граница разорвала пару: свёрнуто {odd_cover} реплик"
    assert odd.history[odd_cover].role == "user", odd.history[odd_cover].role

    # 10. Перегенерация снимает пару **с конца**, а сводка покрывает начало:
    # на коротком чате с нулевым окном они встречаются, и `upto` оказывается
    # больше истории. Зажатый длиной, он остаётся правдой; незажатый заявил
    # бы, что свёрнуто реплик больше, чем в чате было.
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
    # Память в этом чате выключена: вызов на её ведение — второе обращение
    # к модели за обмен, и «в модель ушёл ровно один вызов» перестало бы
    # говорить про то, ради чего проверка написана.
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
        # Память обоим выключена: проверка про экономию сжатия, и вызов
        # на ведение памяти — лишнее обращение к модели в обеих колонках
        # сравнения сразу.
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

    # Вызов на сжатие — такой же вызов к модели, и три правила тела на нём
    # тоже. Особенно третье: собери сводку провайдер со включённым
    # `context-compression`, и он молча выбросил бы середину того самого
    # куска, который мы отдали пересказать, — сводка вышла бы дырявой,
    # а узнать об этом было бы неоткуда.
    folding_body = _service_calls("summary")[-1]["payload"]
    assert not _body_rules(folding_body), f"вызов на сжатие: {_body_rules(folding_body)}"

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
    folding_frame = _frame(events[folded][8], "compressing")
    assert folding_frame.get("strategy") == "summary", folding_frame

    # И сводку в промпте клиенту показывает сервер, а не разбор текста:
    # `summary_at` — её место в `resolved_messages`. Порядок сборки промпта
    # живёт в `build_prompt`, и вторая его копия в браузере разошлась бы молча.
    def start_of(agent_id, index):
        return _frame(events[agent_id][index], "start")

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


@check("сводка живёт отдельно от истории, а очистка уносит ровно свои слои")
def check_summary_apart_and_cleanup():
    """Половины здесь две, и обе про одно: у сводки своя таблица.

    **Первая** — сводка лежит не строкой в `messages`: `save_history` на
    каждой записи делает `DELETE FROM messages`, и лежи сводка там, её стирал
    бы каждый следующий обмен. Доказательство — настоящее переоткрытие файла
    и обмен уже после него: сводка та же, история полная, `seq` без дыр,
    и текста сводки в репликах нет.

    **Вторая** — плата за отдельные таблицы: каскада нет, и каждый слой
    обязан уноситься с каждого пути очистки руками. Пути и слои сведены
    в таблицу (`CLEANUP_TABLE`), там же записано, почему одни клетки
    уносят, а другие **оставляют**. Раньше клетки проверялись врозь, по одной
    в четырёх проверках; здесь они проходятся разом и на чате, у которого
    непусты все семь слоёв, — утверждение об очистке обязано стоять
    на непустом значении, иначе оно показывает покрытие, которого нет.
    """
    from app.agent import Agent

    _stub.install(reply=_service_aware)
    path = _temp_db("summary-restart")
    store = Store(path).init()
    # Память выключена: первая половина про сводку, и врезка рабочей памяти
    # стояла бы в промпте перед ней, сдвигая всё, на что проверка смотрит
    # по номеру. У чатов второй половины она включена — им нужен непустой слой.
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
        #
        # Путь получает свой чат: `clear()` стирает базу целиком, и общий чат
        # на три пути оставил бы последним двум пустую сцену. Идут они в том
        # же порядке, что в `CLEANUP_PATHS`, — от самого узкого к самому
        # широкому, и `clear()` последним.
        for column, (path_name, wipe) in enumerate(CLEANUP_PATHS.items()):
            chat = _filled_chat(again, path_name)
            full = _leftovers(again, chat.id)
            # Сцена непуста во всех семи слоях: без этого «после очистки
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

        # И в памяти объекта сводка тоже пуста, а не только в файле.
        # `summary_cover` зажат длиной истории: оставь её в памяти — и на
        # первых же репликах нового разговора она снова стала бы действующей,
        # накрыв собой его начало. Поэтому смотрим не на пустую таблицу,
        # а на отросшую заново историю: она обязана уехать в модель целиком.
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

    Пишет в неё только человек, руками, через ручки под чатом. Служебный
    вызов, который вёл её сам, прожил один день: на живом прогоне он
    записывал через раз, а починить его вслепую, без ключа, нельзя —
    и автоматическая запись ушла из обоих слоёв памяти целиком. Отсюда
    и здешний главный вопрос: **к модели за память не ходят ни разу**.

    Части четыре: врезка (встаёт при любой обрезке, своим слотом и своей
    подписью), ручки (тип обязателен и без умолчания, номер устойчив,
    чужой чат не тронуть), разница слоёв (окно отбросило начало разговора,
    а вписанная цель уехала в модель всё равно — ровно то, ради чего слой
    и заведён) и файл (записи переживают перезапуск, а три пути очистки —
    нет).
    """
    from app.agent import Agent

    # --- 1. Живой маршрут: врезка при окне, хвост как есть ------------------
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # Вариант обрезки здесь — окно, самый недоверчивый к памяти: он
        # начало **отбрасывает**. Врезка всё равно встаёт — она не про
        # историю, а про задачу.
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

        # К модели сходили ровно девять раз — по разу на обмен. Служебного
        # вызова за рабочей памятью нет ни одного: её ведёт человек, и цена
        # чата от наличия этого слоя не выросла ни на токен.
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
        # Врезка едет ролью `user` с подписью, а не системной репликой:
        # системный промпт живёт ровно в одном месте — `spec.system`, — и
        # второй системной репликой чат перестал бы быть тем, что настроили.
        # Тип записи подписан по-русски — той же картой, какой подписана
        # долговременная память и какой подписан список во вкладке.
        # Закрывающая скобка нейтральная: за врезкой рабочей памяти может
        # встать сводка начала разговора, и «дальше — последние сообщения
        # как есть» соврало бы ровно там, где врезок в промпте три.
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
        #
        # Разбор тела — по образцу долговременной памяти, и тип здесь так же
        # без умолчания: «явно выбирать, что и куда сохраняется» перестало бы
        # быть работой человека, подставь сервер тип за него.
        listed = client.get(url).json()
        assert listed["total"] == 1, listed
        assert listed["records"][0]["seq"] == kept["seq"], listed["records"]
        # Автора у записи нет вовсе: писать в этот слой больше некому,
        # кроме человека, и поле, у которого одно значение, не различает
        # ничего.
        assert set(listed["records"][0]) == {"seq", "kind", "content", "at"}, listed["records"][0]

        bad = client.post(url, json={"content": "без типа"})
        assert bad.status_code == 400 and "goal" in bad.text, bad.text
        assert client.post(url, json={"kind": "цель", "content": "подписью"}).status_code == 400
        assert client.post(url, json={"kind": "profile", "content": "чужой слой"}).status_code == 400
        assert client.post(url, json={"kind": "goal", "content": "   "}).status_code == 400
        # Номер, время и автора сервер не спрашивает: тело, которое их
        # присылает, просит не то, что ручка делает. Автор здесь не случайное
        # лишнее поле — его слал бы старый клиент, и принять его значило бы
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
        # Тип правится тем же телом и **в одиночку**: запись не того типа
        # чинилась бы иначе только удалением с заведением заново. Номер
        # и текст при этом стоят: правили не их.
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
        # это номер. Снимок Дня 10 собирался словарём по ключу, и вторая
        # «цель» вытесняла первую молча.
        twin = client.post(url, json={"kind": "limit", "content": "срок до мая"})
        assert twin.status_code == 200, twin.text
        twin = twin.json()
        assert twin["seq"] > human["seq"], (twin, human)
        assert [(r["kind"], r["content"]) for r in client.get(url).json()["records"]] == [
            ("decision", "берём Kotlin"), ("limit", "бюджет 200к"), ("limit", "срок до мая"),
        ], client.get(url).json()["records"]

        # Удаление плюс новая запись: номер удалённой не достаётся следующей.
        # Обычный `INTEGER PRIMARY KEY` снял бы номер с последней и отдал его
        # новой — и правка «по номеру три» попала бы не в ту запись.
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
    #
    # Ради этого слой и заведён, и видно это ровно здесь: окно отбрасывает
    # начало разговора — старых реплик в промпте нет вовсе, — а вписанная
    # руками цель уезжает в модель всё равно. Краткосрочная память живёт
    # длиной окна, рабочая — задачей.
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

        # Записей нет среди реплик: они в своей таблице, а не в ленте. Лежи они
        # строкой в `messages`, их стирал бы каждый следующий обмен —
        # `save_history` начинается с `DELETE FROM messages`.
        contents = [r[2] for r in again.message_rows(agent_id)]
        assert not any("[факты о разговоре]" in c for c in contents), contents

        asyncio.run(drain(revived.ask("вопрос после перезапуска")))
        assert again.list_working(agent_id) == before, again.list_working(agent_id)
        assert len(revived.history) == 20, len(revived.history)

        # Чистка чата уносит и рабочую память: каскада в схеме нет. Зажима
        # по длине истории у неё нет вовсе — врезка встаёт в промпт, пока
        # в памяти есть хоть одна запись, — и забытый разговор оставил бы
        # свои цели и ограничения следующему.
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

        # Удаление чата и очистка базы уносят рабочую память тем же порядком,
        # и обе эти клетки — в таблице «слой × путь очистки»
        # (`check_summary_apart_and_cleanup`), на чате, у которого непусты все
        # четыре слоя разом. Здесь остаётся то, чего в таблице нет: страж
        # чужого чата. Номера записей сквозные на всю базу, и через агента
        # чужой номер не придёт никогда, а прямым вызовом хранилища — вот так.
        # Без `session_id` в `WHERE` правка ушла бы в соседний разговор.
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
    """Ветвление Дня 10: «сохраните checkpoint, создайте 2 ветки от одного
    места, продолжите диалог в каждой независимо, переключайтесь между ними».

    Ветка — **отдельный чат**: своя строка в `sessions`, свой id от базы, своя
    история под своим `session_id`. Схему `messages` ветвление не тронуло
    вовсе. Отсюда и разделы проверки:

    * **унесено ровно просимое** — первые `at` сообщений и копия конфига,
      с номерами от нуля и без дыр;
    * **сводка едет только своя** — она заменяет собой начало истории,
      и заменять им можно ровно то начало, которое в ветке есть: ветка,
      унёсшая половину разговора, не вправе унести сводку про весь.
      А записи рабочей памяти едут все: их вписал человек, они ничего
      не заменяют, и задача у ветки та же;
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
        # Память выключена: этот раздел про копию истории и конфига, и
        # врезка встала бы в каждый промпт, который здесь сверяется дословно.
        # Копию самой памяти разбирает раздел ниже, там она включена.
        parent = new_agent(
            client, label="родитель", system="СИС", temperature=0.5,
            extra_body={"provider": {"order": ["stub"]}},
        )
        _talk(client, parent, 3)
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

    # И продолжение родителя ветку не трогает **и на уровне врезки**: список
    # сводок у неё свой. Общий дал бы ветке сводку про реплики, которых в ней
    # нет, — молча, следующим сворачиванием родителя.
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
    # `at`. Раньше была — по `upto`, докуда её дочитал служебный вызов: записи
    # были пересказом прочитанных реплик, и ветка, унёсшая половину разговора,
    # не вправе была унести память про весь. Теперь их вписал человек: это его
    # слова о задаче, они не заменяют собой ни одной реплики, а ветка
    # продолжает ту же задачу.
    early = reg.fork(listing, 2, label="ветка от второй реплики")
    late = reg.fork(listing, 10, label="ветка от конца")
    for branch in (early, late):
        assert [(r["kind"], r["content"]) for r in branch.working] == said, branch.working
        assert store.list_working(branch.id) == branch.working, store.list_working(branch.id)
        assert branch.build_prompt("ещё")[0]["content"].startswith("[факты о разговоре]"), (
            "память не встала в промпт ветки"
        )
    # Номера у копий свои: номер принадлежит одному чату, и две записи под
    # одним номером в разных разговорах — ровно та путаница, из-за которой
    # он и стал сквозным на всю базу.
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
        # откуда чат взялся. Ветка, забывшая историю, осталась веткой — и это
        # одна из трёх клеток, где очистка обязана слой **оставить**. Путей
        # у родства два, а не три, и все три его клетки вместе с остальными
        # девятью проходит `check_summary_apart_and_cleanup`; здесь остаётся
        # то, чего в таблице нет: забытая история и живая пометка рядом.
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
    """День 11: три слоя памяти, и третий из них — долговременный.

    Два слоя были и раньше, оба привязаны к чату: краткосрочная — сама
    история (`messages`), рабочая — состояние задачи и сводки
    (`working_memory`, `summaries`). Третий отличается всем сразу: таблица **без**
    `session_id`, наполняется **только руками**, и чат ему читатель, а не
    владелец — удаление чата и `forget()` память не трогают.

    Смотрим по порядку: пустая память неотличима от отсутствующей; ручки
    и их валидация (тип записи выбирает человек); врезка в промпте и её
    место; **три врезки разом** — память, рабочая память и сводка, где слот
    каждой следующей сдвинут предыдущими; хранение врозь; удаление по одной
    без перенумерации и без переиспользования номера; переживание `forget()`,
    удаления чата и переоткрытия файла; `clear()`, который круг замыкает.
    """
    from app.agent import Agent

    # Пока разбирается пустота, служебному вызову отвечаем так, чтобы
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Пустая память неотличима от отсутствующей --------------------
        #
        # Пусто в обоих слоях сразу: врезки нет вовсе, а не пустая. Иначе
        # каждая проверка с точной последовательностью ролей поехала бы
        # на сообщение.
        assert client.get("/api/memory").json() == {"total": 0, "records": []}, "память не пуста"
        plain = new_agent(client, system="СИС")
        start = _frame(_frames(client, plain, "первый"), "start")
        assert start["memory_at"] is None, start["memory_at"]
        assert start["working_at"] is None, start["working_at"]
        assert [m["role"] for m in _stub.CALLS[-1]["messages"]] == ["system", "user"], _stub.CALLS[-1]

        # --- 2. Ручки: тип записи выбирает человек, а не сервер ---------------
        #
        # `kind` обязателен и без умолчания. Подставь сервер «knowledge» на
        # пропущенный ключ — и «явно выбирать, что и куда сохраняется» стало
        # бы «сервер выбрал за тебя», то есть ровно тем, чего задание просит
        # избежать.
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
        #
        # Роль `user` с подписью, а не `system`: системный промпт живёт ровно
        # в одном месте — `spec.system`, — и вторая системная реплика сделала
        # бы чат не тем, что настроили. Тип подписан по-русски, одной картой
        # с интерфейсом.
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
        #
        # Главное место дня. Долговременная память — слой не этого разговора,
        # рабочая — состояние этой задачи, сводка — свёрнутое начало самого
        # разговора. Ни одна не отменяет другую, и в одном промпте они стоят
        # втроём, ровно в этом порядке: от общего к частному. Слот каждой
        # следующей сдвинут теми, что встали перед ней, — считай его
        # по-старому, и просмотр промпта подписал бы памятью сводку.
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
        # не ходят вовсе, её пишет человек. Кадр о паузе — на этот вызов,
        # и он назван: строка состояния берёт текст оттуда.
        went = [_service_kind(call["messages"]) for call in _stub.CALLS]
        assert went == ["summary", None], went
        promised = [e["strategy"] for e in frames if e["event"] == "compressing"]
        assert promised == ["summary"], promised

        # **Сжатию память не достаётся ни одна**: оно пересказывает разговор,
        # и то, что записано о собеседнике или о задаче, ему ни к чему. Попади
        # память в пересказ, она вернулась бы в промпт вторым экземпляром,
        # да ещё и искажённой. Служебный промпт памяти не получает — и теперь
        # это снова один инвариант, без половинок: второй служебный вызов,
        # ради которого он расщеплялся, ушёл вместе со своим слоем.
        folding = _service_calls("summary")[-1]["messages"]
        assert not any("[долговременная память]" in m["content"] for m in folding), folding
        assert not any("о собеседнике: пишу на Kotlin" in m["content"] for m in folding), folding
        assert not any("цель: собрать ТЗ" in m["content"] for m in folding), folding

        # Память читается **один раз на обмен** и раздаётся сразу троим —
        # сборке промпта и обоим слотам кадра `start`. Читай каждый из троих
        # хранилище сам — запись, добавленная соседней вкладкой между этими
        # чтениями, попала бы в промпт, но не в номера врезок (или наоборот),
        # и просмотр промпта подписал бы чужие роли. Гонку здесь
        # не воспроизвести, а вот саму цепочку «прочитали один раз и передали
        # дальше» видно счётчиком. Чтений было два, пока к памяти ходил
        # служебный вызов; вызова не стало — осталось одно.
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
        # Сводки — в краткосрочном разделе, а не в рабочей памяти: сводка
        # не запомненное, а чем заменено то, что не уехало дословно. Выключи
        # сворачивание — не пропадёт ничего, она соберётся заново.
        assert layers["short_term"]["summaries"], layers["short_term"]
        assert layers["short_term"]["summaries"][0]["upto"] == 2, layers["short_term"]
        assert "summaries" not in layers["working"], layers["working"]
        assert [(r["kind"], r["content"]) for r in layers["working"]["records"]] == [
            ("goal", "собрать ТЗ")
        ], layers["working"]
        assert [r["seq"] for r in layers["long_term"]["records"]] == [
            r["seq"] for r in client.get("/api/memory").json()["records"]
        ], layers["long_term"]
        # Слой общий: у соседнего чата он тот же самый, а свои первые два —
        # свои. Выключателя у него нет вовсе: врезка едет всегда, когда
        # в слое что-то лежит.
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
        # слоя больше некому, кроме человека, и поле, у которого одно
        # значение, не различает ничего. Лишним полем в теле оно тоже
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
        # Тип — и здесь в одиночку, и здесь текст остаётся прежним: слои
        # правятся одинаково, и правка, работающая в одном из двух,
        # разъехалась бы с соседним на первом же исправлении.
        typed = client.patch(f"/api/memory/{was_agent['seq']}", json={"kind": "decision"})
        assert typed.status_code == 200, typed.text
        assert typed.json()["kind"] == "decision", typed.json()
        assert typed.json()["content"] == "поправлено руками", typed.json()
        assert client.patch(f"/api/memory/{was_agent['seq']}", json={}).status_code == 400
        assert client.patch(f"/api/memory/{was_agent['seq']}", json={"kind": "х"}).status_code == 400
        # Лишнее поле в правке — 400 тем же `_record_body`, что и при
        # добавлении: номер, автора и время выдаёт сервер, и запросу,
        # который их присылает, ручка делает не то, о чём он просит.
        # Автор здесь не случайное лишнее поле, а самое опасное: пройди
        # он — и «запись человека служебный вызов не трогает» отменялось
        # бы одним запросом.
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
        #
        # «Видна ли ветке запись» — вопрос не тот: дубли видны ровно так же.
        # Поэтому считаем **число** записей до и после, и считаем его
        # на непустом списке: скопируй ветвление слой себе, total удвоился бы,
        # а врезка в промпте ветки сказала бы всё дважды.
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
    #
    # Запись кладёт человек — других в этом слое не бывает, — и она обязана
    # пережить и чат, и перезапуск: область у слоя вся база, а жизнь дольше
    # разговора.
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

    # `forget()` забывает **разговор**: история, сводки и рабочая память —
    # его содержимое, а долговременная — нет. Что запись при этом остаётся
    # в базе, держит таблица очистки (`check_summary_apart_and_cleanup`);
    # здесь — её видимое следствие: чат, забывший разговор, по-прежнему
    # знает, на чём пишет собеседник, и говорит это в промпте.
    agent.forget()
    assert store.list_memory() == [kept], store.list_memory()
    assert "[долговременная память]" in agent.build_prompt("после forget")[0]["content"]

    # --- 10. Миграция: живая база догоняет схему, записи целы ---------------
    #
    # `CREATE TABLE IF NOT EXISTS` ни колонку не добавит, ни лишнюю не снимет,
    # а база у пользователя полна диалогов, и «удалите файл» здесь не ответ:
    # в памяти лежит набранное руками. Поэтому проверка идёт по настоящей
    # старой базе — со своими записями, с колонкой авторства и с двумя
    # таблицами, в которые больше не ходит ни один путь кода.
    import sqlite3

    old_path = _temp_db("memory-old")
    os.makedirs(os.path.dirname(old_path), exist_ok=True)
    old = sqlite3.connect(old_path)
    old.executescript(
        """
        CREATE TABLE memory (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
            content TEXT NOT NULL, author TEXT NOT NULL DEFAULT 'human',
            at REAL NOT NULL
        );
        INSERT INTO memory (kind, content, author, at)
            VALUES ('profile', 'набрано руками', 'human', 1.0);
        CREATE TABLE working_memory (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            kind TEXT NOT NULL, content TEXT NOT NULL, author TEXT NOT NULL,
            at REAL NOT NULL
        );
        INSERT INTO working_memory (session_id, kind, content, author, at)
            VALUES ('ag_00001', 'goal', 'цель из вчерашней базы', 'agent', 1.0);
        CREATE TABLE working_state (
            session_id TEXT PRIMARY KEY, upto INTEGER NOT NULL, metrics TEXT,
            at REAL NOT NULL
        );
        INSERT INTO working_state VALUES ('ag_00001', 4, NULL, 1.0);
        CREATE TABLE facts (
            session_id TEXT NOT NULL, seq INTEGER NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, upto INTEGER NOT NULL, metrics TEXT, at REAL NOT NULL,
            PRIMARY KEY (session_id, seq)
        );
        INSERT INTO facts VALUES ('ag_00001', 0, 'цель', 'снимок Дня 10', 2, NULL, 1.0);
        """
    )
    old.commit()
    old.close()

    with _reopened(old_path) as migrated:
        # Записи на месте и не потеряны — а колонки авторства у них больше
        # нет: снимать её миграция обязана **без** переноса данных, иначе
        # «удалите файл» вернулось бы другим словом.
        assert migrated.list_memory() == [
            {"seq": 1, "kind": "profile", "content": "набрано руками", "at": 1.0}
        ], migrated.list_memory()
        assert migrated.list_working("ag_00001") == [
            {"seq": 1, "kind": "goal", "content": "цель из вчерашней базы", "at": 1.0}
        ], migrated.list_working("ag_00001")
        # И колонки в файле те же, что в схеме: чтение идёт по именам, и
        # оставленная колонка через него не видна вовсе — а она осталась бы
        # в базе, и следующая миграция спорила бы с этой.
        for table, columns in (
            ("memory", {"seq", "kind", "content", "at"}),
            ("working_memory", {"seq", "session_id", "kind", "content", "at"}),
        ):
            left = {r["name"] for r in migrated.conn.execute(f"PRAGMA table_info({table})")}
            assert left == columns, (table, left)
        # Снимок Дня 10 и состояние ведения памяти снесены той же миграцией:
        # ни один путь кода в них больше не ходит.
        tables = {
            row["name"]
            for row in migrated.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "facts" not in tables, tables
        assert "working_state" not in tables, tables
        assert {"memory", "working_memory"} <= tables, tables
        # И повторный запуск на уже мигрированной базе ничего не делает:
        # миграция идемпотентна, иначе второй старт сервера ронял бы его
        # на «no such column».
        assert migrated._migrate() == [], migrated._migrate()
        # Новая запись рядом со старой ложится со своим номером.
        fresh_row = migrated.add_memory("knowledge", "после миграции")
        assert fresh_row["seq"] == 2, fresh_row

    with _restarted(store, agent.id) as (again, revived):
        assert again.list_memory() == [kept], again.list_memory()
        assert "[долговременная память]" in revived.build_prompt("после перезапуска")[0]["content"]

        # Агент без хранилища не падает — память у него просто пуста.
        homeless = Agent(AgentSpec(label="без базы", model="stub/model"))
        assert homeless.memory_items() == [], homeless.memory_items()
        assert homeless.prompt_slots()["memory_at"] is None, homeless.prompt_slots()
        assert homeless.build_prompt("вопрос") == [{"role": "user", "content": "вопрос"}]

        # И единственный путь, который память стирает, — служебная очистка
        # базы. Забудь её там — и `kill_all()` перед каждой проверкой
        # оставлял бы врезку следующей, сдвигая ей роли в промпте.
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
        "пережили forget() и переоткрытие файла, а clear() — нет; миграция "
        "сняла колонку авторства у обоих слоёв и снесла две мёртвые таблицы, "
        "не потеряв записи"
    )


@check("память меняет ответ: тот же вопрос с врезкой и без неё расходится")
def check_memory_changes_answer():
    """Единственный пункт задания Дня 11, который просили именно **проверить**:
    «как это влияет на ответы».

    Все прочие проверки памяти смотрят на **запрос** — что уехало в модель,
    каким по счёту сообщением, с какой подписью. Здесь впервые смотрим
    на **ответ**: тот же вопрос, те же настройки, пустой слой против
    непустого — и два разных ответа.

    Сравнение шло по выключателю памяти, пока выключатель был. Его не стало,
    и сравнение стало честнее: проверяется то, ради чего слой заведён, —
    **запись**, а не настройка. Вписали руками — ответ один, слой пуст —
    другой.

    Держится это на том, что заглушка умеет отвечать по содержимому запроса
    (`reply` принимает `(messages, index)`), а не по его номеру. Заглушка
    моделью не притворяется, и обещать, что живая модель ответит этими же
    словами, проверка не может и не обещает. Она обещает ровно то, что можно
    обещать: разница во врезке доезжает до ответа, а не теряется по дороге.

    Порядок здесь и есть проверка, и первый шаг в нём — пустая память.
    Утверждение о разнице обязано стоять на **непустом** значении с обеих
    сторон: чат с пустым слоем отвечает то же, что чат, заведённый до
    записи, — иначе «пустая память неотличима от отсутствующей» держалось
    бы только на составе промпта, а на ответах разъехалось бы.
    """
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
        #
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
        #
        # Разные ответы сами по себе ничего не доказывают: разойтись они могли
        # бы и от разного вопроса, и от разного системного промпта, и от номера
        # вызова. Поэтому сверяем сами запросы: всё, кроме врезки памяти, в них
        # совпадает слово в слово.
        assert [m for m in loud_prompt if "[долговременная память]" not in m["content"]] \
            == blank_prompt, (loud_prompt, blank_prompt)
        assert blank_prompt[-1]["content"] == question, blank_prompt[-1]
        assert loud_prompt[-1]["content"] == question, loud_prompt[-1]
        assert not any(
            "[долговременная память]" in m["content"] for m in blank_prompt
        ), blank_prompt

        # --- 5. И то же самое в рабочем слое -----------------------------------
        #
        # Слои разные — область и срок жизни, — а показывают они себя одинаково:
        # вписанная запись доезжает до ответа. Запись здесь **своя**, под чатом,
        # и соседнего чата она не касается.
        task = new_agent(client, label="с задачей", system="СИС")
        client.post(
            f"/api/agents/{task}/working", json={"kind": "goal", "content": "пример на Kotlin"}
        )
        working_answer, working_prompt = answer(task, client)
        assert working_answer != empty_answer, working_answer
        assert "Kotlin" in working_answer, working_answer
        assert any("[факты о разговоре]" in m["content"] for m in working_prompt), working_prompt

        # --- 6. Ответ разный, а счёт обменов одинаковый ------------------------
        #
        # Врезка памяти — часть промпта, а не лишний вызов: разницу в ответе
        # она даёт, не добавляя обращений к модели. Вызовов ровно столько,
        # сколько обменов, и служебных среди них нет ни одного.
        assert len(_stub.CALLS) == 3, len(_stub.CALLS)
        assert not _service_calls(), "за памятью сходили к модели"

    return (
        f"с записью — {loud!r}; без неё — {empty_answer!r}; "
        f"рабочая память меняет ответ так же — {working_answer!r}; "
        "запросы различаются одной врезкой"
    )


@check("профиль меняет ответ: один вопрос, два профиля — два разных ответа")
def check_profile_changes_answer():
    """Единственный пункт задания Дня 12, который просили именно **проверить**:
    «ответы для разных профилей».

    Профиль — про то, **как** с человеком разговаривать: стиль, формат,
    контекст его работы. От записи памяти он отличается не местом хранения,
    а наклонением: память это факт («пишет на Kotlin»), профиль —
    распоряжение («отвечай кратко»). Отсюда и роль сообщения: **системным
    едет то, что задал человек**, а выведенное из разговора — обычным,
    с подписью.

    Порядок здесь и есть проверка. Сперва пустой профиль: ни блока, ни
    системного сообщения — пустое обязано быть неотличимо от отсутствующего,
    и у профиля это строже, чем у памяти, потому что системное сообщение
    сдвигает номера **всех** врезок разом. Потом два разных профиля на один
    и тот же вопрос. Потом ловушка: чат **без** системного промпта, у которого
    профиль непуст, — номера врезок обязаны учесть появившееся системное
    сообщение, и ошибка здесь не падает, а расходится молча.

    Держится всё на том, что заглушка отвечает по содержимому запроса
    (`reply` принимает `(messages, index)`), а не по его номеру, — тем же
    способом, каким проверяется «память меняет ответ».
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
        #
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
        #
        # Разные ответы сами по себе не доказывают ничего: разойтись они могли
        # бы и от разного вопроса, и от номера вызова. Поэтому сверяем сами
        # запросы: всё, кроме системного сообщения, в них совпадает слово
        # в слово, а вопрос — тот же самый.
        assert short_prompt[1:] == long_prompt[1:] == blank_prompt, (short_prompt, long_prompt)
        assert short_prompt[0]["content"] != long_prompt[0]["content"], short_prompt

        # --- 6. Профиль едет **системным** сообщением, а не обычным -----------
        #
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
        #
        # Пустое поле уехало бы строкой «формат: » и сказало бы модели ровно
        # ничего, заняв место распоряжения. И системный промпт чата стоит
        # в том же сообщении, а не вторым системным: «кто ты» и «как отвечать»
        # читаются как одно распоряжение.
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
        #
        # Номера берутся из длины уже собранного начала промпта, а системное
        # сообщение теперь заводится и у чата **без** системного промпта —
        # если профиль непуст. Забудь об этом — и все врезки уедут на единицу,
        # причём молча: промпт останется собранным верно, а подписи ролей
        # в просмотре запроса встанут над чужими сообщениями.
        naked = new_agent(client, label="без промпта, с профилем")
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{naked}/working", json={"kind": "goal", "content": "собрать ТЗ"})
        start = _frame(_frames(client, naked, question), "start")
        assert start["memory_at"] == 1 and start["working_at"] == 2, start
        assert start["resolved_messages"][0]["role"] == "system", start["resolved_messages"][0]

        # И обратно: сняли профиль — системного сообщения снова нет, номера
        # вернулись на место. Пустая строка поле снимает, и пустой профиль
        # неотличим от отсутствовавшего.
        client.patch("/api/profile", json={"style": "", "context": ""})
        assert client.get("/api/profile").json() == {"profile": {}}, "профиль не снялся"
        bare = _frame(_frames(client, naked, question), "start")
        assert bare["memory_at"] == 0 and bare["working_at"] == 1, bare
        assert bare["resolved_messages"][0]["role"] == "user", bare["resolved_messages"][0]

        # --- 9. Профиль пишет только человек ----------------------------------
        #
        # Шесть обменов позади, и ни один не дописал в профиль ни строки:
        # пути туда у агента нет вовсе. И лишнего вызова к модели профиль
        # не стоит — он часть промпта, а не служебный вызов.
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


# --- День 14: инварианты — чего ассистент не вправе предлагать ----------------


@check("инварианты: слой глобальный, едет системным, запрещённых слов в промпте нет")
def check_invariants_layer():
    """День 14: то, что ассистент не вправе нарушать, — отдельно от диалога.

    Слой устроен как долговременная память: своя таблица **без** `session_id`,
    записи с идентичностью, пишет только человек. Отличий от неё ровно два,
    и оба принципиальные.

    **Первое — роль.** Инвариант едет **системным** сообщением, четвёртой
    частью к промпту чата, профилю и правилу этапа: системным едет то, что
    задал человек, а инвариант это распоряжение, которому модель следует,
    а не сведения, на которые она опирается. Своего слота он поэтому
    не заводит вовсе — номера врезок памяти от него не сдвигаются.

    **Второе — запрещённые слова.** Они хранятся, правятся и отдаются
    ручкой, но в промпт не уезжают ни одним символом: перечисленный запрет
    сам по себе подсказка его употребить, а законный отказ («почему
    не Java?») без запрещённого слова не написать. Это знание сторожа,
    который смотрит на ответ, и модели оно не показывается.

    Отсюда и ловушка, ровно та же, что у профиля: пустой слой обязан быть
    неотличим от отсутствующего, иначе у чата **без** системного промпта
    и **без** профиля блок заведёт собой системное сообщение и сдвинет
    номера всех врезок разом.
    """
    from app.agent import Agent, PROMPT_SLOTS

    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Пустой слой неотличим от отсутствующего ----------------------
        assert client.get("/api/invariants").json() == {"total": 0, "records": []}
        bare = new_agent(client, system="")
        start = _frame(_frames(client, bare, "первый"), "start")
        assert _stub.CALLS[-1]["messages"] == [
            {"role": "user", "content": "первый"}
        ], _stub.CALLS[-1]["messages"]
        assert start["memory_at"] is None, start["memory_at"]

        # --- 2. Ручки: вид обязателен и без умолчания ------------------------
        #
        # Подставь сервер «architecture» на пропущенный ключ — и «человек явно
        # выбирает» стало бы «сервер выбрал за него», ровно как в памяти.
        bad = [
            {"content": "вид не назван"},
            {"kind": None, "content": "вид снят"},
            {"kind": "stak", "content": "вид с опечаткой"},
            {"kind": "decision", "content": "вид из чужого слоя"},
            {"kind": "stack"},
            {"kind": "stack", "content": "   "},
            {"kind": "stack", "content": "лишнее поле", "seq": 5},
            {"kind": "stack", "content": "слова строкой", "banned": "Java"},
            {"kind": "stack", "content": "слова словарём", "banned": {"1": "Java"}},
            {"kind": "stack", "content": "пустое слово", "banned": ["Java", "  "]},
            {"kind": "stack", "content": "слово числом", "banned": [7]},
        ]
        for payload in bad:
            answer = client.post("/api/invariants", json=payload)
            assert answer.status_code == 400, (payload, answer.status_code, answer.text)
        # «решение» есть и в памяти, и в рабочей памяти — но не здесь: виды
        # слоёв нарочно не пересекаются ни одним словом.
        assert "stack" in client.post("/api/invariants", json={"content": "х"}).json()["detail"]

        # Пустой список слов законен: у большинства инвариантов сторожить
        # нечего, их держит сам текст.
        stack = client.post("/api/invariants", json={
            "kind": "stack", "content": "  бэкенд только на Python  ",
            "banned": ["Java", " Groovy "],
        }).json()
        plain = client.post("/api/invariants", json={
            "kind": "architecture", "content": "монолит, микросервисов не предлагать",
        }).json()
        assert stack["content"] == "бэкенд только на Python", stack
        assert stack["banned"] == ["Java", "Groovy"], stack
        assert plain["banned"] == [], plain
        assert plain["seq"] > stack["seq"], (stack, plain)
        assert client.get("/api/invariants").json()["total"] == 2

        # --- 3. Врезка: системным, последней частью, и системное одно --------
        _stub.reset()
        client.patch("/api/profile", json={"style": "кратко, на ты"})
        talky = new_agent(client, system="СИС")
        client.post(f"/api/agents/{talky}/task", json={"description": "собрать ТЗ"})
        client.post(f"/api/agents/{talky}/messages", json={"text": "вопрос"})
        sent = _stub.CALLS[-1]["messages"]
        assert len([m for m in sent if m["role"] == "system"]) == 1, sent
        head = sent[0]
        assert head["role"] == "system", head
        # Слагаемые сверяются по заголовкам, а сам блок — целиком: граф
        # автомата у соседа свой, и списывать его сюда значило бы краснеть
        # этой проверкой на каждое новое ребро в чужой таблице.
        blocks = head["content"].split("\n\n")
        assert [part.partition("\n")[0] for part in blocks] == [
            "СИС", "[как отвечать]", "[этап задачи: планирование]",
            "[жизненный цикл задачи]", "[чего нельзя]",
        ], blocks
        assert blocks[1] == "[как отвечать]\nстиль: кратко, на ты", blocks[1]
        assert blocks[4] == (
            "[чего нельзя]\n"
            "1. ограничение стека: бэкенд только на Python\n"
            "2. архитектура: монолит, микросервисов не предлагать\n"
            "Соблюдай это. Если просьба противоречит любому пункту — откажись "
            "и назови, какой именно нарушается."
        ), blocks[4]
        # Врезкой ролью `user` блок не едет ни одним экземпляром: своего места
        # в промпте у инвариантов нет вовсе.
        assert not any(
            m["role"] != "system" and "[чего нельзя]" in m["content"] for m in sent
        ), sent

        # --- 4. Запрещённых слов в промпте нет ни одного символа -------------
        #
        # Главное решение дня после роли. Список слов — знание сторожа,
        # который смотрит на **ответ**; модели он не показывается вовсе:
        # перечисленный запрет это подсказка его употребить, а на «почему
        # не Java?» без слова «Java» не ответить.
        for word in ("Java", "Groovy"):
            assert not any(word in m["content"] for m in sent), (word, sent)

        # --- 5. Слота у инвариантов нет: номера врезок не сдвинулись ---------
        assert PROMPT_SLOTS == ("memory_at", "working_at", "task_at", "summary_at"), PROMPT_SLOTS

        # --- 6. Ловушка: пустой слой против непустого у голого чата ----------
        #
        # Та же, что у профиля, и молчит она так же: промпт остаётся собранным
        # верно, а подписи ролей в просмотре запроса встают над чужими
        # сообщениями. Профиль снимаем — голый теперь значит «ни промпта,
        # ни профиля, ни инвариантов».
        client.patch("/api/profile", json={"style": ""})
        assert client.get("/api/profile").json() == {"profile": {}}, "профиль не снялся"
        naked = new_agent(client, system="")
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на бэкенде"})
        shifted = _frame(_frames(client, naked, "вопрос"), "start")
        assert shifted["memory_at"] == 1, shifted["memory_at"]
        assert shifted["resolved_messages"][0]["role"] == "system", shifted["resolved_messages"][0]

        # И обратно: убрали записи — системного сообщения снова нет, номера
        # вернулись на место.
        for record in client.get("/api/invariants").json()["records"]:
            assert client.delete(f"/api/invariants/{record['seq']}").status_code == 200
        back = _frame(_frames(client, naked, "вопрос"), "start")
        assert back["memory_at"] == 0, back["memory_at"]
        assert back["resolved_messages"][0]["role"] == "user", back["resolved_messages"][0]

        # --- 7. Правка и удаление по номеру ----------------------------------
        one = client.post("/api/invariants", json={
            "kind": "business", "content": "оплата только картой", "banned": ["наличные"],
        }).json()
        # Неназванное поле не трогается, названное записывается.
        typed = client.patch(f"/api/invariants/{one['seq']}", json={"kind": "technical"})
        assert typed.json()["kind"] == "technical", typed.json()
        assert typed.json()["content"] == "оплата только картой", typed.json()
        assert typed.json()["banned"] == ["наличные"], typed.json()
        # Пустой список снимает все слова — и это не то же, что не назвать их.
        cleared = client.patch(f"/api/invariants/{one['seq']}", json={"banned": []})
        assert cleared.json()["banned"] == [], cleared.json()
        assert client.patch(f"/api/invariants/{one['seq']}", json={}).status_code == 400
        assert client.patch(
            f"/api/invariants/{one['seq']}", json={"content": "х", "seq": 5}
        ).status_code == 400
        assert client.patch(
            f"/api/invariants/{one['seq']}", json={"banned": "наличные"}
        ).status_code == 400
        assert client.patch("/api/invariants/9999", json={"content": "нет"}).status_code == 404
        assert client.delete("/api/invariants/9999").status_code == 404
        # Номер удалённой заново не выдаётся: AUTOINCREMENT, как у `memory`.
        assert client.delete(f"/api/invariants/{one['seq']}").status_code == 200
        again = client.post("/api/invariants", json={
            "kind": "stack", "content": "бэкенд только на Python", "banned": ["Java"],
        }).json()
        assert again["seq"] > one["seq"], (again, one)

        # --- 8. Читается один раз за обмен -----------------------------------
        #
        # Слой заводит **системное** сообщение, и прочитанный дважды развёл бы
        # промпт с номерами врезок сильнее любой врезки: запись, добавленная
        # соседней вкладкой между двумя чтениями, сдвинула бы их все разом.
        store = REGISTRY.store
        real_list, reads = store.list_invariants, []

        def counted():
            reads.append(1)
            return real_list()

        _stub.reset()
        with patch.object(store, "list_invariants", counted):
            client.post(f"/api/agents/{naked}/messages", json={"text": "сколько раз читали"})
        assert len(reads) == 1, f"чтений слоя за обмен: {len(reads)}, а должно быть одно"
        assert any(
            "[чего нельзя]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]

        # --- 9. Сжатию инварианты не достаются, и это даром ------------------
        #
        # `build_compress_prompt` собирает свои сообщения сам и `spec.system`
        # не читает вовсе. Записано утверждением, а не надеждой: почини это
        # кто-нибудь завтра — инвариант попал бы в пересказ и вернулся бы
        # в промпт вторым экземпляром, да ещё и искажённым.
        _stub.install(reply=_service_aware)
        folding = new_agent(
            client, system="СИС", strategy="summary", keep_last=2, compress_every=2
        )
        _talk(client, folding, 3)
        call = _service_calls("summary")[-1]["messages"]
        assert not any("[чего нельзя]" in m["content"] for m in call), call
        assert not any("бэкенд только на Python" in m["content"] for m in call), call
        assert not any("Java" in m["content"] for m in call), call

        # --- 10. Ветвление слой не копирует ----------------------------------
        #
        # Слой глобальный, и ветка видит его через то же хранилище — не свою
        # копию. «Видна ли ветке запись» — вопрос не тот: дубли видны так же,
        # поэтому считаем **число** записей, и считаем на непустом списке.
        before = client.get("/api/invariants").json()["total"]
        assert before, "слой пуст — копировать нечего, проверять тоже"
        branch = client.post(
            f"/api/agents/{folding}/fork", json={"at": 2}
        ).json()["agents"][0]["id"]
        _stub.install(reply=lambda m, i: f"ответ {i}")
        _stub.reset()
        client.post(f"/api/agents/{branch}/messages", json={"text": "вопрос ветки"})
        assert any(
            "[чего нельзя]" in m["content"] for m in _stub.CALLS[-1]["messages"]
        ), _stub.CALLS[-1]["messages"]
        assert client.get("/api/invariants").json()["total"] == before, (
            "ветвление завело копии инвариантов"
        )

    # --- 11. Чат уходит, инвариант остаётся; clear() замыкает круг -----------
    #
    # Видимое следствие таблицы очистки: чат, забывший разговор, по-прежнему
    # знает, чего ему нельзя предлагать, и говорит это в промпте. Что записи
    # при этом целы в базе, держит `check_summary_apart_and_cleanup`.
    path = _temp_db("invariants")
    store = Store(path).init()
    agent = Agent(AgentSpec(label="с инвариантом", model="stub/model"), store=store)
    store.add_invariant("stack", "бэкенд только на Python", ["Java"])
    asyncio.run(drain(agent.ask("вопрос")))
    assert "[чего нельзя]" in agent.build_prompt("ещё")[0]["content"], "врезки нет"
    agent.forget()
    assert len(store.list_invariants()) == 1, store.list_invariants()
    assert "[чего нельзя]" in agent.build_prompt("после forget")[0]["content"]

    with _restarted(store, agent.id) as (fresh, revived):
        assert len(fresh.list_invariants()) == 1, fresh.list_invariants()
        assert "[чего нельзя]" in revived.build_prompt("после перезапуска")[0]["content"]

        # Агент без хранилища не падает — слой у него просто пуст.
        homeless = Agent(AgentSpec(label="без базы", model="stub/model"))
        assert homeless.invariant_items() == [], homeless.invariant_items()
        assert homeless.build_prompt("вопрос") == [{"role": "user", "content": "вопрос"}]

        # Единственный путь, который слой стирает, — служебная очистка базы.
        # Забудь его там, и `kill_all()` перед каждой проверкой оставлял бы
        # следующей системное сообщение, сдвигая ей номера всех врезок.
        fresh.clear()
        assert fresh.list_invariants() == [], fresh.list_invariants()
        assert revived.build_prompt("после очистки") == [
            {"role": "user", "content": "после очистки"}
        ], revived.build_prompt("после очистки")

    return (
        "пустой слой неотличим от отсутствующего; вид без умолчания и список "
        "слов разбором — одиннадцать кривых тел дали 400; блок едет четвёртой "
        "частью единственного системного сообщения, запрещённых слов в промпте "
        "нет ни одного; слота у него нет — врезки остались на своих номерах, "
        "а у голого чата сдвинулись на 1 и вернулись; за обмен слой прочитан "
        "один раз; сжатию он не достался; ветвление копий не завело"
    )


@check("инварианты меняют ответ: тот же вопрос с записью и без неё расходится")
def check_invariants_change_answer():
    """Единственный пункт задания Дня 14, который просили именно **проверить**:
    «отказывается предлагать решения, которые их нарушают».

    Все прочие проверки слоя смотрят на **запрос** — что уехало в модель,
    каким сообщением, с какой ролью. Здесь смотрим на **ответ**: тот же
    вопрос, те же настройки, пустой слой против непустого — и два разных
    ответа, причём во втором отказ назван своим инвариантом.

    Заглушка не модель и моделью не притворяется: обещать, что живая модель
    ответит этими словами, проверка не вправе. Она обещает ровно то, что
    можно обещать и здесь, и на живом прогоне: разница доезжает до ответа,
    а не теряется по дороге. Довод и устройство те же, что
    у `_memory_aware` и `_profile_aware`.
    """
    _stub.install(reply=_invariant_aware)
    question = "на чём писать бэкенд?"

    def answer(agent_id, client) -> tuple[str, list[dict]]:
        done = _frame(_frames(client, agent_id, question), "done")
        return done["text"], _stub.CALLS[-1]["messages"]

    with TestClient(main.app) as client:
        # --- 1. Слой пуст: ни блока, ни системного сообщения ------------------
        assert client.get("/api/invariants").json()["total"] == 0, "слой не пуст"
        blank = new_agent(client, label="без инвариантов")
        free_answer, blank_prompt = answer(blank, client)
        assert blank_prompt == [{"role": "user", "content": question}], blank_prompt

        # --- 2. Инвариант вписал человек — и только человек -------------------
        #
        # Другого пути сюда нет: агент инварианты не выводит из разговора,
        # это распоряжение. Ровно это и делает пользователь на экране.
        written = client.post("/api/invariants", json={
            "kind": "stack", "content": "бэкенд только на Python",
            "banned": ["Java", "Groovy"],
        })
        assert written.status_code == 200, written.text
        bound = new_agent(client, label="с инвариантом")
        strict_answer, strict_prompt = answer(bound, client)

        # --- 3. Главное: ответ разошёлся, и отказ назвал инвариант ------------
        assert strict_answer != free_answer, f"ответ не изменился: {strict_answer!r}"
        assert "ограничение стека" in strict_answer, strict_answer
        assert "Java" in free_answer, free_answer
        assert "Java" not in strict_answer, strict_answer

        # --- 4. И разошёлся **от инварианта**, а не от чего-нибудь ещё --------
        #
        # Разные ответы сами по себе не доказывают ничего: разойтись они могли
        # бы и от разного вопроса, и от номера вызова. Поэтому сверяем сами
        # запросы: всё, кроме системного сообщения, совпадает слово в слово.
        assert strict_prompt[1:] == blank_prompt, (strict_prompt, blank_prompt)
        assert strict_prompt[0]["role"] == "system", strict_prompt[0]
        assert strict_prompt[0]["content"].startswith("[чего нельзя]"), strict_prompt[0]

        # --- 5. Запрещённых слов нет ни в одном сообщении ни одного промпта ---
        #
        # Они лежат в той же записи и в том же ответе ручки — и всё равно
        # не уезжают: в блок едут только вид и текст.
        assert client.get("/api/invariants").json()["records"][0]["banned"] == [
            "Java", "Groovy"
        ], client.get("/api/invariants").json()
        for word in ("Java", "Groovy"):
            assert not any(word in m["content"] for m in strict_prompt), (word, strict_prompt)

        # --- 6. Ответ разный, а счёт обменов одинаковый -----------------------
        #
        # Блок — часть промпта, а не лишний вызов: разницу в ответе он даёт,
        # не добавляя обращений к модели.
        assert len(_stub.CALLS) == 2, len(_stub.CALLS)
        assert not _service_calls(), "за инвариантами сходили к модели"

    return (
        f"с инвариантом — {strict_answer!r}; без него — {free_answer!r}; "
        "запросы различаются одним системным сообщением, запрещённых слов "
        "в промпте нет ни одного"
    )

# --- День 14, второй PR: сторож смотрит на готовый ответ ----------------------


def _marks(metrics) -> list[dict]:
    """Отметки сторожа в метриках обмена — их может не быть вовсе."""
    return (metrics or {}).get("banned_hits") or []


def _guard_answers(table: dict):
    """Заглушка, отвечающая по вопросу, а не по номеру вызова: сторож смотрит
    на **текст ответа**, и каждый случай задаётся своей парой «вопрос → ответ»."""
    return lambda messages, index: table[messages[-1]["content"]]


@check("сторож ловит запрещённое слово целиком — и только его")
def check_guard_finds_banned_words():
    """Слова лежали в базе с прошлого PR и ждали, кто на них посмотрит.
    Смотрит сторож — в **готовый** ответ, собранный целиком: слово,
    разорванное между кусками потока, по кускам не нашлось бы.

    Граница слова здесь не `\\b`: та держится на `\\w` с обеих сторон и рвётся
    на `C++`. Проверяются оба края сразу — `Java` внутри `JavaScript`
    не отмечается, а `C++` посреди фразы отмечается.

    Словоформы не ловятся, и это записано утверждением, а не умолчанием:
    пробел, названный проверкой, завтра не сойдёт за свойство.
    """
    answers = {
        "стек?": "Берите Java и Kotlin.",
        "фронт?": "JavaScript и TypeScript — обычный выбор.",
        "точно?": "java тоже сойдёт",
        "язык?": "Лучше Котлин.",
        "плюсы?": "Возьмите C++ и не думайте.",
        "формы?": "Пишите Котлином, не пожалеете.",
        "чисто?": "Возьмите Python, он тут к месту.",
    }
    _stub.install(reply=_guard_answers(answers))

    with TestClient(main.app) as client:
        written = client.post("/api/invariants", json={
            "kind": "stack", "content": "бэкенд только на Python",
            "banned": ["Java", "Котлин", "C++"],
        })
        assert written.status_code == 200, written.text
        agent_id = new_agent(client, label="сторож")
        marks = {
            question: _marks(_frame(_frames(client, agent_id, question), "done")["metrics"])
            for question in answers
        }

    # --- 1. Слово из списка в ответе — отметка есть, и она называет оба ----
    #
    # И слово, и правило: отметка без правила оставила бы читателя гадать,
    # чем именно «Java» плоха в этом чате.
    assert marks["стек?"] == [
        {"word": "Java", "rule": "ограничение стека: бэкенд только на Python"}
    ], marks["стек?"]

    # --- 2. Слова нет — отметки нет вовсе ----------------------------------
    assert marks["чисто?"] == [], marks["чисто?"]

    # --- 3. Слово целиком: `Java` внутри `JavaScript` не отмечается ---------
    assert marks["фронт?"] == [], marks["фронт?"]

    # --- 4. Регистр не важен ------------------------------------------------
    assert [m["word"] for m in marks["точно?"]] == ["Java"], marks["точно?"]

    # --- 5. Кириллическое слово ловится тем же правилом ---------------------
    assert [m["word"] for m in marks["язык?"]] == ["Котлин"], marks["язык?"]

    # --- 6. Слово со знаком ловится: граница не `\b` ------------------------
    assert [m["word"] for m in marks["плюсы?"]] == ["C++"], marks["плюсы?"]

    # --- 7. Словоформы не ловятся — это пробел, и он назван -----------------
    assert marks["формы?"] == [], marks["формы?"]

    # --- 8. Сторож ничего не запускает: обменов столько же, сколько вопросов -
    assert len(_stub.CALLS) == len(answers), len(_stub.CALLS)
    assert not _service_calls(), "за сторожем сходили к модели"
    return (
        f"задели запрет {sum(1 for m in marks.values() if m)} ответа из {len(answers)}; "
        "`Java` в `JavaScript` и «Котлином» — нет"
    )


@check("отметка сторожа живёт с репликой: поток, база, суммы, сжатие")
def check_guard_mark_lives_with_turn():
    """Отметка кладётся в метрики обмена — тем же образцом, что `dropped`.
    Отсюда три свойства разом, и каждое проверяется своим утверждением.

    Колонки под отметку нет: метрики пишутся с репликой одной транзакцией,
    значит отметка видна и в старых обменах после перезапуска.
    """
    from app.agent import Agent

    word = "Kafka"
    text = f"Поставьте {word} и живите спокойно."
    path = _temp_db("guard-restart")
    store = Store(path).init()
    store.add_invariant("technical", "очередь — Redis", [word])

    def answer_and_rewrite(messages, index):
        """Соседняя вкладка правит инвариант, пока модель отвечает. Ответ
        обязан судиться тем правилом, что уехало в промпт: перечитанное
        осудило бы его правилом, которого модель не видела."""
        store.update_invariant(
            1, kind="technical", content="очередь — теперь Kafka", banned=["живите"]
        )
        return text

    # Каждый кусок потока — один символ: слово, собранное только целиком,
    # ни в одной дельте не лежит. Искал бы сторож в кусках — не нашёл бы.
    _stub.install(reply=answer_and_rewrite, chunks=len(text))

    async def one_turn():
        agent = Agent(AgentSpec(label="сторож", model="stub/model"), store=store)
        return agent.id, await drain(agent.ask("чем очередь?"))

    agent_id, frames = asyncio.run(one_turn())
    deltas = [f["text"] for f in frames if f["type"] == "delta"]
    done = next(f for f in frames if f["type"] == "done")

    # --- 1. Ответ разрезан на символы, а слово всё равно найдено -----------
    assert max(len(d) for d in deltas) == 1, max(len(d) for d in deltas)
    assert not any(word in d for d in deltas), deltas[:5]
    assert _marks(done["metrics"]) == [
        {"word": word, "rule": "техническое решение: очередь — Redis"}
    ], done["metrics"]

    # --- 2. Правило в отметке — то, что уехало в промпт ---------------------
    #
    # Слой переписан посреди обмена, и новое слово в ответе есть («живите»):
    # перечитай сторож слой — отметка назвала бы его и чужое правило.
    assert store.list_invariants()[0]["content"] == "очередь — теперь Kafka", (
        store.list_invariants()
    )

    # --- 3. Отметка пережила перезапуск: она лежит в базе, а не в памяти ---
    with _restarted(store, agent_id) as (again, revived):
        answer = revived.history[-1]
        assert _marks(answer.metrics) == [
            {"word": word, "rule": "техническое решение: очередь — Redis"}
        ], answer.metrics
        # --- 4. Суммы по чату от отметки не изменились --------------------
        #
        # Сверяются они с числами **заглушки**, а не с копией тех же метрик:
        # копия сдвинулась бы вместе с ними, и утверждение было бы зелёным
        # при любом слагаемом. Слагаемых в итоге ровно столько, сколько
        # полей в USAGE_FIELDS — и ключа отметки среди них нет.
        total = revived.usage_summary()
        assert agent_module.BANNED_METRIC not in total, sorted(total)
        assert total["total_tokens"] == 100, total
        assert total["cost_usd"] == 0.000123, total
        assert total["completion_tokens"] == len(text) // 4, total
        assert again.list_invariants()[0]["content"] == "очередь — теперь Kafka", (
            again.list_invariants()
        )

    # --- 5. Сжатие сторож не трогает ---------------------------------------
    #
    # Служебный вызов пересказывает разговор, а не отвечает собеседнику:
    # запрещённое слово в пересказе — это слово из истории, а не предложение
    # его употребить. Инварианты сжатию не достаются вовсе, и судить его
    # было бы нечем.
    _stub.install(reply=lambda messages, index: text)
    with TestClient(main.app) as client:
        client.post("/api/invariants", json={
            "kind": "technical", "content": "очередь — Redis", "banned": [word],
        })
        folded = new_agent(
            client, label="со сжатием", strategy="summary",
            keep_last=KEEP, compress_every=EVERY,
        )
        _talk(client, folded, 11)
        agent = REGISTRY.require(folded)
        assert agent.summaries, "сворачивания не было — проверять нечего"
        for item in agent.summaries:
            assert _marks(item["metrics"]) == [], item["metrics"]
        # А у ответов того же чата отметка есть: слово одно и то же,
        # и отсутствие у сводки — решение, а не совпадение.
        answers = [t for t in agent.history if t.role == "assistant"]
        assert all(_marks(t.metrics) for t in answers), [t.metrics for t in answers]

    return (
        f"слово собрано из {len(deltas)} дельт по одному символу; отметка пережила "
        f"перезапуск с прежним правилом; сумм не изменила; "
        f"у сводок ({len(agent.summaries)}) отметок нет"
    )


# --- День 13: состояние задачи — строгая машина, ручное управление ------------


def _task_url(agent_id: str) -> str:
    return f"/api/agents/{agent_id}/task"


def _task_of(client, agent_id: str) -> dict | None:
    """Состояние задачи так, как его видит клиент: полем `task` самого чата.
    Отдельной ручки чтения нет — второе место «состояние наружу» обязано было
    бы совпадать с первым, и совпадало бы только пока за ним следят."""
    return client.get(f"/api/agents/{agent_id}").json()["task"]


def _at_stage(client, stage: str, label: str = "задача"):
    """Чат, доведённый до нужного этапа **командами** — других путей нет.

    Утверждений здесь нет: на какой этап он встал, проверяет сама проверка.
    Пауза берётся с планирования, значит снятие вернёт туда же.
    """
    agent_id = new_agent(client, label=f"{label} {stage}")
    client.post(_task_url(agent_id), json={"description": f"{label} — собрать ТЗ"})
    moves = {
        "planning": (),
        "execution": ("next",),
        "validation": ("next", "next"),
        "done": ("next", "next", "next"),
        "paused": ("pause",),
    }[stage]
    for move in moves:
        client.patch(_task_url(agent_id), json={"move": move})
    return agent_id


@check("этап меняется только переходом из таблицы, и двигает его человек")
def check_task_machine():
    """Автомат Дня 13: планирование → выполнение → проверка → готово, пауза
    с любого незавершённого этапа.

    Ходов три и все три — нажатия человека: у модели нет ни одного рычага.
    Таблица переходов одна, и проходится она целиком: пятнадцать клеток
    «этап × ход», где семь ведут дальше, а восемь обязаны отказать, оставив
    состояние прежним.

    Ожидаемое действие — третья ось задания, хранится наравне с этапом
    и шагом: не задавали — едет умолчание этапа, задали — своё, сменили
    этап — снова умолчание.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Вся таблица переходов, клетка за клеткой --------------------
        walked, refused = [], []
        for stage in STAGES:
            for move in MOVES:
                agent_id = _at_stage(client, stage, "обход")
                before = _task_of(client, agent_id)
                assert before["stage"] == stage, (stage, before)
                answer = client.patch(_task_url(agent_id), json={"move": move})
                target = TRANSITIONS.get((stage, move))
                if target is None:
                    # Незаконный ход: отказ, и состояние цело. Сделать
                    # соседний ход за человека было бы хуже отказа.
                    assert answer.status_code == 409, (stage, move, answer.text)
                    assert _task_of(client, agent_id) == before, (
                        f"{stage} + {move}: отказали, а состояние сдвинулось"
                    )
                    refused.append((stage, move))
                    continue
                # Снятие паузы возвращает туда, откуда встали: помощник
                # ставил её с планирования.
                expected = "planning" if target == RESUME else target
                assert answer.status_code == 200, (stage, move, answer.text)
                assert answer.json()["task"]["stage"] == expected, (stage, move, answer.json())
                walked.append((stage, move))
        assert len(walked) == 7 and len(refused) == 8, (walked, refused)
        # «Готово» терминально: с него не уводит ни один ход.
        assert [m for (s, m) in refused if s == "done"] == list(MOVES), refused

        # --- 2. Пауза с каждого незавершённого этапа, и возврат туда же -----
        for stage in ("planning", "execution", "validation"):
            agent_id = _at_stage(client, stage, "пауза")
            paused = client.patch(_task_url(agent_id), json={"move": "pause"})
            assert paused.status_code == 200, (stage, paused.text)
            assert paused.json()["task"]["stage"] == "paused", paused.json()
            back = client.patch(_task_url(agent_id), json={"move": "resume"})
            assert back.status_code == 200, (stage, back.text)
            assert back.json()["task"]["stage"] == stage, (stage, back.json())

        # --- 3. Ожидаемое действие: умолчание, своё, снова умолчание --------
        #
        # Смена этапа заданное сбрасывает: оставленное, оно называло бы
        # действие прошлого этапа.
        agent_id = _at_stage(client, "execution", "ожидание")
        default = _task_of(client, agent_id)["expects"]
        assert default == STAGE_EXPECTS["execution"], default
        mine = client.patch(_task_url(agent_id), json={"expects": "показать черновик"})
        assert mine.status_code == 200, mine.text
        assert mine.json()["task"]["expects"] == "показать черновик", mine.json()
        # Шаг правится тем же телом и в одиночку: оси разные.
        stepped = client.patch(_task_url(agent_id), json={"step": "пишу проверку"})
        assert stepped.json()["task"] == {
            **mine.json()["task"], "step": "пишу проверку",
        }, stepped.json()
        moved = client.patch(_task_url(agent_id), json={"move": "next"})
        assert moved.json()["task"]["expects"] == STAGE_EXPECTS["validation"], moved.json()
        assert moved.json()["task"]["step"] == "пишу проверку", "смена этапа съела шаг"

        # --- 4. Границы ручек ------------------------------------------------
        url = _task_url(agent_id)
        for body in ({}, {"move": "назад"}, {"move": None}, {"stage": "done"},
                     {"step": "   "}, {"expects": ""}, {"move": "next", "step": 5}):
            assert client.patch(url, json=body).status_code == 400, body
        assert client.post(url, json={"description": "  "}).status_code == 400
        assert client.post(url, json={"description": "х", "stage": "done"}).status_code == 400
        # Кривой текст рядом с законным ходом состояние не двигает: тело
        # разбирается целиком до первой правки.
        assert _task_of(client, agent_id)["stage"] == "validation", _task_of(client, agent_id)

        # --- 5. Ход называет этап, из которого его выбирали ------------------
        #
        # Таблица стережёт, какие рёбра есть, но не то, из какой вершины
        # человек на самом деле выходил, — а он видел на экране именно её.
        # Две вкладки на одном чате иначе делают два хода подряд: этап
        # перепрыгивается, и обе получают 200.
        picked = _at_stage(client, "planning", "сверка")
        ahead = client.patch(_task_url(picked), json={"move": "next", "from": "planning"})
        assert ahead.status_code == 200, ahead.text
        assert ahead.json()["task"]["stage"] == "execution", ahead.json()
        # Вторая вкладка всё ещё думает, что чат на планировании.
        stale = client.patch(_task_url(picked), json={"move": "next", "from": "planning"})
        assert stale.status_code == 409, stale.text
        assert "планирование" in stale.json()["detail"], stale.text
        assert "выполнение" in stale.json()["detail"], stale.text
        assert _task_of(client, picked)["stage"] == "execution", "отказали, а этап сдвинулся"
        # Не названный `from` по-прежнему проходит: он необязателен.
        assert client.patch(_task_url(picked), json={"move": "next"}).status_code == 200
        assert _task_of(client, picked)["stage"] == "validation", _task_of(client, picked)
        # Незнакомый этап отсекается на границе, а не совпадением: подпись
        # вместо ключа — 400, и ход не состоялся.
        assert client.patch(
            _task_url(picked), json={"move": "next", "from": "проверка"}
        ).status_code == 400
        # Один `from` не просит ничего: он сверяет, а не двигает.
        assert client.patch(_task_url(picked), json={"from": "validation"}).status_code == 400
        assert _task_of(client, picked)["stage"] == "validation", _task_of(client, picked)

        # --- 6. Режим выключается, и тогда двигать нечего ---------------------
        assert client.delete(url).json() == {"task": None}, "режим не выключился"
        assert _task_of(client, agent_id) is None, _task_of(client, agent_id)
        assert client.patch(url, json={"move": "next"}).status_code == 409, "двинули выключенный"
        assert client.patch(url, json={"step": "х"}).status_code == 409, "вписали в выключенный"

        # --- 7. Состояние наружу едет одним местом — полем чата ---------------
        #
        # Отдельной ручки чтения нет: второе место «состояние наружу» обязано
        # было бы совпадать с первым. И поля конфига о режиме нет — «идёт ли
        # задача» говорит сама строка состояния, а второе поле давало бы вход
        # и выход мимо команд.
        chat = client.get(f"/api/agents/{picked}").json()
        assert "task" in chat and "workflow" not in chat, sorted(chat)
        assert client.get(_task_url(picked)).status_code == 405, "ручка чтения вернулась"
        # Включить режим ручке конфига нечем...
        bare = new_agent(client, label="мимо команды")
        assert client.patch(f"/api/agents/{bare}", json={"workflow": "plan"}).status_code == 400
        assert _task_of(client, bare) is None, "режим включили мимо команды"
        # ...и спрятать живую задачу тоже: тогда она воскресла бы перезапуском.
        assert client.patch(f"/api/agents/{picked}", json={"workflow": "off"}).status_code == 400
        assert _task_of(client, picked)["stage"] == "validation", "конфиг спрятал задачу"
        # Одно место — одно значение, и у **выгруженного** чата тоже: так
        # выглядит и вытеснение по потолку, и перезапуск сервера. Список
        # слева приезжает другой ветвью кода — у живого чата задачу отдаёт
        # он сам, у выгруженного строка из базы, — и назвать они обязаны одно.
        mine = _task_of(client, picked)
        assert REGISTRY._unload(picked) is True, "чат не был живым"
        assert REGISTRY._unload(bare) is True, "чат не был живым"
        cold = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
        assert cold[picked]["task"] == mine, (mine, cold[picked]["task"])
        assert cold[bare]["task"] is None, cold[bare]["task"]

        # --- 8. К модели за состоянием не ходят ни разу -----------------------
        #
        # Ни вызова, определяющего этап, ни разбора ответа: состояние двигает
        # человек, и чат от режима задачи не дорожает ни на токен.
        before_calls = len(_stub.CALLS)
        _talk(client, agent_id, 2)
        assert len(_stub.CALLS) == before_calls + 2, len(_stub.CALLS)
        assert not _service_calls(), "за состоянием задачи сходили к модели"

    return (
        f"{len(walked)} законных перехода прошли, {len(refused)} незаконных "
        "отказаны и состояние цело; «готово» терминально; пауза берётся с трёх "
        "этапов и возвращает туда же; умолчание этапа перебивается заданным, "
        "а смена этапа возвращает к умолчанию; выгруженный чат называет "
        "в списке ту же задачу, что отдаёт сам"
    )


@check("отказ базы не двигает этап и не прячет задачу")
def check_task_write_is_atomic():
    """Состояние задачи меняется в памяти **после** базы, а не до.

    Отказ базы — путь штатный (503 на занятом файле), и человек по нему
    получает отказ. Сдвинутый под отказом этап расходится с тем, что человек
    видит: шапка прежняя, а следующий обмен уезжает с правилом нового этапа,
    и перезапуск потом молча откатывает назад.

    Выход из режима той же монетой: снятие обязано быть целым. Флаг,
    прятавший недоудалённую строку, был не отказоустойчивостью, а
    расхождением с отложенным сроком.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("task-atomic")
    store = Store(path).init()

    def boom(*args, **kwargs):
        raise StoreBusyError("база занята другим процессом, повторите")

    try:
        chat = agent_module.Agent(AgentSpec(label="с задачей", model="stub/model"), store=store)
        chat.start_task("собрать ТЗ")
        chat.write_task({"step": "пишу проверку"})
        before = chat.task_items()
        assert before["stage"] == "planning" and before["step"] == "пишу проверку", before

        # --- 1. Ход: отказ оставляет этап прежним и в памяти, и в базе -------
        refused = False
        with patch.object(store, "save_task", boom):
            try:
                chat.move_task("next")
            except StoreBusyError:
                refused = True
        assert refused, "база отказала, а ход прошёл"
        assert chat.task_items() == before, (before, chat.task_items())
        assert store.load_task(chat.id)["stage"] == "planning", store.load_task(chat.id)
        # И промпт следующего обмена — с правилом прежнего этапа: сдвинутый
        # в памяти этап виден только здесь, шапка о нём не знает.
        head = chat.build_prompt("ещё")[0]["content"]
        assert STAGE_RULES["planning"] in head, head
        assert STAGE_RULES["execution"] not in head, head

        # --- 2. Выход из режима: снято целиком или не тронуто ----------------
        refused = False
        with patch.object(store, "clear_task", boom):
            try:
                chat.stop_task()
            except StoreBusyError:
                refused = True
        assert refused, "база отказала, а режим выключился"
        assert chat.task_items() == before, "задачу спрятали, а строка в базе цела"
        assert store.load_task(chat.id) is not None, "строку сняли, а человеку отдали отказ"
        assert STAGE_RULES["planning"] in chat.build_prompt("ещё")[0]["content"]

        # --- 3. Без отказа снимается целиком: и в памяти, и в базе -----------
        chat.stop_task()
        assert chat.task_items() == {}, chat.task_items()
        assert store.load_task(chat.id) is None, store.load_task(chat.id)
    finally:
        store.close()
    return (
        "отказ базы на ходе оставил этап прежним в памяти, в базе и в промпте; "
        "отказ на выходе не спрятал живую задачу; без отказа она снялась целиком"
    )


@check("этап едет системным сообщением, состояние задачи — врезкой")
def check_task_in_prompt():
    """Что из состояния задачи видит модель.

    Правило этапа — **системным** сообщением: это распоряжение, которому
    модель следует. Само состояние — врезкой ролью `user` с подписью
    `[задача]`: это сведения, на которые она опирается. Разделение то же,
    что у профиля и памяти.

    Пять правил и пять «ожидается» собираются из пяти настоящих запросов,
    а не читаются из таблиц в коде: одно правило на все этапы так и покраснеет.
    """
    _stub.install(reply=lambda m, i: f"ответ {i}")
    with TestClient(main.app) as client:
        # --- 1. Пять этапов — пять разных правил и пять разных «ожидается» ---
        rules, expects = {}, {}
        for stage in STAGES:
            agent_id = _at_stage(client, stage, "промпт")
            start = _frame(_frames(client, agent_id, "вопрос"), "start")
            prompt = start["resolved_messages"]
            head = prompt[0]
            assert head["role"] == "system", (stage, head)
            # Сравнивается само правило, а не сообщение целиком: подпись
            # этапа в заголовке различает блоки и без разных правил — и одно
            # правило на все пять прошло бы незамеченным.
            block = next(
                part for part in head["content"].split("\n\n")
                if part.startswith("[этап задачи: ")
            )
            title, _, rule = block.partition("\n")
            assert title == f"[этап задачи: {STAGE_LABELS[stage]}]", (stage, title)
            rules[stage] = rule
            # Номер врезки сверяется до обращения по нему: пустой или
            # посчитанный «суммой предыдущих» слот обязан краснеть
            # утверждением, а не падением по индексу.
            at = start["task_at"]
            assert isinstance(at, int) and at < len(prompt), (stage, at, len(prompt))
            insert = prompt[at]
            assert insert["role"] == "user", (stage, insert)
            assert insert["content"].startswith("[задача]"), (stage, insert)
            expects[stage] = next(
                line for line in insert["content"].splitlines()
                if line.startswith("ожидается: ")
            )
        assert len(set(rules.values())) == len(STAGES), rules
        assert all(text.strip() for text in rules.values()), rules
        assert len(set(expects.values())) == len(STAGES), expects
        assert "не продолжай" in rules["paused"], rules["paused"]

        # --- 2. Врезка едет при включённом режиме, даже с пустым шагом -------
        agent_id = _at_stage(client, "execution", "врезка")
        start = _frame(_frames(client, agent_id, "вопрос"), "start")
        empty = start["resolved_messages"][start["task_at"]]["content"]
        assert "шаг:" not in empty, empty
        assert "описание: врезка — собрать ТЗ" in empty, empty
        client.patch(_task_url(agent_id), json={"step": "пишу проверку"})
        start = _frame(_frames(client, agent_id, "вопрос"), "start")
        filled = start["resolved_messages"][start["task_at"]]["content"]
        assert "шаг: пишу проверку" in filled, filled

        # --- 3. Выключенный режим неотличим от невключавшегося ---------------
        #
        # Ни врезки, ни правила, ни системного сообщения: иначе всякая
        # последовательность ролей сдвинулась бы на сообщение.
        bare = new_agent(client, label="без задачи")
        start = _frame(_frames(client, bare, "вопрос"), "start")
        assert start["resolved_messages"] == [{"role": "user", "content": "вопрос"}], start
        assert start["task_at"] is None, start
        client.delete(_task_url(agent_id))
        after = _frame(_frames(client, agent_id, "вопрос"), "start")
        assert after["task_at"] is None, after
        assert not any("[задача]" in m["content"] for m in after["resolved_messages"]), after
        assert after["resolved_messages"][0]["role"] == "user", after["resolved_messages"][0]

        # --- 4. Все четыре врезки разом: номера сходятся с промптом ----------
        #
        # Номера берутся из длины собранного начала промпта, а не считаются
        # суммой: сумма, переписанная вторым местом, расходится молча.
        full = new_agent(client, label="все врезки", system="СИС",
                         strategy="summary", keep_last=2, compress_every=2)
        client.post(_task_url(full), json={"description": "собрать ТЗ"})
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{full}/working", json={"kind": "goal", "content": "ТЗ"})
        _talk(client, full, 3)
        start = _frame(_frames(client, full, "вопрос"), "start")
        prompt = start["resolved_messages"]
        assert [start[name] for name in agent_module.PROMPT_SLOTS] == [1, 2, 3, 4], start
        assert prompt[0]["content"].startswith("СИС\n\n[этап задачи"), prompt[0]
        assert prompt[1]["content"].startswith("[долговременная память]"), prompt[1]
        assert prompt[2]["content"].startswith("[факты о разговоре]"), prompt[2]
        assert prompt[3]["content"].startswith("[задача]"), prompt[3]
        assert prompt[4]["content"].startswith("[пересказ начала разговора"), prompt[4]
        assert len([m for m in prompt if m["role"] == "system"]) == 1, prompt

        # Сжатию состояние задачи не досталось: пересказ пересказывает
        # разговор, а не то, чем человек занят.
        compress = _service_calls("summary")[-1]["messages"]
        assert not any("[задача]" in m["content"] for m in compress), compress

        # --- 5. Ветка уносит состояние и живёт своей копией -------------------
        parent = _task_of(client, full)
        branch = client.post(f"/api/agents/{full}/fork", json={"at": 2}).json()["agents"][0]
        assert branch["task"] == parent, (parent, branch["task"])
        client.patch(_task_url(branch["id"]), json={"move": "next"})
        assert _task_of(client, branch["id"])["stage"] == "execution"
        assert _task_of(client, full) == parent, "ход в ветке двинул родителя"

    # --- 6. Перезапуск: продолжение без повторных объяснений ----------------
    _stub.reset()
    _stub.install(reply=lambda m, i: f"ответ {i}")
    path = _temp_db("task-restart")
    store = Store(path).init()
    chat = agent_module.Agent(AgentSpec(label="с задачей", model="stub/model"), store=store)
    chat.start_task("собрать ТЗ")
    chat.move_task("next")
    chat.write_task({"step": "пишу проверку"})
    _ask(chat, 4)
    before = chat.task_items()
    chat_id = chat.id
    assert before["stage"] == "execution" and before["step"] == "пишу проверку", before

    with _restarted(store, chat_id) as (again, revived):
        assert revived.task_items() == before, (before, revived.task_items())
        prompt = revived.build_prompt("ещё")
        assert prompt[0]["role"] == "system" and "выполнение" in prompt[0]["content"], prompt[0]
        assert prompt[1]["content"].startswith("[задача]"), prompt[1]
        # Состояние лежит в своей таблице, а не строкой в ленте: иначе его
        # стирал бы каждый обмен — `save_history` начинается с `DELETE`.
        assert not any("[задача]" in r[2] for r in again.message_rows(chat_id)), "врезка в ленте"
    return (
        "пять этапов — пять разных правил в системном сообщении и пять разных "
        "«ожидается» во врезке; врезка едет и с пустым шагом; выключенный режим "
        f"уходит без единого сообщения; {len(agent_module.PROMPT_SLOTS)} врезки "
        "встали номерами 1–4; ветка унесла состояние копией, перезапуск его сохранил"
    )


def _lifecycle(client, agent_id: str, text: str = "вопрос") -> tuple[dict, str]:
    """Кадр `start` обмена и блок жизненного цикла из его системного
    сообщения — пустая строка, если блока в промпте нет вовсе."""
    start = _frame(_frames(client, agent_id, text), "start")
    head = start["resolved_messages"][0] if start["resolved_messages"] else {}
    parts = head.get("content", "").split("\n\n") if head.get("role") == "system" else []
    return start, next((p for p in parts if p.startswith("[жизненный цикл задачи]")), "")


@check("модель знает автомат: блок собран из таблицы, чужой работы не делает")
def check_task_lifecycle():
    """Что модель знает о машине состояний, и что из этого доезжает до ответа.

    Блок `[жизненный цикл задачи]` собран **из `TRANSITIONS`**, а не выписан
    руками: таблица остаётся единственным источником правды, и добавленное
    ребро доезжает до модели само. Ходы в блоке — те же, что законны у ручки:
    считает их одна и та же таблица.

    Едет блок **системным** сообщением, рядом с правилом этапа: это
    распоряжение, а не сведения. Своей врезки он не заводит — номера врезок
    памяти от него не сдвигаются, и выключенный режим по-прежнему
    неотличим от невключавшегося.

    Последнее утверждение — про **ответ**: заглушка читает блок там же, где
    прочитала бы его модель, и на просьбу о работе чужого этапа отвечает
    отказом, называя этап и ход. Словами живой модели заглушка не
    притворяется: обещать она вправе только то, что знание машины доезжает
    до ответа, а не теряется по дороге.
    """
    _stub.install(reply=_stage_aware)
    with TestClient(main.app) as client:
        # --- 1. Ходы в блоке — те же, что законны у ручки -------------------
        lines, blocks = {}, {}
        for stage in STAGES:
            agent_id = _at_stage(client, stage, "цикл")
            _, blocks[stage] = _lifecycle(client, agent_id)
            # Отметка «сейчас здесь» берётся с запасным пустым значением:
            # её отсутствие обязано краснеть утверждением, а не падением.
            lines[stage] = next(
                (line for line in blocks[stage].splitlines() if " — сейчас здесь: " in line),
                "",
            )
            assert lines[stage].startswith(f"{STAGE_LABELS[stage]} — сейчас здесь: "), (
                stage, lines[stage]
            )
            # Законные ходы сверяются с теми, что называет отказ ручки:
            # обоих считает одна таблица, и разойтись им негде.
            allowed = [move for move in main._allowed_from(stage).split(", ") if move]
            named = re.findall(r"\((/[a-z-]+)\)", lines[stage])
            assert named == [MOVE_COMMANDS[move] for move in allowed], (stage, lines[stage])
            for move in MOVES:
                if move not in allowed:
                    assert MOVE_COMMANDS[move] not in lines[stage], (stage, move, lines[stage])
            # Все пять этапов названы: чтобы назвать чужой этап, модель
            # обязана его знать, а не догадаться по соседям.
            assert all(STAGE_LABELS[s] in blocks[stage] for s in STAGES), (stage, blocks[stage])
        assert len(set(blocks.values())) == len(STAGES), blocks
        assert "ходов отсюда нет" in lines["done"], lines["done"]
        assert not any("ходов отсюда нет" in lines[s] for s in STAGES if s != "done"), lines

        # --- 2. Новое ребро в таблице доезжает до модели само ----------------
        #
        # Выпиши блок руками — и здесь он назвал бы прежнее: модель звала бы
        # ход, которого нет, или молчала бы о том, который есть.
        closed = _at_stage(client, "done", "ребро")
        with patch.dict(TRANSITIONS, {("done", "next"): "planning"}):
            _, block = _lifecycle(client, closed)
            assert "готово — сейчас здесь: дальше (/task-next) → планирование" in block, block
        _, block = _lifecycle(client, closed)
        assert "ходов отсюда нет" in block, block

        # --- 3. Выключенный режим уходит без блока и без системного ---------
        #
        # Проверяется до врезок памяти: непустой слой встаёт в промпт и
        # голому чату — и «уходит ни с чем» держалось бы уже не блоком.
        bare = new_agent(client, label="без задачи")
        start, block = _lifecycle(client, bare)
        assert block == "", block
        assert start["resolved_messages"] == [{"role": "user", "content": "вопрос"}], start

        # --- 4. Системным, одним сообщением, и врезок не прибавилось --------
        full = new_agent(client, label="все врезки", system="СИС",
                         strategy="summary", keep_last=2, compress_every=2)
        client.post(_task_url(full), json={"description": "собрать ТЗ"})
        client.post("/api/memory", json={"kind": "profile", "content": "пишу на Kotlin"})
        client.post(f"/api/agents/{full}/working", json={"kind": "goal", "content": "ТЗ"})
        _talk(client, full, 3)
        start, block = _lifecycle(client, full)
        prompt = start["resolved_messages"]
        assert block and prompt[0]["role"] == "system", prompt[0]
        assert prompt[0]["content"].startswith("СИС\n\n[этап задачи"), prompt[0]
        assert len([m for m in prompt if m["role"] == "system"]) == 1, prompt
        assert not any(
            m["role"] != "system" and "[жизненный цикл" in m["content"] for m in prompt
        ), prompt
        assert agent_module.PROMPT_SLOTS == (
            "memory_at", "working_at", "task_at", "summary_at"
        ), agent_module.PROMPT_SLOTS
        assert [start[name] for name in agent_module.PROMPT_SLOTS] == [1, 2, 3, 4], start

        # Сжатию блок не достаётся: служебный вызов пересказывает разговор,
        # а не отвечает собеседнику.
        compress = _service_calls("summary")[-1]["messages"]
        assert not any("[жизненный цикл" in m["content"] for m in compress), compress

        # --- 5. Главное: ассистент не делает работу чужого этапа -------------
        #
        # Утверждение об **ответе**, а не о составе запроса: просьбу о коде
        # на планировании заглушка отклоняет, называя этап и ход, — и оба
        # слова взяты ею из самого блока.
        before = len(_stub.CALLS)
        asking = _at_stage(client, "planning", "отказ")
        refused = _frame(_frames(client, asking, "напиши код"), "done")["text"]
        knowing = _stub.CALLS[-1]["messages"]
        # Тот же ярлык — тот же промпт: два чата различает только блок.
        with patch.object(agent_module, "lifecycle_block", lambda stage: ""):
            blind = _at_stage(client, "planning", "отказ")
            obeyed = _frame(_frames(client, blind, "напиши код"), "done")["text"]
        ignorant = _stub.CALLS[-1]["messages"]
        assert "«выполнение»" in refused and "/task-next" in refused, refused
        assert "def main" not in refused, refused
        assert "def main" in obeyed and "выполнение" not in obeyed, obeyed
        assert refused != obeyed, refused

        # И разошлись они **от блока**: всё остальное в двух запросах
        # совпадает слово в слово, а обращений к модели поровну.
        stripped = [dict(m) for m in knowing]
        stripped[0]["content"] = "\n\n".join(
            part for part in stripped[0]["content"].split("\n\n")
            if not part.startswith("[жизненный цикл задачи]")
        )
        assert stripped == ignorant, (stripped, ignorant)
        assert len(_stub.CALLS) == before + 2, (before, len(_stub.CALLS))

    return (
        f"пять этапов — пять разных блоков, ходы в каждом те же, что у ручки; "
        f"новое ребро в таблице блок назвал; врезок по-прежнему "
        f"{len(agent_module.PROMPT_SLOTS)}, системное сообщение одно; "
        f"на планировании ответ — {refused!r}, без блока — {obeyed!r}"
    )


@check("токены служебного вызова попадают в итог по чату")
def check_service_tokens_counted():
    """Вызов на сжатие тоже уехал в модель и тоже оплачен. Экономия,
    не вычитающая его стоимость, — враньё, поэтому он входит в `usage_total`
    отдельным слагаемым: в истории его нет, и сам собой он туда не попадёт.

    Числа служебного вызова здесь заведомо больше всех остальных вместе
    взятых: потеряйся они, сумма разошлась бы на порядок.

    Служебных вызовов было два, пока рабочую память вёл агент. Второе
    слагаемое ушло вместе с ним — и ушло целиком: цену, которой больше
    не платят, считать нечего.
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
        # Память выключена у всех трёх: проверка перебирает пути **обмена**,
        # и служебные вызовы в этот перебор не входят — за тело вызова
        # на ведение памяти отвечает своя проверка.
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
        # И то же самое с другой стороны: путь записи обязан **открывать**
        # транзакцию, а не только не трогать соединение по известным именам.
        # Список запрещённых форм ловит те обходы, которые уже видели;
        # способов достать голое соединение больше, чем их в списке, и
        # мутация «миграция идёт мимо tx()» прошла мимо него зелёной.
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
            conn.execute(
                "INSERT INTO invariants (kind, content, banned, at) "
                "VALUES ('stack', ?, ?, 0)",
                (key, key),
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
