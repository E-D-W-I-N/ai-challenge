"""FastAPI-сервер чата: реестр агентов процесса и разговор с любым из них.

Заранее заведённые чаты берутся из day.py в корне ветки. Они ничем не
особенные: обычные чаты с именем, системным промптом и настройками — их так
же переименовывают, удаляют и правят, как любой созданный руками.

Сервер держит состояние: агент — объект в реестре процесса, историю диалога
хранит он, а не браузер. Клиент шлёт только новый текст и id агента.

С Дня 7 это состояние переживает перезапуск: реестр стал реестром чатов
поверх SQLite (`app/store.py`). Ручки не изменились — изменилось то, что
список слева строится по базе, а не по памяти процесса: после рестарта он
выглядит так же, как до него, вместе со всей перепиской. В память чат
поднимается при открытии.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, llm
from .agent import SAMPLING_FIELDS, Agent, AgentBusyError
from .config import ROOT, has_key
from .llm import MissingKeyError
from .registry import REGISTRY, UnknownAgentError
from .schema import AgentSpec
from .store import StoreBusyError

LOG = logging.getLogger("app.main")
"""Логгер сервера. Пишет туда же, куда uvicorn, — в терминал, где его запустили."""

STATIC_DIR = Path(__file__).resolve().parent / "static"

# day.py лежит в корне ветки, рядом с app/. Кладём корень в sys.path сами,
# чтобы сервер поднимался и не из корня тоже.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import day as _day

    PRESET_CHATS: list[AgentSpec] = list(_day.CHATS)
except Exception as exc:  # noqa: BLE001 — без day.py серверу нечего поднимать
    raise RuntimeError(
        f"day.py не загрузился ({type(exc).__name__}: {exc}). "
        "День в ветке один, прятать ошибку не от кого — почините day.py."
    ) from exc

_wrong = next((a for a in PRESET_CHATS if not isinstance(a, AgentSpec)), None)
if _wrong is not None:
    raise RuntimeError(
        f"day.py: CHATS содержит {type(_wrong).__name__}, а должен — только AgentSpec"
    )
_labels = [a.label for a in PRESET_CHATS]
if len(set(_labels)) != len(_labels):
    _dupes = sorted({label for label in _labels if _labels.count(label) > 1})
    raise RuntimeError(
        f"day.py: имена чатов должны быть уникальны, а повторяются: {', '.join(_dupes)}"
    )
_nameless = [a.label for a in PRESET_CHATS if not a.preset]
if _nameless:
    raise RuntimeError(
        "day.py: у каждой заготовки должен быть preset — устойчивый ключ, по которому "
        f"сервер помнит, что она заведена. Нет у: {', '.join(_nameless)}"
    )
_keys = [a.preset for a in PRESET_CHATS]
if len(set(_keys)) != len(_keys):
    _dupes = sorted({key for key in _keys if _keys.count(key) > 1})
    raise RuntimeError(
        f"day.py: ключи заготовок должны быть уникальны, а повторяются: {', '.join(_dupes)}. "
        "Заготовка с чужим ключом молча не появится: сервер сочтёт её уже заведённой"
    )

NEW_CHAT_SPEC = AgentSpec(
    label="Новый чат",
    model="openai/gpt-4o-mini",
    system="Ты — полезный ассистент. Отвечай по-русски, по делу.",
)
"""Заготовка кнопки «Новый чат». Имя ей выдаёт `_next_chat_label`."""

CHAT_NUMBER_KEY = "chat_number"
"""Ключ счётчика имён по умолчанию в таблице `meta`.

Счётчик только растёт и номера не переиспользует. Иначе после удаления
третьего чата следующий стал бы вторым «Новым чатом 3» — а двух одинаковых
имён по умолчанию быть не должно.

