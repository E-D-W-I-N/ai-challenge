"""FastAPI-сервер чата: реестр чатов и разговор с любым из них.

Состояние держит сервер: историю хранит агент, а не браузер, и клиент шлёт
только новый текст и id. Реестр стоит поверх SQLite, список слева строится
по базе, а в память чат поднимается при открытии.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, llm
from .agent import SAMPLING_FIELDS, Agent, AgentBusyError
from .config import has_key
from .llm import MissingKeyError
from .plan import PlanError
from .registry import REGISTRY, UnknownAgentError
from .schema import (
    CONTEXT_FIELDS,
    CONTEXT_NUMBERS,
    MEMORY_KINDS,
    PROFILE_FIELDS,
    STRATEGIES,
    WORKFLOWS,
    WORKING_KINDS,
    AgentSpec,
)
from .store import StoreBusyError

STATIC_DIR = Path(__file__).resolve().parent / "static"

NEW_CHAT_SPEC = AgentSpec(label="Новый чат", model="openai/gpt-4o-mini")
"""Что получает кнопка «Новый чат»: пусто всё, кроме модели — без неё запрос
некуда отправить. Промпт задаёт пользователь. Имя выдаёт `_next_chat_label`."""

CHAT_NUMBER_KEY = "chat_number"
"""Ключ счётчика имён в `meta`. Счётчик только растёт: двух «Новых чатов 3»
быть не должно. Живёт в базе, а не в процессе, — в памяти он после
перезапуска начался бы с единицы и разошёлся бы с консолью."""


def _next_chat_label() -> str:
    return f"Новый чат {REGISTRY.store.next_counter(CHAT_NUMBER_KEY)}"


def _next_branch_label() -> str:
    """Имя ветки. Счётчик тот же, что у «Нового чата», поэтому две ветки
    от одного места получают разные имена. Имя родителя сюда не вписывается:
    его меняют из списка слева, и вписанное разошлось бы с ним."""
    return f"Ветка {REGISTRY.store.next_counter(CHAT_NUMBER_KEY)}"


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    # Общий httpx-клиент переживает все запросы, поэтому закрывать его надо
    # руками: без этого uvicorn на остановке ругается на незакрытый пул.
    await llm.aclose()


app = FastAPI(title="AI Challenge Agents", version="2.0.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(StoreBusyError)
async def _store_busy(_request: Request, exc: StoreBusyError) -> JSONResponse:
    """Занятая база — 503 с объяснением, а не голый 500: два процесса на одном
    файле штатны, и это очередь, а не поломка."""
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --- вспомогательное ----------------------------------------------------------


async def _context_lengths() -> dict[str, int]:
    """Длины контекста по моделям. Каталог недоступен — просто не покажем заполнение."""
    try:
        models = await catalog.fetch_models()
    except Exception:
        return {}
    return {m["id"]: m["context_length"] for m in models}


async def _pump(
    make_events: Callable[[], AsyncIterator[dict]],
    request: Request,
    on_close: Callable[[], None],
) -> AsyncIterator[str]:
    """Гоняет поток событий в SSE и гасит его, когда клиент ушёл: брошенная
    вкладка иначе жжёт токены. `on_close` зовётся отсюда — клиент,
    отвалившийся до первого события, отменяет задачу, не дав ей начаться,
    и `finally` внутри `make_events()` не сработает никогда."""
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
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event is done:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        on_close()


def _stream(
    make_events: Callable[[], AsyncIterator[dict]],
    request: Request,
    on_close: Callable[[], None],
):
    return StreamingResponse(
        _pump(make_events, request, on_close),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- разбор конфига агента ----------------------------------------------------

MAX_SPAWN_BATCH = 250
"""Спавн бесплатен, но список из тела ничем не ограничен, а реестр — живая
память процесса."""

# Целые параметры отделены от дробных: «top_k: 0.5» должен получить 400,
# а не уехать к провайдеру и вернуться оттуда невнятной ошибкой.
_INT_FIELDS = ("max_tokens", "top_k")
_FLOAT_FIELDS = tuple(f for f in SAMPLING_FIELDS if f not in _INT_FIELDS)

PATCHABLE = (
    "label",
    "system",
    "model",
    "stop",
    "response_format",
    *SAMPLING_FIELDS,
    *CONTEXT_FIELDS,
)
"""Что панель справа вправе менять у живого чата: всё, что в ней видно,
и ничего сверх. Имя меняют из списка слева тем же полем `label`."""


def _optional_field(payload: dict, name: str, types: tuple, hint: str, where: str = ""):
    """Либо null, либо нужного типа, иначе 400 с текстом. bool отбрасывается
    отдельно: в Python True — это int."""
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, types):
        raise HTTPException(status_code=400, detail=f"{where}{name}: {hint}")
    return value


def _choice_field(payload: dict, name: str, allowed: tuple, default: str, where: str = ""):
    """Поле из закрытого списка: чужое значение — 400 с перечислением, а не
    молчаливая подмена умолчанием. Отсутствие ключа и `null` дают умолчание:
    у стратегии нет состояния «не выбрана»."""
    value = payload.get(name)
    if value is None:
        return default
    if not isinstance(value, str) or value not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"{where}{name}: одно из {', '.join(allowed)}, а не {value!r}",
        )
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
    """Отсутствие ключа и присланный null дают одно и то же — None,
    «не отправлять параметр». Кто из двух пришёл, решает вызывающий."""
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


def _context_fields(payload: dict, where: str = "") -> dict:
    """Стратегия, рабочий процесс, окно памяти и порог сжатия. Числа
    разбираются как сэмплирование: `keep_last = null` («резать нечем»)
    и `keep_last = 0` — разные вещи. Стратегия — из списка: чужое значение
    отсекается здесь, на границе."""
    values: dict = {
        "strategy": _choice_field(payload, "strategy", STRATEGIES, "full", where),
        # Рядом со стратегией и тем же разбором, но пунктом её переключателя
        # не является: обрезка про число реплик, процесс — про план задачи.
        "workflow": _choice_field(payload, "workflow", WORKFLOWS, "off", where),
    }
    for name in CONTEXT_NUMBERS:
        values[name] = _optional_field(payload, name, (int,), "целое число или null", where)
    if values["keep_last"] is not None and values["keep_last"] < 0:
        raise HTTPException(
            status_code=400, detail=f"{where}keep_last: целое число от нуля или null"
        )
    if values["compress_every"] is not None and values["compress_every"] <= 0:
        raise HTTPException(
            status_code=400, detail=f"{where}compress_every: целое число больше нуля или null"
        )
    return values


def _fork_point(payload: dict, history_len: int) -> int:
    """Сколько первых сообщений унести. Кривое число — 400, а не 500. Ключ
    обязателен: это и есть точка ветвления. Больше длины истории тоже 400 —
    молчаливый зажим дал бы ветку не от того места, о котором просили."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='тело: объект {"at": N}')
    unknown = [key for key in payload if key != "at"]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                "тело ветвления — только at: остальное ветка берёт у родителя. "
                f"Лишние поля: {', '.join(sorted(unknown))}"
            ),
        )
    at = payload.get("at")
    # bool отдельно: в Python True это int, и `{"at": true}` уехало бы
    # ветвлением по первому сообщению.
    if isinstance(at, bool) or not isinstance(at, int):
        raise HTTPException(
            status_code=400,
            detail=f"at: сколько первых сообщений унести — целое число от 0 до {history_len}",
        )
    if at < 0 or at > history_len:
        raise HTTPException(
            status_code=400,
            detail=(
                f"at: {at} — а в истории сообщений {history_len}. "
                f"Унести можно от 0 до {history_len}"
            ),
        )
    return at


