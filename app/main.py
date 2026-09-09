"""FastAPI-стенд: реестр агентов процесса и чат с любым из них.

Ростер берётся из day.py в корне ветки: ветка — это один день, и стенд в ней
ровно один. В ростере и агенты самого Дня 6, и агенты заданий дней 1–5 — их
конфиги перенесены сюда из веток тех дней, чтобы с ними можно было поговорить.

Сервер держит состояние: агент — объект в реестре процесса, историю диалога
хранит он, а не браузер. Клиент шлёт только новый текст и id агента. Реестр
живёт в памяти: перезапуск процесса стирает его — это постановка Дня 7.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, llm
from .agent import SAMPLING_FIELDS, Agent, AgentBusyError
from .config import ROOT, has_key
from .llm import MissingKeyError
from .registry import REGISTRY, UnknownAgentError
from .schema import AgentSpec

STATIC_DIR = Path(__file__).resolve().parent / "static"

# day.py лежит в корне ветки, рядом с app/. Кладём корень в sys.path сами,
# чтобы стенд поднимался и не из корня тоже.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import day as _day

    ROSTER: list[AgentSpec] = list(_day.AGENTS)
except Exception as exc:  # noqa: BLE001 — без дня стенду нечего показывать
    raise RuntimeError(
        f"day.py не загрузился ({type(exc).__name__}: {exc}). "
        "День в ветке один, прятать ошибку не от кого — почините day.py."
    ) from exc

if not ROSTER:
    raise RuntimeError("day.py: AGENTS должен быть непустым списком AgentSpec")
_wrong = next((a for a in ROSTER if not isinstance(a, AgentSpec)), None)
if _wrong is not None:
    raise RuntimeError(
        f"day.py: AGENTS содержит {type(_wrong).__name__}, а должен — только AgentSpec"
    )
_labels = [a.label for a in ROSTER]
if len(set(_labels)) != len(_labels):
    _dupes = sorted({label for label in _labels if _labels.count(label) > 1})
    raise RuntimeError(
        f"day.py: имена агентов должны быть уникальны, а повторяются: {', '.join(_dupes)}"
    )

NEW_CHAT_SPEC = AgentSpec(
    label="Новый чат",
    model="openai/gpt-4o-mini",
    system="Ты — полезный ассистент. Отвечай по-русски, по делу.",
    note="Чистый чат: конфиг настраивается в панели справа.",
)
"""Конфиг кнопки «Новый чат». Группы у него нет — это чат пользователя."""


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    ensure_roster()
    yield
    # Общий httpx-клиент переживает все запросы, поэтому закрывать его надо
    # руками: без этого uvicorn на остановке ругается на незакрытый пул.
    await llm.aclose()


app = FastAPI(title="AI Challenge Agents", version="2.0.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# --- ростер -------------------------------------------------------------------


def ensure_roster() -> list[Agent]:
    """Поднимает недостающих агентов ростера. Идемпотентна.

    Зовётся на старте процесса и после «Очистить все чаты»: агенты дней 1–5 —
    часть стенда, а не пользовательские чаты, и пропасть они не должны.
    """
    live = {a.spec.label for a in REGISTRY.list() if a.spec.group}
    return [REGISTRY.create(spec) for spec in ROSTER if spec.label not in live]


def _chat_agents() -> list[Agent]:
    """Чаты пользователя: у них нет группы. Ростер — всё остальное."""
    return [a for a in REGISTRY.list() if not a.spec.group]


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

PATCHABLE = ("label", "system", "model", "history_limit", *SAMPLING_FIELDS)
"""Что панель справа вправе менять у живого агента.

Всё сразу: панель и есть редактор конфига, а не набор заплаток поверх него.
Снаружи не меняются только `group` (по ней «Очистить все чаты» отличает
ростер от чатов) и стартовые `messages`.
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

    return AgentSpec(
        label=str(payload.get("label") or "Новый чат"),
        model=model,
        messages=_parse_messages(payload.get("messages") or [], where),
        system=text("system"),
        draft=text("draft"),
        # Группу извне задать нельзя: она отличает агентов ростера от чатов
        # пользователя, и «Очистить все чаты» ходит именно по ней.
        group="",
        note=text("note"),
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
                f"агента {agent_id} нет в реестре: он мог быть вытеснен по лимиту "
                "или стёрт перезапуском стенда — создайте нового"
            ),
        ) from exc


