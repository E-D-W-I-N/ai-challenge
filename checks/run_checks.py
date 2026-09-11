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
import tempfile

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


@check("на чистом старте список чатов пуст")
def check_empty_start():
    """Чаты заводит пользователь: ни заготовок, ни чата «по умолчанию».
    Пустой список — это работа клиента, и его половина в browser_check.js."""
    with TestClient(main.app) as client:
        assert client.get("/api/agents").json()["agents"] == []
        agent = client.post("/api/agents", json={}).json()["agents"][0]
        assert client.delete(f"/api/agents/{agent['id']}").status_code == 200
        assert client.get("/api/agents").json()["agents"] == []
    return "до первого чата список пуст и становится пустым снова"


@check("список чатов идёт в порядке создания, новый — последним")
def check_list_order():
    """Порядок списка слева не стерёг никто.

    Развернул сортировку реестра наоборот — все проверки остались зелёными,
    хотя список слева перевернулся бы целиком. Порядок здесь видимое
    поведение, а не деталь: чаты нумеруются возрастающе, и «Новый чат 7»
    выше «Новый чат 3» — это не тот список, который завёл пользователь.

    Клиентская половина — в `checks/browser_check.js`: кнопка «Новый чат»
    добавляет строку в конец, а не в начало.
    """
    with TestClient(main.app) as client:
        made = [client.post("/api/agents", json={}).json()["agents"][0] for _ in range(4)]
        listed = client.get("/api/agents").json()["agents"]
        assert [a["id"] for a in listed] == [a["id"] for a in made], (
            [a["label"] for a in listed], [a["label"] for a in made]
        )

        # Удаление из середины порядок остальных не трогает.
        client.delete(f"/api/agents/{made[1]['id']}")
        after = [a["id"] for a in client.get("/api/agents").json()["agents"]]
        assert after == [made[0]["id"], made[2]["id"], made[3]["id"]], after

        # Разговор в старом чате не поднимает его наверх: список не по свежести.
        _stub.install(reply="ок")
        client.post(f"/api/agents/{made[0]['id']}/messages", json={"text": "?"})
        talked = [a["id"] for a in client.get("/api/agents").json()["agents"]]
        assert talked == after, talked

        # А новый встаёт последним.
        fresh = client.post("/api/agents", json={}).json()["agents"][0]
        tail = [a["id"] for a in client.get("/api/agents").json()["agents"]]
        assert tail[-1] == fresh["id"], tail
    return "порядок создания, удаление из середины и разговор его не меняют"


@check("у свежего чата нет системного промпта, и в теле его нет вовсе")
def check_new_chat_is_blank():
    """Смотрим не на поле в ответе ручки, а на то, что ушло в модель: пустой
    `system` обязан пропасть из промпта целиком. Пустая строка в роли `system` —
    это не «промпта нет», это заданный пустой промпт."""
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
    return "свежий чат пуст, промпт появляется только заданный руками"


# --- 3. выпиленное осталось выпиленным ----------------------------------------

# Слово и чем оно было. Одна таблица вместо шести проверок, обходивших файлы
# в поисках одних и тех же слов.
#
# Скоупа у правила нет намеренно, и списка расширений тоже: оба были и оба
# сужались молча — `scenario` уехал на сервер, `draft` в конфиг, а из «.py,
# .js, .html, .css, .md» довольно было убрать `.css`, чтобы вернуть
# `list-group` в стили зелёным. Поэтому правило смотрит **во все файлы под
# контролем версий**, двоичные тоже, а сузить его можно только записью
# в ONLY — с файлом и причиной. Забытая запись делает правило шире, а не уже.
#
# Поиск регистронезависимый: слова вроде «судья» и «колонка» жили в клиенте
# текстом для пользователя, а он пишется с большой буквы.

# Единственный файл, которому запретные слова положены: он их и запрещает.
# Собирать их из кусков ("PRESET" + "_CHATS") оказалось мало: с
# регистронезависимым поиском `autoname` находил `AutoName` в соседней строке
# таблицы, да и приём хрупкий — достаточно раз написать слово целиком.
# Поблажка только для слов из таблицы: имя референса собрано из кусков
# и здесь буквально не встречается, поэтому его обход идёт по ВСЕМ файлам,
# включая этот, — иначе в нём одном имя можно было бы спрятать.
SELF = "checks/run_checks.py"