def _text_field(payload: dict, name: str, where: str = "") -> str:
    """Строка или null. Снятое поле — пустая строка, а не None."""
    return _optional_field(payload, name, (str,), "строка или null", where) or ""


def _stop_field(payload: dict, where: str = "") -> list[str] | None:
    """Список строк, пустые не в счёт: поле в панели построчное, и лишний
    перевод строки остановил бы генерацию сразу. Правило одно на создание
    и на правку."""
    stop = _optional_field(payload, "stop", (list,), "список строк или null", where)
    if stop is not None and not all(isinstance(x, str) for x in stop):
        raise HTTPException(status_code=400, detail=f"{where}stop: список строк или null")
    return [x.strip() for x in (stop or []) if x.strip()] or None


def _label_field(payload: dict) -> str:
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(status_code=400, detail="label: непустая строка")
    return label.strip()


def _kind_field(payload: dict, kinds: tuple = MEMORY_KINDS) -> str:
    """Тип записи памяти: обязателен и только из списка. Намеренно **не**
    `_choice_field`: тот подставляет умолчание, и «человек явно выбирает»
    тихо стало бы «сервер выбрал за него». Списков два, валидатор один."""
    kind = payload.get("kind")
    if not isinstance(kind, str) or kind not in kinds:
        raise HTTPException(
            status_code=400,
            detail=(
                f"kind: одно из {', '.join(kinds)}, а не {kind!r} — "
                "тип записи выбирает человек, сервер за него не выбирает"
            ),
        )
    return kind