Живёт он в базе, а не в процессе: счётчик в памяти после перезапуска начался
бы с единицы и выдал бы «Новый чат 1» поверх уже существующего. Заодно это
разводит сервер и консоль, работающие с одним файлом.
"""


def _next_chat_label() -> str:
    return f"Новый чат {REGISTRY.store.next_counter(CHAT_NUMBER_KEY)}"


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    bootstrap_chats()
    yield
    # Общий httpx-клиент переживает все запросы, поэтому закрывать его надо
    # руками: без этого uvicorn на остановке ругается на незакрытый пул.
    await llm.aclose()


app = FastAPI(title="AI Challenge Agents", version="2.0.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(StoreBusyError)
async def _store_busy(_request: Request, exc: StoreBusyError) -> JSONResponse:
    """Занятая база — это 503 с объяснением, а не голый 500.

    Два процесса на одной базе — режим штатный (сервер и консоль рядом),
    и упереться в блокировку тут не поломка, а очередь. Пользователю нужен
    текст «занято, повторите», а не строка из драйвера sqlite.
    """
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --- заранее заведённые чаты --------------------------------------------------


PRESETS_KEY = "preset_chats_seeded"
"""Ключи заготовок, которые база уже заводила. JSON-список строк.

Не «всё заведено», а **что именно заведено**. Одна отметка на всю базу
выполняла бы два требования из трёх: удалённый чат не возвращался бы,
переименованный не задваивался бы — но дописать в `day.py` новую заготовку
и увидеть её в существующей базе стало бы нельзя навсегда. А это тот самый
файл, который заказчик открывает, чтобы дописать заготовку.
"""

LEGACY_BOOTSTRAP_KEY = "preset_chats_done"
"""Отметка прошлой версии: «заготовки заведены», без уточнения каких.

Базу с ней надо перевести на ключи, а не заводить всё заново: у заказчика
в ней лежит переписка, и двадцать два дубликата он увидит первым делом.
"""


def _seeded_presets() -> set[str] | None:
    """Какие заготовки база уже заводила. None — запись не читается.

    База, записанная прошлой версией, знает только «заводила вообще». Считаем,
    что заводила она сегодняшний `day.py`: заготовок в ней ровно столько,
    сколько было на момент той записи, и отметить их все — единственный способ
    не насыпать дубликатов. Заготовка, дописанная в `day.py` после этого,
    в такой базе не появится ровно один раз, при переходе; дальше — как у всех.

    Нечитаемая запись — это не «пусто». Пустой набор значит «не заводили
    ничего», и на нём сервер завёл бы все заготовки заново: удалённые чаты
    вернулись бы, а живые задвоились. Отличить одно от другого нельзя,
    поэтому здесь честное «не знаю», а решение принимает `bootstrap_chats`.
    """
    store = REGISTRY.store
    raw = store.get_meta(PRESETS_KEY)
    if raw is not None:
        return _loads_presets(raw)
    if store.get_meta(LEGACY_BOOTSTRAP_KEY):
        return {spec.preset for spec in PRESET_CHATS}
    return set()


def _loads_presets(raw: str) -> set[str] | None:
    """Набор ключей из записи `meta`. None — запись испорчена.

    Строгий разбор: не список, не строка внутри — значит, читать нечего.
    Выкинуть непонятный элемент и продолжить было бы тем же угадыванием,
    только тише: пропавший ключ вернул бы удалённый чат.
    """
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    if not all(isinstance(item, str) for item in parsed):
        return None
    return set(parsed)


RETIRED_PRESETS = ("assistant",)
"""Ключи заготовок, которых в `day.py` больше нет и быть не должно.

Просто убрать строку из файла для этого мало. Правило «удаление строки живой
чат не трогает» — то, ради чего ключи и заводились: заказчик правит `day.py`,
и заготовка, временно закомментированная там, не должна уносить переписку.
Но «Ассистент» заказчик попросил именно удалить, а не оставить сиротой,
и отличить одно от другого может только автор дня — здесь, в этом списке.