# Сузить правило можно только здесь, и сужение обязано быть оправданным: слово
# должно законно встречаться за пределами скоупа — иначе это выключенное
# правило, ждущее, когда слово вернётся, и проверка это скажет. Осталось одно:
# «children» вне реестра — свойство DOM-узла. Оно стережётся ещё и поведением
# (check_removed_endpoints): греп ловит упоминание в комментарии, но не поле,
# выставленное из кода, а поведение — наоборот.
ONLY = {
    "children": ("app/registry.py", "вне реестра это свойство DOM-узла"),
}

# Растяжка: сужения заморожены вместе с файлами. Проверку, стерегущую саму
# себя, написать нельзя — но тронуть придётся и эту строку, и в дифф попадёт
# «я ослабляю проверку». Дублирование здесь и есть механизм.
NARROWED = (("children", "app/registry.py"),)

SHOWCASE_CAPTIONS = (
    "Единственное отличие",
    "База пары",
    "ступень",
    "Панель экспертов",
)
"""Подписи выпиленной витрины сценариев — те, что объясняли интерфейс сам себе.

Стерегутся дважды и по-разному, потому что вернуться могут двумя путями.
Таблица `GONE` ищет их в исходниках: так ловится подпись, вернувшаяся
в разметку или в клиент, — она не попадёт ни в один ответ ручки. Обход тела
ответа в `check_removed_endpoints` ищет их в том, что реально уехало наружу:
так ловится фраза, которой в файлах буквально нет — собранная по дороге или
приехавшая из конфига. Ни одна из двух проверок не покрывает случай другой.
"""

GONE = [
    # заготовки дней 1–5: файл описаний, поднятие на старте, черновик
    *[(f"День {n}", "заготовки дней 1–5") for n in range(1, 6)],
    ("PRESET_CHATS", "заготовки дней 1–5"),
    ("bootstrap_chats", "заготовки дней 1–5"),
    ("draft", "черновик с подставленным вопросом"),
    # сценарии: прогон, судья, зависимости, серии
    ("Scenario", "сценарии"),
    ("judge", "модель-судья"),
    ("depends_on", "зависимости сценариев"),
    ("repeats", "серии прогонов"),
    ("прогон", "сценарии"),
    ("судья", "модель-судья"),
    ("колонк", "витрина сценариев"),
    # родительские связи жили ради субагентов прогона
    ("parent_id", "каскад субагентов"),
    ("kill_children", "каскад субагентов"),
    ("children", "каскад субагентов"),
    # витрина: группы, пояснения, кнопка очистки, автоимя
    ("list-group", "группы чатов"),
    ("state.groups", "группы чатов"),
    ("note", "пояснения из описаний сценариев"),
    ("Очистить все", "кнопка «Очистить все чаты»"),
    ("clear-all", "кнопка «Очистить все чаты»"),
    ("clearAll", "кнопка «Очистить все чаты»"),
    ("/api/agents/reset", "ручка очистки"),
    ("maybeAutoName", "автоимя по первому сообщению"),
    ("chatTitle", "автоимя по первому сообщению"),
    ("autoname", "автоимя по первому сообщению"),
    ("карандаш", "подпись, объясняющая интерфейс сам себе"),
    # управление контекстом: оно станет заданием Дня 9 и вернётся осознанно
    ("history_limit", "окно памяти в конфиге агента"),
    ("history-limit", "ключ CLI, задававший окно памяти"),
    ("MAX_STORED_MESSAGES", "потолок хранимых реплик"),
    ("окно памяти", "поле панели «Окно памяти, сообщений»"),
    # рельс справа от ленты: полоса точек по числу сообщений
    ("rail", "рельс с точками справа от ленты"),
    # витрина сценариев объясняла себя подписями — их не должно быть в коде
    *[(caption, "пояснение витрины") for caption in SHOWCASE_CAPTIONS],
]

# Имя клиента-референса: он был инструментом разработки, а не частью продукта,
# и не должен встречаться нигде — ни в коде, ни в именах файлов, ни в двоичных.
REFERENCE_NAME = "o" + "mlx"