def _record_body(payload, allowed: tuple) -> dict:
    """Объект и только известные поля, список — параметром. Лишнее поле —
    400, а не молчаливый пропуск: номер и время выдаёт сервер. Валидатор
    один на все три слоя — довод тот же, что у `_kind_field`."""
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail=f'тело: объект с полями {", ".join(allowed)}'
        )
    unknown = [key for key in payload if key not in allowed]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"тело — только {', '.join(allowed)}: номер и время выдаёт "
                f"сервер. Лишние поля: {', '.join(sorted(unknown))}"
            ),
        )
    return payload


def _content_field(payload: dict) -> str:
    """Текст записи памяти: непустая строка. Пустая уехала бы в промпт
    строкой «о собеседнике: » и заняла бы место, ничего не сказав."""
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=400, detail="content: непустая строка")
    return content.strip()


def _profile_field(payload: dict, name: str) -> str:
    """Значение поля профиля: строка, можно пустая — ею поле **снимают**.
    А не-строка — 400: `null` пришёл бы от клиента, который путает «снять»
    с «не трогать», и разница между ними здесь есть."""
    value = payload.get(name)
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=f"{name}: строка; пустая снимает поле, а не оставляет прежнее",
        )
    return value.strip()


def _parse_spec(payload: dict, where: str) -> AgentSpec:
    """Конфиг агента из JSON. Все ошибки — 400 с текстом, а не 500."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{where[:-1]}: должен быть объектом")

    return AgentSpec(
        label=str(payload.get("label") or _next_chat_label()),
        model=_model_field(payload, where),
        system=_text_field(payload, "system", where),
        stop=_stop_field(payload, where),
        response_format=_optional_field(
            payload, "response_format", (dict,), "объект или null", where
        ),
        extra_body=_optional_field(payload, "extra_body", (dict,), "объект или null", where) or {},
        **_sampling_fields(payload, where),
        **_context_fields(payload, where),
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


@app.get("/api/agents")
async def list_agents() -> dict:
    """Всё, что нужно клиенту для списка слева и статуса ключа. Ключа здесь
    нет и быть не может — наружу уходит только факт его наличия. Счётчики,
    которых список не касается, живут в /api/health."""
    return {
        "has_key": has_key(),
        "live": len(REGISTRY),
        "max_agents": REGISTRY.max_agents,
        # Список — по базе: чат, вытесненный из памяти по потолку, из него
        # исчезать не должен. Выгрузка — не удаление.
        "agents": REGISTRY.catalogue(),
    }


@app.post("/api/agents")
async def create_agents(payload: dict = Body(default=None)) -> dict:
    """Тело: {"agents": [конфиг, ...]} — пачка, {"agent": конфиг} — один,
    пустое тело — «Новый чат». Пачка и есть ответ на критерий прошлого дня.
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
    """Панель справа: имя, системный промпт, модель, сэмплирование.
    Присланный `null` снимает параметр, пропущенный ключ не трогает ничего."""
    agent = _agent(agent_id)
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(status_code=400, detail=f"тело: объект с полями {', '.join(PATCHABLE)}")
    unknown = [key for key in payload if key not in PATCHABLE]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"менять можно только {', '.join(PATCHABLE)}, а не {', '.join(sorted(unknown))}",
        )
    # Правка во время генерации разрешена намеренно: `Agent.ask` идёт
    # на слепке конфига и текущий ответ исказить не может. Действует
    # со следующего сообщения.
    sampling = _sampling_fields(payload)
    context = _context_fields(payload)
    if "model" in payload:
        agent.spec.model = _model_field(payload)
        agent.context_length = (await _context_lengths()).get(agent.spec.model)
    if "label" in payload:
        agent.spec.label = _label_field(payload)
    if "system" in payload:
        agent.spec.system = _text_field(payload, "system")
    if "stop" in payload:
        agent.spec.stop = _stop_field(payload)
    if "response_format" in payload:
        agent.spec.response_format = _optional_field(
            payload, "response_format", (dict,), "объект или null"
        )
    for name in SAMPLING_FIELDS:
        if name in payload:
            setattr(agent.spec, name, sampling[name])
    for name in CONTEXT_FIELDS:
        if name in payload:
            setattr(agent.spec, name, context[name])
    # Правка из панели — часть чата: без записи она не пережила бы рестарт,
    # и следующий запрос ушёл бы со старым конфигом.
    agent.save_config()
    return agent.as_dict()


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict:
    _agent(agent_id)
    REGISTRY.kill(agent_id)
    return {"killed": [agent_id], "live": len(REGISTRY)}