Ключ при этом остаётся в наборе заведённых, поэтому обратно чат не заводится:
отзыв — это удаление, а не забывание. Проход идемпотентный: после первого
запуска удалять уже нечего.
"""


def retire_presets() -> list[str]:
    """Удаляет чаты отозванных заготовок — из памяти и из базы.

    Ищет их по `preset` в сохранённом конфиге, а не по имени: чат могли
    переименовать, и он всё равно тот самый.
    """
    if not RETIRED_PRESETS:
        return []
    store = REGISTRY.store
    removed = []
    for row in store.list_sessions():
        preset = (row["config"] or {}).get("preset")
        if preset in RETIRED_PRESETS and REGISTRY.kill(row["id"]):
            removed.append(row["id"])
    if removed:
        # warning, а не info: это единственное место, где сервер удаляет чужие
        # чаты сам, и в терминале uvicorn это должно быть видно. INFO туда
        # не доходит — у корневого логгера нет обработчика.
        LOG.warning(
            "заготовки %s отозваны — удалено чатов: %d",
            ", ".join(sorted(RETIRED_PRESETS)),
            len(removed),
        )
    return removed


def bootstrap_chats() -> list[Agent]:
    """Заводит заготовки из day.py, которых база ещё не заводила.

    Дальше они живут наравне со всеми: их переименовывают, удаляют и правят.
    Никакой отдельной ветки обработки у них нет — только строчка в day.py
    вместо кнопки «Новый чат».

    Узнаёт их сервер по `preset` — ключу, который не делает больше ничего
    и потому не меняется. Ни имя, ни настройки, ни порядок строк в `day.py`
    для этого не годятся: их правят. Отсюда три обещания сразу:

    * удалённый чат не возвращается — его ключ остался отмеченным;
    * переименованный не задваивается — сверка не про имя;
    * дописанная заготовка появляется и в существующей базе — её ключа
      в наборе ещё нет.

    Если набор не читается, не заводится ничего: см. `_seeded_presets`.
    Заготовки, отозванные из `day.py` насовсем, убираются здесь же:
    см. `RETIRED_PRESETS`.
    """
    seeded = _seeded_presets()
    if seeded is None:
        # Запись о заведённых заготовках не читается. Завести их «на всякий
        # случай» — это вернуть удалённые чаты и задвоить живые, то есть ровно
        # та поломка, от которой набор ключей и защищает. Ничего не заводим
        # и ничего не перезаписываем: испорченное значение остаётся на месте,
        # его можно посмотреть и починить. Всё, что уже есть в базе, работает
        # как обычно — набор управляет только заведением заготовок.
        LOG.error(
            "%s в базе не читается (%r) — заготовки из day.py не заведены. "
            "Все существующие чаты на месте и работают. Почините или удалите "
            "эту запись в таблице meta, чтобы заготовки снова заводились.",
            PRESETS_KEY,
            REGISTRY.store.get_meta(PRESETS_KEY),
        )
        return []

    retire_presets()
    fresh = [spec for spec in PRESET_CHATS if spec.preset not in seeded]
    created = [REGISTRY.create(spec) for spec in fresh]
    # Отмечаем весь сегодняшний day.py, а не только заведённое сейчас: набор
    # должен пережить и удаление строки из файла — иначе вернувшаяся строка
    # завела бы второй чат рядом с живым.
    REGISTRY.store.set_meta(
        PRESETS_KEY, json.dumps(sorted(seeded | {spec.preset for spec in PRESET_CHATS}))
    )
    return created


# --- вспомогательное ----------------------------------------------------------


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _context_lengths() -> dict[str, int]:
    """Длины контекста по моделям. Каталог недоступен — просто не покажем заполнение."""
    try:
        models = await catalog.fetch_models()
    except Exception:
        return {}
    return {m["id"]: m["context_length"] for m in models}


async def _pump(
    make_events: Callable[[], AsyncIterator[dict]],
    request: Request | None,
    on_close: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """Гоняет поток событий в SSE и гасит его, когда клиент ушёл.

    Обрыв клиента обязан гасить вызов: брошенная вкладка иначе жжёт токены,
    а агент ещё и допишет недосмотренный ответ в историю. Генератор событий
    отменяется, его `finally` доводит отмену до вызова.

    `on_close` снимает бронь агента, и делает это именно здесь. Если клиент
    отвалился до первого события, задача с генератором отменяется, не начав
    выполняться: `make_events()` не запускается, и его собственный `finally`
    не сработает никогда. Бронь залипла бы навсегда — агент вечно отвечал бы
    409 и никогда не вытеснился бы по потолку, потому что числится занятым.
    Единственное место, которое выполнится при любом исходе, — это finally
    здесь: генератор `_pump` starlette всегда либо дочитывает, либо закрывает.
    """
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump() -> None:
        try:
            events = make_events()
            async with contextlib.aclosing(events):
                async for event in events:
                    await queue.put(event)
        finally:
            queue.put_nowait(done)

    task = asyncio.create_task(pump())
    try:
        while True:
            if request is not None and await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is done:
                break
            yield _sse(event)
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if on_close is not None:
            on_close()


def _stream(
    make_events: Callable[[], AsyncIterator[dict]],
    request: Request | None,
    on_close: Callable[[], None] | None = None,
):
    return StreamingResponse(
        _pump(make_events, request, on_close),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- разбор конфига агента ----------------------------------------------------

_ROLES = ("system", "user", "assistant")

MAX_SPAWN_BATCH = 250
"""Сколько агентов можно создать одним запросом.