# Файлы, которых не должно быть вовсе.
GONE_FILES = ("day.py", "checks/_daysrc.py", "app/commands.py")


def _tracked() -> list[str]:
    names = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    return [n for n in names if os.path.isfile(os.path.join(ROOT, n))]


def _read(name: str) -> str:
    """Содержимое файла под ROOT. Двоичное — с заменой: пропустить файл
    из-за кодировки значит сузить обход, только через исключение."""
    with open(os.path.join(ROOT, name), "rb") as handle:
        return handle.read().decode("utf-8", "replace")


def _files_for(word: str, tracked: list[str]) -> list[str]:
    """Где действует правило: всё под контролем версий, кроме самой проверки.

    Или один файл из ONLY, если правило сужено.
    """
    only, _ = ONLY.get(word.lower(), (None, ""))
    files = [n for n in ([only] if only else tracked) if n != SELF]
    assert files, f"правилу «{word}» не осталось ни одного файла"
    return files


def _reference_files(tracked: list[str]) -> list[str]:
    """Имя референса ищется везде, без единого исключения — включая SELF."""
    return list(tracked)


def _found(word: str, body: str) -> bool:
    """Регистр не спасает: слово есть, как бы его ни написали."""
    return word.lower() in body.lower()


@check("выпиленное осталось выпиленным: ни слова в исходниках")
def check_nothing_left_behind():
    """Печатает **все** совпадения разом, а не первое: раньше шесть проверок
    падали по одной с понятным именем, и от слияния диагностика не должна
    стать хуже.

    Перед обходом — самопроверка. Потерянное правило молчит ровно так же,
    как соблюдённое: каждое правило обязано доказать, что вообще способно
    сработать, а каждая оговорка — что она рабочая, а не прикрывает
    настоящий след.
    """
    tracked = _tracked()
    body_of = {n: _read(n) for n in tracked}

    assert SELF in tracked, f"{SELF} не под контролем версий — поблажка про себя не сработает"
    words = {w.lower() for w, _ in GONE}
    assert len(words) == len(GONE), "в таблице повторяются слова"

    # 0. Обход покрывает всё, что под контролем версий. Считать слепые файлы
    #    от того же списка, который вернул `_tracked()`, мало: так видны
    #    сужения ниже него, но не он сам — отбросить `.css` прямо в `_tracked()`
    #    проходило зелёным. Поэтому сверяемся со свежим `git ls-files`, взятым
    #    здесь и мимо всех помощников.
    listed = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.split()
    assert listed, "git ls-files не вернул ничего — сверять обход не с чем"
    on_disk = [n for n in listed if os.path.isfile(os.path.join(ROOT, n))]
    dropped = sorted(set(on_disk) - set(tracked))
    assert not dropped, (
        f"из обхода выпали файлы под контролем версий: {', '.join(dropped)} — "
        "список подрезан в самом `_tracked()`"
    )

    # Не-UTF-8 обход обязан прочитать, а не уронить: иначе двоичный файл
    # выпадет из него молча. Проверяем настоящим чтением, а не верой
    # в аргумент "replace"; имя абсолютное, поэтому ROOT из `_read` отпадает
    # и в репозитории ничего не появляется.
    with tempfile.NamedTemporaryFile(suffix=".bin") as probe:
        probe.write(b"\xff\xfe\x00" + "хвост".encode("utf-8"))
        probe.flush()
        assert _read(probe.name).endswith("хвост"), "обход падает на не-UTF-8"

    wide = [w for w, _ in GONE if w.lower() not in ONLY]
    assert wide, "сужены все правила до одного — обходить стало нечего"
    for word in wide:
        seen = set(_files_for(word, tracked))
        blind = [n for n in tracked if n != SELF and n not in seen]
        assert not blind, (
            f"правило «{word}» не смотрит в {', '.join(sorted(blind))} — "
            "обход сузился; несуженное правило обязано видеть всё под контролем версий"
        )
    blind_ref = [n for n in tracked if n not in _reference_files(tracked)]
    assert not blind_ref, (
        f"имя референса не ищется в {', '.join(sorted(blind_ref))} — "
        "его обход идёт по всем файлам, включая сам файл проверки"
    )

    # 1. Правило срабатывает на своём же слове, в любом регистре.
    for word, what in GONE:
        _files_for(word, tracked)
        for probe in (word, word.upper(), word.lower(), f"// хвост {word.title()} хвост"):
            assert _found(word, probe), (
                f"правило «{word}» ({what}) не сработало бы даже на {probe!r} — "
                "оно ничего не стережёт"
            )
    assert _found(REFERENCE_NAME, REFERENCE_NAME.upper()), "правило про референс не сработает"

    # 2. Сужений ровно столько, сколько заявлено, и все они про живые правила.
    narrowed_now = tuple(sorted((w, only) for w, (only, _) in ONLY.items()))
    assert narrowed_now == tuple(sorted(NARROWED)), (
        f"сужения изменились: было {sorted(NARROWED)}, стало {list(narrowed_now)}. "
        "Сужение правила ослабляет проверку — если это осознанно, поправьте NARROWED"
    )
    stale = [w for w in ONLY if w not in words]
    assert not stale, f"скоуп есть, а правила нет: {', '.join(stale)}"

    # 3. Каждое сужение оправдано. Скоуп нужен затем, что слово законно
    #    встречается ЗА его пределами; если не встречается — сужение
    #    бессмысленно, и это выключенное правило, ждущее, когда слово вернётся.
    pointless = []
    for word, (only, why) in ONLY.items():
        assert only in body_of, f"скоуп «{word}» указывает на файл вне репозитория: {only}"
        outside = [
            n for n in tracked
            if n != SELF and n != only and _found(word, body_of[n])
        ]
        if not outside:
            pointless.append(f"«{word}» ({why}) за пределами {only} не встречается")
    assert not pointless, (
        "сужение ничем не оправдано, снимите его — иначе оно молча прикроет "
        "настоящий след:\n  " + "\n  ".join(pointless)
    )

    # 4. Сам обход.
    hits = []
    for word, what in GONE:
        for name in _files_for(word, tracked):
            if _found(word, body_of[name]):
                hits.append(f"{name}: «{word}» ({what})")

    # Референс — по всем файлам разом, включая имена, двоичные и сам файл
    # проверки: имя собрано из кусков и буквально в нём не встречается.
    for name in _reference_files(tracked):
        if REFERENCE_NAME in name.lower():
            hits.append(f"{name}: имя референса в имени файла")
        elif _found(REFERENCE_NAME, body_of[name]):
            hits.append(f"{name}: имя референса в содержимом")

    for path in GONE_FILES:
        assert not os.path.exists(os.path.join(ROOT, path)), f"{path} жив"

    assert not hits, f"осталось {len(hits)} упоминаний:\n  " + "\n  ".join(hits)
    return (
        f"{len(GONE) + 1} правил по {len(tracked)} файлам под контролем версий, "
        f"регистр не спасает, сужено {len(ONLY)} — ни одного совпадения"
    )