@app.post("/api/agents/{agent_id}/fork")
async def fork_agent(agent_id: str, payload: dict = Body(default=None)) -> dict:
    """Ветка от этого чата: тело `{"at": N}` — сколько первых сообщений унести.
    Ответ в том же виде, что у создания: ветка и есть обычный чат. Ключа
    к модели не нужно, а занятость родителя не мешает — история не меняется
    до конца обмена."""
    parent = _agent(agent_id)
    at = _fork_point({} if payload is None else payload, len(parent.history))
    started = time.perf_counter()
    branch = REGISTRY.fork(parent, at, label=_next_branch_label())
    return {
        "created": 1,
        "spawn_ms": round((time.perf_counter() - started) * 1000, 2),
        "live": len(REGISTRY),
        "agents": [branch.as_dict()],
    }


# --- состояние задачи: кнопки человека ----------------------------------------
# Переходы те же, что у инструментов модели (`plan.apply`): второй проверки
# в продукте нет. `PlanError` — **409**, а не 400: тело здесь пустое и кривым
# быть не может, а исправлять надо не запрос, а этап.


def _plan_move(agent: Agent, move: Callable[[], dict], busy_ok: bool = False) -> dict:
    """Один переход по кнопке: занятость — 409, не тот этап — тоже 409.
    Занятого не двигаем: план обмен читает живой, и утверждение посреди
    него дало бы промпт одного этапа с правилом другого.

    Исключение ровно одно — пауза (`busy_ok`): нажимают её именно тогда,
    когда агент работает, и спорить ей не с чем. Правило этапа и блок задачи
    пересобираются перед каждым оборотом (`Agent.restage`), так что следующий
    оборот увидит `paused`, инструменты на нём погаснут, и агент договорит
    словами."""
    if agent.busy and not busy_ok:
        raise HTTPException(
            status_code=409,
            detail=f"агент {agent.id} уже занят: дождитесь текущего ответа",
        )
    try:
        return move()
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/agents/{agent_id}/plan/approve")
async def approve_plan(agent_id: str) -> dict:
    """Кнопка «Утвердить план». Только на этапе `approval`: «утверждено
    дважды» неотличимо от «утверждено не то»."""
    agent = _agent(agent_id)
    return {"plan": _plan_move(agent, agent.approve_plan)}


@app.post("/api/agents/{agent_id}/plan/reopen")
async def reopen_plan(agent_id: str) -> dict:
    """Кнопка «Переоткрыть задачу». Только у завершённой; шаги остаются
    `done`, этап становится `validation`. Обмен не отправляется."""
    agent = _agent(agent_id)
    return {"plan": _plan_move(agent, agent.reopen_plan)}


@app.post("/api/agents/{agent_id}/plan/pause")
async def pause_plan(agent_id: str) -> dict:
    """Кнопка «Пауза»: на любом этапе, кроме завершённого, и **на занятом
    агенте тоже** — остановить просят работающего. Ставит только человек,
    а держится пауза на отказе обоих инструментов модели."""
    agent = _agent(agent_id)
    return {"plan": _plan_move(agent, agent.pause_plan, busy_ok=True)}


@app.post("/api/agents/{agent_id}/plan/resume")
async def resume_plan(agent_id: str) -> dict:
    """Кнопка «Продолжить»: снимает паузу. Этап возвращается тот же — он
    вычисляется из списка, а список не менялся."""
    agent = _agent(agent_id)
    return {"plan": _plan_move(agent, agent.resume_plan)}