Спавн бесплатен, но список из тела запроса ничем не ограничен, а реестр —
живая память процесса. Двести пятьдесят с запасом покрывают демонстрацию
сотни и не дают одним запросом раздуть процесс.
"""

# Целые параметры отделены от дробных: «top_k: 0.5» должен получить 400,
# а не уехать к провайдеру и вернуться оттуда невнятной ошибкой.
_INT_FIELDS = ("max_tokens", "top_k")
_FLOAT_FIELDS = tuple(f for f in SAMPLING_FIELDS if f not in _INT_FIELDS)

PATCHABLE = (
    "label",
    "system",
    "model",
    "history_limit",
    "stop",
    "response_format",
    *SAMPLING_FIELDS,
)
"""Что панель справа вправе менять у живого чата.

Всё, что видно в панели, и ничего сверх: стартовая заготовка `messages`
снаружи не правится, а имя меняют из списка слева тем же полем `label`.
"""


def _optional_field(payload: dict, name: str, types: tuple, hint: str, where: str = ""):
    """Необязательное поле: либо null, либо нужного типа. Иначе 400 с текстом.

    bool отбрасывается отдельно: в Python True — это int, и «temperature: true»
    иначе доехало бы до провайдера.
    """
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, types):
        raise HTTPException(status_code=400, detail=f"{where}{name}: {hint}")
    return value


def _model_field(payload: dict, where: str = "") -> str:
    """id модели: непустая строка. Пусто — 400, а не падение внутри вызова."""
    value = payload.get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    detail = (
        f"{where}model: id модели OpenRouter непустой строкой"
        if where
        else "model обязателен: id модели OpenRouter строкой"
    )
    raise HTTPException(status_code=400, detail=detail)


def _sampling_fields(payload: dict, where: str = "") -> dict:
    """Разбирает параметры сэмплирования.

    Отсутствие ключа и присланный null дают одно и то же — None, «не отправлять
    параметр». Кто из двух пришёл, решает вызывающий по наличию ключа в теле.
    """
    values: dict = {}
    for name in _FLOAT_FIELDS:
        value = _optional_field(payload, name, (int, float), "число или null", where)
        values[name] = float(value) if value is not None else None
    for name in _INT_FIELDS:
        values[name] = _optional_field(payload, name, (int,), "целое число или null", where)

    if values["max_tokens"] is not None and values["max_tokens"] <= 0:
        raise HTTPException(
            status_code=400, detail=f"{where}max_tokens: целое число больше нуля или null"
        )
    if values["top_k"] is not None and values["top_k"] < 0:
        raise HTTPException(status_code=400, detail=f"{where}top_k: целое число от нуля или null")
    return values


def _parse_messages(raw, where: str) -> list[dict]:
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail=f"{where}messages: список сообщений или пусто")
    messages: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail=f"{where}messages[{index}] должен быть объектом {{role, content}}",
            )
        role = item.get("role")
        content = item.get("content")
        if role not in _ROLES:
            raise HTTPException(
                status_code=400,
                detail=f"{where}messages[{index}].role должен быть один из {', '.join(_ROLES)}",
            )
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"{where}messages[{index}].content должен быть непустой строкой",
            )
        messages.append({"role": role, "content": content})
    return messages


def _parse_spec(payload: dict, where: str = "") -> AgentSpec:
    """Конфиг агента из JSON. Все ошибки — 400 с текстом, а не 500."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{where[:-1] or 'агент'}: должен быть объектом")

    model = _model_field(payload, where)
    sampling = _sampling_fields(payload, where)

    stop = _optional_field(payload, "stop", (list,), "список строк или null", where)
    if stop is not None and not all(isinstance(x, str) for x in stop):
        raise HTTPException(status_code=400, detail=f"{where}stop: список строк или null")

    response_format = _optional_field(payload, "response_format", (dict,), "объект или null", where)
    extra_body = _optional_field(payload, "extra_body", (dict,), "объект или null", where) or {}

    history_limit = _optional_field(
        payload, "history_limit", (int,), "целое число от нуля или null", where
    )
    if history_limit is not None and history_limit < 0:
        raise HTTPException(
            status_code=400, detail=f"{where}history_limit: целое число от нуля или null"
        )

    def text(name: str) -> str:
        return _optional_field(payload, name, (str,), "строка или null", where) or ""

    messages = _parse_messages(payload.get("messages") or [], where)
    system = text("system")
    if system and any(m["role"] == "system" for m in messages):
        # У системного промпта одно место — поле `system`. Если он задан
        # и там, и сообщением, одно из двух пришлось бы выбросить молча.
        raise HTTPException(
            status_code=400,
            detail=(
                f"{where}system задан и полем, и сообщением в messages: "
                "у системного промпта одно место — выберите его"
            ),
        )

    return AgentSpec(
        label=str(payload.get("label") or _next_chat_label()),
        model=model,
        messages=messages,
        system=system,
        draft=text("draft"),
        # Ключ заготовки снаружи не задаётся: чат, назвавшийся чужим ключом,
        # отменил бы появление настоящей заготовки.
        preset="",
        stop=stop or None,
        response_format=response_format,
        extra_body=extra_body,
        history_limit=history_limit,
        **sampling,
    )