@check("ручек выпиленных механик нет, и мёртвых полей наружу тоже")
def check_removed_endpoints():
    """Поведенческая половина: ручки отвечают 404, а поля не торчат наружу."""
    with TestClient(main.app) as client:
        for path in ("/api/scenarios", "/api/run/0"):
            assert client.get(path).status_code == 404, path
        assert client.post("/api/scenarios/0/agents").status_code == 404
        # 405 — путь совпал с GET /api/agents/{id}: ручки reset всё равно нет.
        assert client.post("/api/agents/reset").status_code in (404, 405), "ручка reset жива"

        agent = client.post("/api/agents", json={}).json()["agents"][0]
        for gone in (
            "parent_id", "group", "note", "origin", "draft", "children", "history_limit"
        ):
            assert gone not in agent, f"наружу торчит мёртвое поле {gone}"

        # Окно памяти ушло и из ручки правки: панель такого поля не показывает,
        # а PATCH принимает ровно то, что в панели есть.
        assert client.patch(
            f"/api/agents/{agent['id']}", json={"history_limit": 4}
        ).status_code == 400, "PATCH всё ещё принимает history_limit"

        # Каскад субагентов — ещё и поведением, не только словом в исходнике:
        # греп по `children` сужен до реестра (в клиенте это свойство DOM-узла)
        # и упоминание в комментарии ловит, а поле, выставленное из кода, — нет.
        for owner, name in ((REGISTRY, "реестр"), (REGISTRY.require(agent["id"]), "агент")):
            for attr in ("children", "parent", "parent_id", "kill_children"):
                assert not hasattr(owner, attr), f"у {name} снова есть {attr}"

        assert "groups" not in client.get("/api/agents").json(), "групп в ответе быть не должно"

        # Подписи витрины — по тому, что реально уехало наружу, а не по тому,
        # что написано в файлах. Фраза может не встречаться в исходнике вовсе:
        # достаточно собрать её по дороге, и греп по коду промолчит. Смотрим
        # оба ответа, куда попадает `Agent.as_dict`, и служебный.
        agent.update(
            client.patch(f"/api/agents/{agent['id']}", json={"label": "чат"}).json()
        )
        bodies = {
            "GET /api/agents": client.get("/api/agents").text,
            "GET /api/agents/{id}": client.get(f"/api/agents/{agent['id']}").text,
            "GET /api/health": client.get("/api/health").text,
        }
    for where, body in bodies.items():
        for caption in SHOWCASE_CAPTIONS:
            assert caption.lower() not in body.lower(), (
                f"{where}: наружу уехало пояснение витрины «{caption}»"
            )
    return (
        f"четыре ручки отдают 404/405, мёртвых полей нет, "
        f"{len(SHOWCASE_CAPTIONS)} подписей витрины нет в теле {len(bodies)} ответов"
    )