@app.post("/api/agents/{agent_id}/plan/reset")
async def reset_plan(agent_id: str) -> dict:
    """Кнопка «Сбросить задачу»: пустой список и все три флажка сняты.
    Законна всегда — на ней держится и «начать другую задачу», и «вернуться
    в обычный чат»."""
    agent = _agent(agent_id)
    return {"plan": _plan_move(agent, agent.reset_plan)}


@app.post("/api/agents/{agent_id}/cancel")
async def cancel_agent(agent_id: str) -> dict:
    agent = _agent(agent_id)
    agent.cancel()
    return {"cancelled": agent_id, "was_busy": agent.busy}


# --- долговременная память ----------------------------------------------------
# Ручки глобальные, без `agent_id`: слой один на всю базу, и чат ему
# не владелец, а читатель. Путь под чатом обещал бы память чата.


@app.get("/api/memory")
async def list_memory() -> dict:
    """Вся долговременная память целиком: отбирать не по чему, и скрывать
    от пользователя часть того, что уезжает в его промпты, нельзя."""
    records = REGISTRY.store.list_memory()
    return {"total": len(records), "records": records}


@app.post("/api/memory")
async def add_memory(payload: dict = Body(...)) -> dict:
    """Новая запись, сделанная человеком; оба поля обязательны, тип без
    умолчания. Ответ — записанная строка с номером от базы: именно
    записанная, `redact()` чистит текст по дороге."""
    _record_body(payload, ("kind", "content"))
    return REGISTRY.store.add_memory(_kind_field(payload), _content_field(payload))


@app.patch("/api/memory/{seq}")
async def edit_memory(seq: int, payload: dict = Body(...)) -> dict:
    """Правка по номеру: `kind`, `content` или оба. Разбор тела общий
    с рабочей памятью (`_record_body`)."""
    _record_body(payload, ("kind", "content"))
    if not payload:
        raise HTTPException(
            status_code=400,
            detail="тело правки пустое: назовите kind, content или оба",
        )
    current = next(
        (r for r in REGISTRY.store.list_memory() if r["seq"] == seq), None
    )
    if current is None:
        raise HTTPException(
            status_code=404,
            detail=f"записи памяти {seq} нет: её уже удалили или номера такого не было",
        )
    record = REGISTRY.store.update_memory(
        seq,
        kind=_kind_field(payload) if "kind" in payload else current["kind"],
        content=_content_field(payload) if "content" in payload else current["content"],
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"записи памяти {seq} нет: её уже удалили или номера такого не было",
        )
    return record


@app.delete("/api/memory/{seq}")
async def delete_memory(seq: int) -> dict:
    """Удаляет одну запись по номеру. Нет такой — 404, а не тихое «ок»:
    вторая вкладка показывает список с прошлой минуты, и разница между
    «удалил» и «нечего было удалять» ей важна."""
    if not REGISTRY.store.delete_memory(seq):
        raise HTTPException(
            status_code=404,
            detail=f"записи памяти {seq} нет: её уже удалили или номера такого не было",
        )
    return {"deleted": seq}


# ── профиль: как отвечать именно этому человеку ───────────────────────────
# По образцу долговременной памяти: слой глобальный, `agent_id` в пути нет.
# Пишет в него только человек — распоряжений из разговора агент не выводит.


@app.get("/api/profile")
async def get_profile() -> dict:
    """Профиль целиком — только заполненные поля: снятое удаляется, а не
    хранится пустым. Вкладка показывает пустым то, чего в ответе нет."""
    return {"profile": REGISTRY.store.load_profile()}


@app.patch("/api/profile")
async def patch_profile(payload: dict = Body(...)) -> dict:
    """Любое подмножество трёх полей; разбор тела общий с памятью. Названные
    поля записываются, неназванные не трогаются, пустая строка поле снимает.
    Ответ — **записанное**: текст по дороге чистит `redact()`."""
    _record_body(payload, PROFILE_FIELDS)
    if not payload:
        raise HTTPException(
            status_code=400,
            detail=f"тело правки пустое: назовите {', '.join(PROFILE_FIELDS)} или часть",
        )
    values = {name: _profile_field(payload, name) for name in payload}
    return {"profile": REGISTRY.store.save_profile(values)}