# --- жизненный цикл агентов ---------------------------------------------------


def _agent(agent_id: str) -> Agent:
    try:
        return REGISTRY.require(agent_id)
    except UnknownAgentError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"чата {agent_id} нет ни в памяти, ни в базе: его удалили — "
                "создайте новый. Вытеснение по лимиту и перезапуск сервера "
                "чат не стирают, такой ручка поднимает из базы сама"
            ),
        ) from exc


def _listing() -> dict:
    """Всё, что нужно клиенту для списка слева и статуса ключа.

    Список плоский: ни групп, ни разделения на «свои» и «заготовленные».
    Ключа здесь нет и быть не может — наружу уходит только факт его наличия.
    """
    agents = REGISTRY.catalogue()
    return {
        "has_key": has_key(),
        "live": len(REGISTRY),
        "stored": len(agents),
        "max_agents": REGISTRY.max_agents,
        "evicted": REGISTRY.evicted,
        # Список — по базе: чат, вытесненный из памяти по потолку, из него
        # исчезать не должен. Выгрузка — не удаление.
        "agents": agents,
    }


@app.get("/api/agents")
async def list_agents() -> dict:
    return _listing()


@app.post("/api/agents")
async def create_agents(payload: dict = Body(default=None)) -> dict:
    """Создать агента или пачку агентов.

    Тело: {"agents": [конфиг, ...]} — пачка, {"agent": конфиг} — один,
    пустое тело — «Новый чат» по дефолтному конфигу.

    Пачка и есть ответ на критерий дня: сто разных конфигов одним запросом,
    сто объектов в одном процессе, ни одного вызова к модели.
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="тело: объект с ключом agents или agent")

    raw = payload.get("agents")
    if raw is None:
        single = payload.get("agent")
        raw = [single] if single is not None else [None]
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="agents: непустой список конфигов")
    if len(raw) > MAX_SPAWN_BATCH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"agents: за один запрос можно создать не больше {MAX_SPAWN_BATCH} агентов, "
                f"а прислано {len(raw)}"
            ),
        )

    started = time.perf_counter()
    specs = [
        replace(NEW_CHAT_SPEC, label=_next_chat_label())
        if item is None
        else _parse_spec(item, f"agents[{i}].")
        for i, item in enumerate(raw)
    ]
    context_lengths = await _context_lengths()
    agents = REGISTRY.create_many(specs, context_lengths=context_lengths)
    return {
        "created": len(agents),
        "spawn_ms": round((time.perf_counter() - started) * 1000, 2),
        "live": len(REGISTRY),
        "agents": [a.as_dict() for a in agents],
    }


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """Конфиг агента со стенограммой: стартовый промпт и весь диалог."""
    return _agent(agent_id).as_dict(with_transcript=True)


@app.patch("/api/agents/{agent_id}")
async def patch_agent(agent_id: str, payload: dict = Body(...)) -> dict:
    """Панель справа: имя, системный промпт, модель, память, сэмплирование.

    Изменения применяются к живому агенту и действуют со следующего сообщения —
    в том числе если прямо сейчас идёт генерация. Присланный `null` снимает
    параметр: он перестаёт уходить в OpenRouter вовсе; пропущенный ключ
    не трогает ничего.
    """
    agent = _agent(agent_id)
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail=f"тело: объект с полями {', '.join(PATCHABLE)}")
    unknown = [key for key in payload if key not in PATCHABLE]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"менять можно только {', '.join(PATCHABLE)}, а не {', '.join(sorted(unknown))}",
        )
    # Правка во время генерации разрешена намеренно. Текущий ответ она
    # исказить не может: `Agent.ask` снимает слепок конфига в начале обмена
    # и собирает из него и промпт, и тело запроса, — живой конфиг после
    # этого не читается вовсе. Зато запрет стоил дорого: 409 приходил ровно
    # тогда, когда правку и хочется внести — пока читаешь длинный ответ, —
    # и терялся навсегда, потому что повторять его было нечем. Правка
    # действует со следующего сообщения, ровно как и обещает строка
    # состояния под панелью.

    sampling = _sampling_fields(payload)
    if "model" in payload:
        agent.spec.model = _model_field(payload)
        agent.context_length = (await _context_lengths()).get(agent.spec.model)
    if "label" in payload:
        label = payload.get("label")
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(status_code=400, detail="label: непустая строка")
        agent.spec.label = label.strip()
    if "system" in payload:
        system = payload.get("system")
        if system is not None and not isinstance(system, str):
            raise HTTPException(status_code=400, detail="system: строка или null")
        agent.spec.system = system or ""
    if "history_limit" in payload:
        limit = _optional_field(payload, "history_limit", (int,), "целое число от нуля или null")
        if limit is not None and limit < 0:
            raise HTTPException(
                status_code=400, detail="history_limit: целое число от нуля или null"
            )
        agent.spec.history_limit = limit
    if "stop" in payload:
        stop = _optional_field(payload, "stop", (list,), "список строк или null")
        if stop is not None and not all(isinstance(x, str) for x in stop):
            raise HTTPException(status_code=400, detail="stop: список строк или null")
        # Пустая строка стоп-строкой не является: поле в панели построчное,
        # и лишний перевод строки не должен превращаться в параметр.
        agent.spec.stop = [x.strip() for x in (stop or []) if x.strip()] or None
    if "response_format" in payload:
        agent.spec.response_format = _optional_field(
            payload, "response_format", (dict,), "объект или null"
        )
    for name in SAMPLING_FIELDS:
        if name in payload:
            setattr(agent.spec, name, sampling[name])
    # Правка из панели — часть чата: без записи она не пережила бы рестарт,
    # и чат поднялся бы на конфиге из day.py, молча отменив выбор.
    agent.save_config()
    return agent.as_dict()


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict:
    _agent(agent_id)
    REGISTRY.kill(agent_id)
    return {"killed": [agent_id], "live": len(REGISTRY)}


@app.post("/api/agents/{agent_id}/cancel")
async def cancel_agent(agent_id: str) -> dict:
    agent = _agent(agent_id)
    agent.cancel()
    return {"cancelled": agent_id, "was_busy": agent.busy}


# --- каталог моделей ----------------------------------------------------------


@app.get("/api/models")
async def list_models() -> dict:
    """Каталог моделей для дропдауна — целиком, без отбора."""
    try:
        models = await catalog.fetch_models()
    except Exception as exc:  # каталог недоступен — UI не должен падать
        raise HTTPException(status_code=502, detail=f"каталог моделей недоступен: {exc}") from exc
    return {"total": len(models), "models": models}


# --- разговор -----------------------------------------------------------------


def _require_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='тело: объект {"text": "..."}')
    unknown = [key for key in payload if key != "text"]
    if unknown:
        # Ленту клиент не шлёт. Молча проигнорировать messages нельзя: клиент
        # считал бы, что диалог продолжается, а он начинался бы заново.
        raise HTTPException(
            status_code=400,
            detail=(
                "тело сообщения — только text: историю хранит агент. "
                f"Лишние поля: {', '.join(sorted(unknown))}"
            ),
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="text: непустая строка")
    return text.strip()


def _reserve(agent: Agent) -> None:
    """Бронь берётся синхронно, до первого await: 409 приходит кодом ответа.

    Иначе между проверкой «занят?» и первым awaitом в генераторе успевает
    пролезть второй запрос и получить 200 с ошибкой внутри потока.
    """
    try:
        agent.reserve()
    except AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _require_key() -> None:
    if not has_key():
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY не найден: скопируйте .env.example в .env и впишите ключ",
        )


@app.post("/api/agents/{agent_id}/messages")
async def send_message(
    agent_id: str, request: Request, payload: dict = Body(...)
) -> StreamingResponse:
    """Сообщение агенту. Тело — только текст: ленту диалога хранит агент."""
    agent = _agent(agent_id)
    text = _require_text(payload)
    _require_key()
    _reserve(agent)
    return _stream(lambda: _chat_events(agent, text), request, agent.release)


@app.post("/api/agents/{agent_id}/regenerate")
async def regenerate(agent_id: str, request: Request) -> StreamingResponse:
    """Перегенерация: последний ответ заменяется новым, а не дублируется.

    Пара «вопрос — ответ» снимается с истории до вызова, поэтому модель видит
    ровно тот же контекст, что и в первый раз, а в ленте остаётся один ответ.
    Если вызов не отдал ни одного токена, снятое возвращается на место:
    иначе неудачная перегенерация уносила бы и прошлый ответ, и сам вопрос,
    а восстановить их было бы неоткуда — историю хранит сервер.
    """
    agent = _agent(agent_id)
    _require_key()
    _reserve(agent)
    taken = agent.take_last_exchange()
    if taken is None:
        agent.release()
        raise HTTPException(
            status_code=409, detail="перегенерировать нечего: последнего ответа в истории нет"
        )
    return _stream(
        lambda: _regenerate_events(agent, taken), request, _restore_and_release(agent, taken)
    )


def _restore_and_release(agent: Agent, taken) -> Callable[[], None]:
    """Что сделать, когда поток перегенерации закрылся, чем бы он ни кончился.

    Клиент мог отвалиться **до первого события**: тогда задача с генератором
    отменяется, не начав выполняться, `_regenerate_events` не запускается,
    и его `finally` не сработает никогда — снятая пара пропала бы вместе
    с вопросом. То же место, что и у брони: единственное, что выполнится
    при любом исходе, — это `finally` в `_pump`, и зовёт он вот это.

    `restore` сам проверяет, не занял ли место новый обмен, поэтому позвать
    его и отсюда, и из генератора безопасно.
    """

    def close() -> None:
        agent.restore(taken)
        agent.release()

    return close


async def _regenerate_events(agent: Agent, taken) -> AsyncIterator[dict]:
    """Обмен перегенерации плюс возврат снятой пары, если ответа не случилось."""
    restored = False
    try:
        stream = _chat_events(agent, taken.question)
        async with contextlib.aclosing(stream):
            async for event in stream:
                if event.get("event") == "done" and not event.get("committed"):
                    # Ответа не было вовсе: место свободно, кладём старое назад
                    # до того, как клиент перерисует ленту по серверу.
                    restored = agent.restore(taken)
                    event = {**event, "restored": restored}
                yield event
    finally:
        # Поток оборвали до `done` — например клиент ушёл со страницы.
        # `restore` сам проверит, не занял ли место новый обмен.
        if not restored:
            agent.restore(taken)
        # Случай «клиент отвалился, не увидев ни одного события», сюда
        # не доходит вовсе: генератор не запускается. Его закрывает
        # `_restore_and_release` из `finally` в `_pump`.


async def _chat_events(agent: Agent, text: str) -> AsyncIterator[dict]:
    """Обмен агента с моделью, переложенный в события SSE."""
    try:
        stream = agent.ask(text)
        async with contextlib.aclosing(stream):
            async for event in stream:
                out = {key: value for key, value in event.items() if key != "type"}
                out["event"] = event["type"]
                out["agent"] = agent.id
                yield out
    except (AgentBusyError, MissingKeyError) as exc:
        yield {"event": "error", "agent": agent.id, "message": str(exc), "metrics": None}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — падает обмен, сервер живёт
        yield {
            "event": "error",
            "agent": agent.id,
            "message": f"{type(exc).__name__}: {exc}",
            "metrics": None,
        }


# --- служебное ----------------------------------------------------------------


@app.get("/api/health")
async def health() -> dict:
    """Что живо прямо сейчас: ключ, реестр, потолок одновременных вызовов."""
    return {
        "has_key": has_key(),
        "agents_live": len(REGISTRY),
        "agents_max": REGISTRY.max_agents,
        "agents_evicted": REGISTRY.evicted,
        "sessions_stored": REGISTRY.store.count_sessions(),
        "llm_max_concurrency": llm.max_concurrency(),
    }
