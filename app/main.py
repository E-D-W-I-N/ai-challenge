"""FastAPI-сервер чата: реестр чатов и разговор с любым из них.

Состояние держит сервер: агент — объект в реестре, историю диалога хранит
он, а не браузер, и клиент шлёт только новый текст и id. Переживает это
перезапуск: реестр стоит поверх SQLite (`app/store.py`), список слева
строится по базе, а в память чат поднимается при открытии.

Список чатов начинается пустым: заводит их пользователь.
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
from .registry import REGISTRY, UnknownAgentError
from .schema import AgentSpec
from .store import StoreBusyError

STATIC_DIR = Path(__file__).resolve().parent / "static"

NEW_CHAT_SPEC = AgentSpec(label="Новый чат", model="openai/gpt-4o-mini")
"""Что получает кнопка «Новый чат»: пусто всё, кроме модели — без неё запрос
некуда отправить. Промпт задаёт пользователь. Имя выдаёт `_next_chat_label`."""

CHAT_NUMBER_KEY = "chat_number"
"""Ключ счётчика имён в таблице `meta`. Счётчик только растёт и номера
не переиспользует: двух «Новых чатов 3» одновременно быть не должно.

Живёт в базе, а не в процессе: в памяти он после перезапуска начался бы
с единицы, а заодно разошёлся бы с консолью на том же файле."""


def _next_chat_label() -> str:
    return f"Новый чат {REGISTRY.store.next_counter(CHAT_NUMBER_KEY)}"


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
    """Гоняет поток событий в SSE и гасит его, когда клиент ушёл: брошенная
    вкладка иначе жжёт токены.

    `on_close` зовётся именно отсюда. Клиент, отвалившийся до первого события,
    отменяет задачу, не дав ей начаться: `make_events()` не запускается, и его
    собственный `finally` не сработает никогда. Единственное место, которое
    выполнится при любом исходе, — этот `finally`.
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

MAX_SPAWN_BATCH = 250
"""Сколько агентов можно создать одним запросом: спавн бесплатен, но список
из тела ничем не ограничен, а реестр — живая память процесса."""

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
)
"""Что панель справа вправе менять у живого чата.

Всё, что видно в панели, и ничего сверх. Имя меняют из списка слева
тем же полем `label`.
"""


def _optional_field(payload: dict, name: str, types: tuple, hint: str, where: str = ""):
    """Необязательное поле: либо null, либо нужного типа. Иначе 400 с текстом.

    bool отбрасывается отдельно: в Python True — это int.
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


def _text_field(payload: dict, name: str, where: str = "") -> str:
    """Строка или null. Снятое поле — пустая строка, а не None."""
    return _optional_field(payload, name, (str,), "строка или null", where) or ""


def _stop_field(payload: dict, where: str = "") -> list[str] | None:
    """Стоп-строки: список строк, пустые не в счёт.

    Поле в панели построчное, и лишний перевод строки не должен превращаться
    в стоп-строку: пустая строка остановила бы генерацию сразу. Правило одно
    на создание и на правку — разойдясь, они однажды уже разошлись, и POST
    сохранял то, что PATCH выбрасывал.
    """
    stop = _optional_field(payload, "stop", (list,), "список строк или null", where)
    if stop is not None and not all(isinstance(x, str) for x in stop):
        raise HTTPException(status_code=400, detail=f"{where}stop: список строк или null")
    return [x.strip() for x in (stop or []) if x.strip()] or None


def _label_field(payload: dict) -> str:
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        raise HTTPException(status_code=400, detail="label: непустая строка")
    return label.strip()


def _parse_spec(payload: dict, where: str = "") -> AgentSpec:
    """Конфиг агента из JSON. Все ошибки — 400 с текстом, а не 500."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{where[:-1] or 'агент'}: должен быть объектом")

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
    """Всё, что нужно клиенту для списка слева и статуса ключа. Ключа здесь
    нет и быть не может — наружу уходит только факт его наличия."""
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
    """Создать агента или пачку.

    Тело: {"agents": [конфиг, ...]} — пачка, {"agent": конфиг} — один,
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
    """Панель справа: имя, промпт, модель, сэмплирование.

    Действует со следующего сообщения — в том числе если прямо сейчас идёт
    генерация. Присланный `null` снимает параметр, пропущенный ключ
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
    # Правка во время генерации разрешена намеренно: `Agent.ask` снимает
    # слепок конфига в начале обмена и живой конфиг после этого не читает,
    # так что текущий ответ она исказить не может.

    sampling = _sampling_fields(payload)
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
    # Правка из панели — часть чата: без записи она не пережила бы рестарт,
    # и следующий запрос ушёл бы со старым конфигом.
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
        # Ленту клиент не шлёт, и молча проглотить лишнее поле нельзя:
        # клиент считал бы, что диалог продолжается, а он начинался бы заново.
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
    """Последний ответ заменяется новым, а не дублируется.

    Пара «вопрос — ответ» снимается с истории до вызова: модель видит тот же
    контекст. Не отдал ни токена — снятое возвращается на место, иначе
    неудачная попытка унесла бы и прошлый ответ, и вопрос.
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

    Клиент мог отвалиться **до первого события**: `_regenerate_events` тогда
    не запускается вовсе, и снятая пара пропала бы вместе с вопросом.
    `restore` сам проверяет, не занял ли место новый обмен.
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