@app.get("/api/agents/{agent_id}/memory")
async def agent_memory(agent_id: str) -> dict:
    """Все три слоя памяти этого чата разом. Краткосрочная — **счётчиком**,
    а не стенограммой: лента уже едет в `GET /api/agents/{id}`. Сводки рядом
    с ней, хотя они не память, а то, **чем заменено** не уехавшее дословно.
    Рабочая — записи с номерами, долговременная — общий список."""
    agent = _agent(agent_id)
    return {
        "short_term": {
            "messages": len(agent.history),
            "summaries": [
                {"seq": i, "upto": item["upto"], "content": item["content"]}
                for i, item in enumerate(agent.summaries)
            ],
        },
        "working": {"records": list(agent.working)},
        "long_term": {"records": REGISTRY.store.list_memory()},
    }


# --- рабочая память чата ------------------------------------------------------
# Ручки под чатом, в отличие от долговременных: область слоя — разговор.
# Набор и разбор тела те же, что у долговременной памяти: пишет в оба слоя
# один человек, и разной формой они разъехались бы на первой правке.


@app.get("/api/agents/{agent_id}/working")
async def list_working(agent_id: str) -> dict:
    """Рабочая память этого чата целиком — записи о состоянии задачи."""
    agent = _agent(agent_id)
    records = list(agent.working)
    return {"total": len(records), "records": records}


@app.post("/api/agents/{agent_id}/working")
async def add_working(agent_id: str, payload: dict = Body(...)) -> dict:
    """Новая запись, сделанная человеком; оба поля обязательны. Ответ —
    записанная строка с номером от базы: им её потом правят и удаляют."""
    agent = _agent(agent_id)
    _record_body(payload, ("kind", "content"))
    return agent.add_working_record(
        _kind_field(payload, WORKING_KINDS), _content_field(payload)
    )


@app.patch("/api/agents/{agent_id}/working/{seq}")
async def edit_working(agent_id: str, seq: int, payload: dict = Body(...)) -> dict:
    """Правка по номеру: `kind`, `content` или оба. Пустое тело — 400:
    молчаливое «ок» на запрос ни о чём неотличимо от сломанной кнопки.
    Номер не меняется: он и есть идентичность записи."""
    agent = _agent(agent_id)
    _record_body(payload, ("kind", "content"))
    if not payload:
        raise HTTPException(
            status_code=400,
            detail="тело правки пустое: назовите kind, content или оба",
        )
    record = agent.edit_working_record(
        seq,
        kind=_kind_field(payload, WORKING_KINDS) if "kind" in payload else None,
        content=_content_field(payload) if "content" in payload else None,
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"записи рабочей памяти {seq} в этом чате нет: её уже удалили",
        )
    return record


@app.delete("/api/agents/{agent_id}/working/{seq}")
async def delete_working(agent_id: str, seq: int) -> dict:
    """Удаляет одну запись. Нет такой — 404, а не тихое «ок»: вторая вкладка
    показывает список с прошлой минуты, и разница ей важна. Номер удалённой
    не достанется следующей: у `seq` стоит AUTOINCREMENT."""
    agent = _agent(agent_id)
    if not agent.drop_working_record(seq):
        raise HTTPException(
            status_code=404,
            detail=f"записи рабочей памяти {seq} в этом чате нет: её уже удалили",
        )
    return {"deleted": seq}


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
        # Ленту клиент не шлёт: проглоти мы лишнее поле, он считал бы, что
        # диалог продолжается, а тот начинался бы заново.
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
    """Бронь берётся синхронно, до первого await: иначе второй запрос успеет
    пролезть и получит 200 с ошибкой внутри потока вместо честного 409."""
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
    """Последний ответ заменяется новым, а не дублируется. Пара снимается
    с истории до вызова — модель видит тот же контекст; не отдал ни токена —
    снятое возвращается на место."""
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
    """Зовётся, чем бы поток перегенерации ни кончился: клиент мог отвалиться
    до первого события, и снятая пара пропала бы вместе с вопросом.
    `restore` сам проверяет, не занял ли место новый обмен."""

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
        if not restored:
            agent.restore(taken)


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
    """Что живо прямо сейчас: ключ, реестр, число сохранённых чатов."""
    return {
        "has_key": has_key(),
        "agents_live": len(REGISTRY),
        "agents_max": REGISTRY.max_agents,
        "agents_evicted": REGISTRY.evicted,
        "sessions_stored": REGISTRY.store.count_sessions(),
        "llm_max_concurrency": llm.max_concurrency(),
    }