def _listing() -> dict:
    """Всё, что нужно клиенту для списка слева и статуса ключа.

    Ключа здесь нет и быть не может: наружу уходит только факт его наличия.
    """
    return {
        "has_key": has_key(),
        "live": len(REGISTRY),
        "max_agents": REGISTRY.max_agents,
        "evicted": REGISTRY.evicted,
        # Порядок групп задаёт day.py, а не сортировка: «День 10» не должен
        # оказаться между первым и вторым.
        "groups": list(dict.fromkeys(spec.group for spec in ROSTER if spec.group)),
        "agents": [a.as_dict() for a in REGISTRY.list()],
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
        replace(NEW_CHAT_SPEC) if item is None else _parse_spec(item, f"agents[{i}].")
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


@app.post("/api/agents/reset")
async def reset_chats() -> dict:
    """«Очистить все чаты»: сносит чаты пользователя, ростер не трогает.

    Агенты дней 1–5 — часть стенда, а не переписка: они переживают очистку
    и восстанавливаются, если чего-то не хватает.
    """
    killed: list[str] = []
    for agent in _chat_agents():
        killed.extend(REGISTRY.kill(agent.id))
    ensure_roster()
    return {"killed": killed, **_listing()}


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """Конфиг агента со стенограммой: стартовый промпт и весь диалог."""
    return _agent(agent_id).as_dict(with_transcript=True)


@app.patch("/api/agents/{agent_id}")
async def patch_agent(agent_id: str, payload: dict = Body(...)) -> dict:
    """Панель справа: имя, системный промпт, модель, память, сэмплирование.

    Изменения применяются к живому агенту и действуют со следующего сообщения.
    Присланный `null` снимает параметр — он перестаёт уходить в OpenRouter
    вовсе; пропущенный ключ не трогает ничего.
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
    if agent.busy:
        raise HTTPException(
            status_code=409,
            detail=f"агент {agent_id} занят: правка конфига посреди ответа исказила бы метрики",
        )

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
    for name in SAMPLING_FIELDS:
        if name in payload:
            setattr(agent.spec, name, sampling[name])
    return agent.as_dict()


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict:
    _agent(agent_id)
    killed = REGISTRY.kill(agent_id)
    return {"killed": killed, "live": len(REGISTRY)}


@app.post("/api/agents/{agent_id}/cancel")
async def cancel_agent(agent_id: str) -> dict:
    agent = _agent(agent_id)
    agent.cancel()
    return {"cancelled": agent_id, "was_busy": agent.busy}


# --- каталог моделей ----------------------------------------------------------


@app.get("/api/models")
async def list_models(
    requires: str = Query("", description="csv: temperature,stop,response_format"),
    exclude_free: bool = False,
    exclude_temperature_capped: bool = False,
) -> dict:
    try:
        models = await catalog.fetch_models()
    except Exception as exc:  # каталог недоступен — UI не должен падать
        raise HTTPException(status_code=502, detail=f"каталог моделей недоступен: {exc}") from exc
    needed = tuple(p.strip() for p in requires.split(",") if p.strip())
    filtered = catalog.filter_models(
        models,
        requires=needed,
        exclude_free=exclude_free,
        exclude_temperature_capped=exclude_temperature_capped,
    )
    return {"total": len(models), "count": len(filtered), "models": filtered}


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
    """
    agent = _agent(agent_id)
    _require_key()
    _reserve(agent)
    question = agent.drop_last_exchange()
    if question is None:
        agent.release()
        raise HTTPException(
            status_code=409, detail="перегенерировать нечего: последнего ответа в истории нет"
        )
    return _stream(lambda: _chat_events(agent, question), request, agent.release)


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
    except Exception as exc:  # noqa: BLE001 — падает обмен, стенд живёт
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
        "llm_max_concurrency": llm.max_concurrency(),
    }