@check("в модель уезжает вся история: ни хвоста, ни отсечки по росту")
def check_whole_history_goes_to_model():
    """На месте выпиленного окна памяти.

    Прежняя проверка стерегла обрезку: окно `history_limit` резало хвост,
    а `MAX_STORED_MESSAGES` — само хранилище. Обрезки больше нет, и стеречь
    надо ровно обратное: что в промпт попадают **все** реплики и что рост
    истории ничего из неё не выбрасывает.

    Пороги взяты заведомо выше прежних отсечек — 20 сообщений в окне
    по умолчанию и 400 хранимых: вернись любая из них, здесь станет красно.
    """
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

        # true — не число. В Python True это int, и без отдельной проверки
        # «temperature: true» уехало бы к провайдеру единицей.
        for bad in ({"temperature": True}, {"max_tokens": True}):
            assert client.patch(f"/api/agents/{full}", json=bad).status_code == 400, bad
            born = client.post("/api/agents", json={"agent": {"model": "stub/m", **bad}})
            assert born.status_code == 400, (bad, born.text)
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
    """Смотрим не на ответ ручки, а на то, что реально ушло в модель: промпт
    и каждое поле панели."""
    _stub.install(reply="ок")
    with TestClient(main.app) as client:
        agent_id = new_agent(client, model="старая/модель", system="СТАРЫЙ ПРОМПТ")
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "первый"})
        assert _stub.CALLS[-1]["messages"][0]["content"] == "СТАРЫЙ ПРОМПТ"

        patched = client.patch(
            f"/api/agents/{agent_id}",
            json={"system": "НОВЫЙ ПРОМПТ", **PANEL_FIELDS},
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
    # Новый промпт встал на место старого, а прошлый обмен из истории никуда
    # не делся: правка панели меняет конфиг, а не переписку.
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"], sent
    assert sent[1]["content"] == "первый", sent[1]
    return "промпт, модель и все параметры уехали новыми, история на месте"


@check("правка панели во время генерации не теряется")
def check_patch_during_generation():
    """Править во время генерации можно: обмен снимает слепок конфига в начале
    и живой конфиг после этого не читает, поэтому текущий ответ правка исказить
    не может. Действует она со следующего сообщения."""

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

    return "PATCH во время генерации принят, текущий ответ цел, следующий — новый"


@check("обмен идёт целиком на одном конфиге: смешанного запроса не бывает")
def check_config_snapshot():
    """Конфиг читается в двух точках: промпт собирает `build_prompt`, тело —
    `build_payload`, и между ними стоит `yield` события `start`. Правка,
    попавшая в это окно, дала бы смешанный запрос — новую модель со старым
    системным промптом.

    Шагаем генератор руками: `__anext__` останавливает его ровно в окне,
    и правим конфиг оттуда. Со слепком уезжает один конфиг целиком.
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
    а не запоминается при создании.
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


@check("provider.require_parameters стоит на каждом вызове")
def check_require_parameters():
    """Стерёг его только текст предупреждения в панели, а не тело запроса.

    Убрал `require_parameters` из build_payload — ни одна проверка не
    покраснела, хотя это правило, на котором держится весь смысл панели:
    без него OpenRouter вправе увести запрос к провайдеру, который молча
    проигнорирует temperature или stop, и стенд покажет неправду.

    Проверяется на всех путях: обычное сообщение, перегенерация, чат
    с закреплённым поставщиком и чат без единого заданного параметра.
    """
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
        provider = call["payload"].get("provider")
        assert provider and provider.get("require_parameters") is True, (
            f"вызов ушёл без provider.require_parameters: {call['payload'].get('provider')!r}"
        )
    assert _stub.CALLS[-1]["payload"]["provider"]["order"] == ["openai"], _stub.CALLS[-1]["payload"]

    # И то же самое напрямую, без веб-слоя: правило живёт в build_payload,
    # а не в ручке, поэтому CLI и любой другой вызывающий получают его тоже.
    from app.llm import build_payload

    payload = build_payload(AgentSpec(label="без веба", model="stub/m"))
    assert payload["provider"]["require_parameters"] is True, payload["provider"]
    return "4 вызова через ручки и один напрямую — все с require_parameters"


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
        {"response_format": {"type": "json_object"}},
        {"temperature": 0.0, "top_p": 0, "max_tokens": 7},
    ]
    watched = ("stop", "system", "response_format", *SAMPLING_FIELDS)
    with TestClient(main.app) as client:
        for case in cases:
            created = client.post(
                "/api/agents", json={"agent": {"model": "stub/model", **case}}
            )
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
        for bad in ({"stop": "СТОП"}, {"top_k": 0.5}, {"max_tokens": 0}):
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


@check("панель правит живого агента: модель, промпт, имя")
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
            },
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["model"] == "stub/new" and body["label"] == "Переименован"
        client.post(f"/api/agents/{agent_id}/messages", json={"text": "привет"})
        assert client.patch(f"/api/agents/{agent_id}", json={"note": "х"}).status_code == 400

    call = _stub.CALLS[-1]
    assert call["model"] == "stub/new", call["model"]
    assert call["messages"][0]["content"] == "новый промпт", call["messages"][0]
    return "PATCH меняет модель, промпт и имя; чужие поля не принимаются"


# --- 6. лента: рассуждение и перегенерация ------------------------------------


@check("стенограмма — это ровно диалог, и в ней есть всё, что рисует клиент")
def check_transcript_shape():
    """Формат стенограммы не стерёг никто: поля можно было убрать молча.

    Клиент рисует ленту **из ответа ручки**, а не из того, что дорисовал
    по дороге: после каждого обмена он перечитывает агента и перерисовывает
    всё заново. Значит контракт стенограммы — часть поведения, и он такой:
    в ней ровно реплики диалога, по одной на ход, в порядке разговора,
    и у каждой есть поля, которые клиент читает.

    Клиентская половина — в `checks/browser_check.js`, блоком «лента рисуется
    из стенограммы»: по реплике на узел, текст, имя модели, провайдер,
    рассуждение, ошибка.
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
    """`ttft_ms` стоит на первом токене **ответа**: на reasoning-модели это
    момент, когда она додумала, а не когда заговорила. Первый токен вообще
    считается отдельно, и на плитке показан именно он."""
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
    """Пара снимается с истории до вызова. Вызов упал, не отдав ни токена, —
    вернуть её обязаны: восстанавливать было бы неоткуда, историю хранит
    сервер."""
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


@check("обрыв до первого события не теряет снятую перегенерацией пару")
def check_regenerate_disconnect():
    """`take_last_exchange` зовётся в обработчике, до `_stream`. Отвались
    клиент до первого опроса — генератор отменится, не начав выполняться,
    и его `finally` не сработает никогда. Возврат поэтому висит на потоке,
    там же, где снятие брони."""
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

    return f"{first['label']}, {second['label']}, после удаления — {third['label']}"


@check("переименование и удаление живут в списке слева")
def check_list_actions():
    """Серверная половина: PATCH меняет имя, пустое имя отвергается, DELETE убирает.

    Клиентская половина — в `checks/browser_check.js`, блоком «переименование
    чата в списке слева»: клик по кнопке, Enter, Escape, потеря фокуса,
    пустое имя. Раньше она была грепом по исходнику («в app.js есть строка
    function startRename»), то есть описывала реализацию: переименование
    ломалось, не тронув ни одной из тех строк, и греп оставался зелёным.
    """
    assert 'id="f-label"' not in read("app/static/index.html"), "имя всё ещё правится в панели"

    with TestClient(main.app) as client:
        agent_id = new_agent(client, label="Было")
        renamed = client.patch(f"/api/agents/{agent_id}", json={"label": "Стало"})
        assert renamed.status_code == 200 and renamed.json()["label"] == "Стало"
        assert client.patch(f"/api/agents/{agent_id}", json={"label": "  "}).status_code == 400
        assert client.delete(f"/api/agents/{agent_id}").status_code == 200
        assert client.get(f"/api/agents/{agent_id}").status_code == 404
    return "PATCH меняет имя, пустое отвергается, DELETE убирает; поля имени в панели нет"


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

    free = catalog._normalize(
        {"id": "x/y:free", "pricing": {"prompt": "0", "completion": "0"}, "context_length": 8000}
    )
    assert free["is_free"] is True, free

    # Отбор при этом не вернулся: чат показывает каталог целиком.
    assert not hasattr(catalog, "filter_models"), "фильтры каталога вернулись"

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
            # по ней плитка «Контекст» показывает заполнение. Стерёг её
            # только сам каталог: `context_length` можно было не передать
            # ни при создании, ни при смене модели, и всё оставалось зелёным.
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
    return "три поля на месте, отбор не вернулся, длина контекста доезжает до метрик"


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
    """Лента — колоночный флексбокс, и её элементы по умолчанию сжимаются.
    У карточки к тому же `overflow: hidden`, из-за чего её автоматический
    минимальный размер равен нулю: без запрета сжатия она давится в полоску
    вместо прокрутки."""
    css = read("app/static/style.css")
    block = css[css.index(".chat {") : css.index(".feed {")]
    for rule in ("min-height: 0", "overflow: hidden", "flex-direction: column"):
        assert rule in block, f"у .chat нет правила {rule}"
    # `min-height: 0` переехало с обёртки на саму ленту: обёртка была нужна
    # только рельсу и ушла вместе с ним, а без этого правила колонка не даёт
    # ленте сжаться — она растягивает чат и уносит композер за нижний край.
    feed_block = css[css.index(".feed {") : css.index(".feed > *")]
    assert "min-height: 0" in feed_block, "у .feed нет min-height: 0"
    assert ".composer { flex: 0 0 auto" in css, "композер должен быть нерастяжимым"
    assert "overflow-y: auto" in feed_block, "лента должна прокручиваться"

    # Главное: элементам ленты запрещено сжиматься.
    assert ".feed > * { flex: 0 0 auto; }" in css, "элементы ленты всё ещё сжимаются"
    assert "scroll-behavior: smooth" not in feed_block, (
        "плавная прокрутка ленты дёргает её на каждом куске ответа"
    )
    return "элементы ленты не сжимаются, лента прокручивается, композер закреплён"


@check("клиент: разбор markdown и раскладка проверены настоящими вызовами")
def check_browser():
    """Греп по исходнику прошёл бы и если экранирование переедет **после**
    разбора — самая опасная поверхность демо была бы прикрыта пустышкой.
    Клиентский код исполняется под node: payload на входе, утверждения
    про выход."""
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

    # Все изменяемые поля конфига: их ровно три, и каждое обязано быть своим.
    assert first.spec.extra_body is not shared.extra_body
    assert first.spec.extra_body["provider"] is not shared.extra_body["provider"]
    assert first.spec.extra_body is not second.spec.extra_body
    assert first.spec.stop is not shared.stop and first.spec.stop is not second.spec.stop
    assert first.spec.response_format is not shared.response_format
    assert first.spec.response_format is not second.spec.response_format

    first.spec.extra_body["provider"]["order"] = ["only-me"]
    first.spec.stop.append("ЕЩЁ")
    first.spec.response_format["type"] = "json_schema"
    assert "order" not in shared.extra_body["provider"], shared.extra_body
    assert "order" not in second.spec.extra_body["provider"], second.spec.extra_body
    assert shared.stop == ["\n"], shared.stop
    assert second.spec.stop == ["\n"], second.spec.stop
    assert shared.response_format == {"type": "json_object"}, shared.response_format
    assert second.spec.response_format == {"type": "json_object"}, second.spec.response_format
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

    # Сам потолок настраивается из окружения — и это не стерёг никто:
    # выбрось чтение AGENT_MAX_LIVE, и набор остался бы зелёным.
    from app.registry import DEFAULT_MAX_AGENTS

    os.environ["AGENT_MAX_LIVE"] = "7"
    try:
        assert AgentRegistry().max_agents == 7, AgentRegistry().max_agents
    finally:
        os.environ.pop("AGENT_MAX_LIVE")
    assert AgentRegistry().max_agents == DEFAULT_MAX_AGENTS
    return "вытеснены три самых старых, свежие и занятый на месте, AGENT_MAX_LIVE читается"


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
    return "лишние поля в теле → 400, пустой текст тоже"


@check("«Стоп» гасит генерацию: частичный ответ записан и помечен")
def check_cancel():
    """Кнопку «Стоп» не стерёг никто: ручка cancel, флаг `cancelled` и текст
    «генерация отменена» можно было выкинуть целиком, не уронив ни одной
    проверки. Между тем это единственный способ не платить за ответ,
    который уже не нужен."""
    _stub.install(reply="а" * 200, chunks=20, delay=0.02)

    async def scenario():
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
            stopped = await client.post(f"/api/agents/{agent_id}/cancel")
            return stopped, await talking, REGISTRY.require(agent_id)

    stopped, response, agent = asyncio.run(scenario())
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["was_busy"] is True, stopped.text

    done = next(e for e in sse(response.text) if e["event"] == "done")
    assert done["cancelled"] is True, done
    assert done["error"] == "генерация отменена", done

    # Оборванный ответ всё равно часть диалога: он уже оплачен, и следующий
    # вопрос должен видеть, чем кончилось.
    roles = [t.role for t in agent.history]
    assert roles == ["user", "assistant"], roles
    answer = agent.history[-1]
    assert answer.error == "генерация отменена", answer.error
    assert 0 < len(answer.content) < 200, len(answer.content)
    return f"стрим оборван на {len(answer.content)} символах, ответ помечен отменой"


@check("без ключа сообщение не уходит: 503 вместо вызова к модели")
def check_no_key():
    """Стенд без .env — обычное состояние свежего клона. Проверка была только
    на то, что ключ не утекает; на то, что его отсутствие останавливает вызов,
    не было никакой: сними `_require_key`, и набор остался бы зелёным."""
    _stub.install(reply="ок")
    saved = main.has_key
    main.has_key = lambda: False
    try:
        with TestClient(main.app) as client:
            agent_id = new_agent(client)
            blocked = client.post(f"/api/agents/{agent_id}/messages", json={"text": "?"})
            repeated = client.post(f"/api/agents/{agent_id}/regenerate")
            listing = client.get("/api/agents").json()
            agent = REGISTRY.require(agent_id)
    finally:
        main.has_key = saved

    assert blocked.status_code == 503, blocked.text
    assert repeated.status_code == 503, repeated.text
    assert listing["has_key"] is False, listing
    assert not _stub.CALLS, f"до модели дошло {len(_stub.CALLS)} вызовов"
    assert agent.busy is False, "бронь не должна залипнуть на отказе"
    return "оба пути отдают 503, вызова нет, бронь не взята"


@check("ключ и заголовки атрибуции: окружение сильнее .env")
def check_env_reading():
    """В app/config.py не смотрела ни одна проверка: выбрось чтение .env или
    заголовки атрибуции — всё осталось бы зелёным. Ключ при этом живёт
    ровно там, и «стенд не настроен» отличается от «ключ есть» только этим
    файлом. Настоящий .env не трогаем: читаем из временного каталога."""
    import pathlib
    import tempfile

    import app.config as config

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
    return ".env читается, окружение сильнее файла, заголовки едут только заданные"


@check("CLI говорит с агентом без веб-слоя")
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
