"""FastAPI-стенд: реестр агентов, чат с агентом, прогон сценария субагентами.

Сценарии и ростер агентов берутся из day.py в корне ветки: ветка — это один
день, и стенд в ней ровно один. Сценарий адресуется его позицией в SCENARIOS.

С Дня 6 сервер держит состояние: агент — объект в реестре процесса, историю
диалога хранит он, а не браузер. Клиент шлёт только новый текст и id агента.

С Дня 7 это состояние переживает перезапуск: реестр стал реестром сессий
поверх SQLite (`app/store.py`). Ручки не изменились — изменилось то, что
`/api/agents/{id}` теперь находит и ту сессию, которой в памяти уже нет:
её поднимают из базы вместе с историей. Добавилась одна ручка, `/api/sessions`:
список сохранённых диалогов, из которого клиент их и переключает.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import AsyncIterator, Callable

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, commands, llm
from .agent import Agent, AgentBusyError, effective_history_limit
from .config import ROOT, has_key
from .llm import MissingKeyError
from .registry import REGISTRY, UnknownAgentError
from .schema import AgentSpec, Scenario
from .store import StoreBusyError

STATIC_DIR = Path(__file__).resolve().parent / "static"

# day.py лежит в корне ветки, рядом с app/. Кладём корень в sys.path сами,
# чтобы стенд поднимался и не из корня тоже.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import day as _day

    SCENARIOS = _day.SCENARIOS
except Exception as exc:  # noqa: BLE001 — без дня стенду нечего показывать
    raise RuntimeError(
        f"day.py не загрузился ({type(exc).__name__}: {exc}). "
        "День в ветке один, прятать ошибку не от кого — почините day.py."
    ) from exc

if not isinstance(SCENARIOS, list) or not SCENARIOS:
    raise RuntimeError("day.py: SCENARIOS должен быть непустым списком Scenario")
_wrong = next((s for s in SCENARIOS if not isinstance(s, Scenario)), None)
if _wrong is not None:
    raise RuntimeError(
        f"day.py: SCENARIOS содержит {type(_wrong).__name__}, а должен — только Scenario"
    )

# Ростер агентов дня — те, с кем говорят в чате. day.py вправе его не задавать:
# тогда стенд поднимает одного собеседника по дефолтному конфигу.
DEFAULT_CHAT_MODEL = "openai/gpt-4o-mini"
DEFAULT_CHAT_SYSTEM = (
    "Ты — агент стенда AI-челленджа. Отвечай коротко и по делу, по-русски. "
    "Ты помнишь предыдущие сообщения этого разговора."
)
AGENTS: list[AgentSpec] = list(getattr(_day, "AGENTS", None) or []) or [
    AgentSpec(
        label="Ассистент",
        model=DEFAULT_CHAT_MODEL,
        messages=[],
        system=DEFAULT_CHAT_SYSTEM,
        note="Собеседник по умолчанию: day.py не задал AGENTS.",
    )
]
_bad = next((a for a in AGENTS if not isinstance(a, AgentSpec)), None)
if _bad is not None:
    raise RuntimeError(
        f"day.py: AGENTS содержит {type(_bad).__name__}, а должен — только AgentSpec"
    )


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    yield
    # Общий httpx-клиент переживает все запросы, поэтому закрывать его надо
    # руками: без этого uvicorn на остановке ругается на незакрытый пул.
    await llm.aclose()


app = FastAPI(title="AI Challenge Bench", version="1.0.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(StoreBusyError)
async def _store_busy(_request: Request, exc: StoreBusyError) -> JSONResponse:
    """Занятая база — это 503 с объяснением, а не голый 500.

    Два процесса на одной базе — режим штатный, и упереться в блокировку тут
    не поломка, а очередь. Пользователю нужен текст «занято, повторите», а не
    строка из драйвера sqlite.
    """
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def _scenario_public(index: int, scenario: Scenario) -> dict:
    return {
        "index": index,
        "title": scenario.title,
        "description": scenario.description,
        "layout": scenario.layout,
        "sessions": [asdict(s) for s in scenario.sessions],
    }


@app.get("/api/scenarios")
async def list_scenarios() -> dict:
    """Сценарии дня в порядке из SCENARIOS — этот же порядок задаёт index."""
    return {
        "has_key": has_key(),
        "scenarios": [_scenario_public(i, s) for i, s in enumerate(SCENARIOS)],
        "roster": [asdict(a) for a in AGENTS],
        "commands": commands.help_text(SCENARIOS),
    }


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

    Обрыв клиента обязан гасить вызов и на POST-потоке тоже: брошенная вкладка
    иначе жжёт токены, а агент ещё и допишет недосмотренный ответ в историю.
    Генератор событий отменяется, его `finally` доводит отмену до субагентов.

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


# --- разбор пользовательского ввода: и конфиг агента, и overrides у /api/run ---

_ROLES = ("system", "user", "assistant")

MAX_SPAWN_BATCH = 250
"""Сколько агентов можно создать одним запросом.

Спавн бесплатен, но список из тела запроса ничем не ограничен, а реестр —
живая память процесса. Двести пятьдесят с запасом покрывают демонстрацию
сотни и не дают одним запросом раздуть процесс.
"""

MAX_REPEATS = 20
"""Потолок серии у агента, созданного через API.

`repeats` умножает число вызовов к платному API один в один: без потолка
один запрос мог бы попросить стенд сходить в модель миллион раз. Колонки
из day.py под этот потолок не попадают — там серию задаёт автор дня.
"""

# Что клиент вправе переопределить у колонки сценария. Всё остальное —
# messages, label, depends_on, extra_body — принадлежит автору дня: подмена
# label ломает сопоставление колонок в UI, подмена messages — сам сценарий.
_OVERRIDABLE = ("model", "temperature", "max_tokens")


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
    """temperature и max_tokens — общие для конфига агента и для overrides."""
    temperature = _optional_field(payload, "temperature", (int, float), "число или null", where)
    max_tokens = _optional_field(payload, "max_tokens", (int,), "целое число или null", where)
    if max_tokens is not None and max_tokens <= 0:
        raise HTTPException(
            status_code=400, detail=f"{where}max_tokens: целое число больше нуля или null"
        )
    return {
        "temperature": float(temperature) if temperature is not None else None,
        "max_tokens": max_tokens,
    }


def _parse_overrides(raw: str, sessions: list[AgentSpec]) -> dict[str, dict]:
    """Разбирает query-параметр overrides у /api/run.

    Проверяет overrides тот же код, что и конфиг агента, и проверок ровно
    столько же: кривой ввод обязан получить 400 с текстом, а не 500. До этой
    проверки список вместо объекта ронял AttributeError, лишний ключ —
    TypeError в AgentSpec(**fields), а «label» в патче молча переименовывал
    колонку.
    """
    if not raw:
        return {}
    try:
        patch = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"overrides не JSON: {exc}") from exc
    if not isinstance(patch, dict):
        raise HTTPException(
            status_code=400,
            detail="overrides: объект вида {«колонка»: {model, temperature, max_tokens}}",
        )

    labels = {session.label for session in sessions}
    clean: dict[str, dict] = {}
    for label, fields in patch.items():
        where = f"overrides[«{label}»]."
        if label not in labels:
            raise HTTPException(
                status_code=400, detail=f"overrides: колонки «{label}» нет в сценарии"
            )
        if not isinstance(fields, dict):
            raise HTTPException(
                status_code=400,
                detail=f"{where[:-1]}: объект с полями {', '.join(_OVERRIDABLE)}",
            )
        unknown = [key for key in fields if key not in _OVERRIDABLE]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{where[:-1]}: менять можно только {', '.join(_OVERRIDABLE)}, "
                    f"а не {', '.join(sorted(unknown))}"
                ),
            )

        sampling = _sampling_fields(fields, where)
        patched: dict = {}
        # Берём только те поля, что клиент прислал: явный null снимает значение
        # сценария, а пропущенный ключ его не трогает.
        if "model" in fields:
            patched["model"] = _model_field(fields, where)
        for name in ("temperature", "max_tokens"):
            if name in fields:
                patched[name] = sampling[name]
        clean[label] = patched
    return clean


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
    system = _optional_field(payload, "system", (str,), "строка или null", where) or ""
    note = _optional_field(payload, "note", (str,), "строка или null", where) or ""

    repeats = _optional_field(payload, "repeats", (int,), "целое число или null", where)
    if repeats is not None and not 1 <= repeats <= MAX_REPEATS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{where}repeats: целое число от 1 до {MAX_REPEATS} — "
                "серия множит вызовы к платному API один в один"
            ),
        )

    history_limit = _optional_field(
        payload, "history_limit", (int,), "целое число от нуля или null", where
    )
    if history_limit is not None and history_limit < 0:
        raise HTTPException(
            status_code=400, detail=f"{where}history_limit: целое число от нуля или null"
        )

    return AgentSpec(
        label=str(payload.get("label") or "агент"),
        model=model,
        messages=_parse_messages(payload.get("messages") or [], where),
        temperature=sampling["temperature"],
        max_tokens=sampling["max_tokens"],
        stop=stop or None,
        response_format=response_format,
        repeats=repeats or 1,
        note=note,
        extra_body=extra_body,
        system=system,
        history_limit=history_limit,
    )


# --- жизненный цикл агентов ---------------------------------------------------


def _agent(agent_id: str) -> Agent:
    try:
        return REGISTRY.require(agent_id)
    except UnknownAgentError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"сессии {agent_id} нет ни в памяти, ни в базе: её удалили — "
                "создайте новую. Вытеснение по лимиту и перезапуск стенда "
                "сессию не стирают, такую ручка поднимает из базы сама"
            ),
        ) from exc


@app.post("/api/agents")
async def create_agents(payload: dict = Body(...)) -> dict:
    """Создать агента или пачку агентов.

    Тело: {"agents": [конфиг, ...]} — пачка, или {"agent": конфиг} — один.
    Пачка и есть ответ на критерий дня: сто разных конфигов одним запросом,
    сто объектов в одном процессе, ни одного вызова к модели.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="тело: объект с ключом agents или agent")

    parent_id = payload.get("parent_id")
    if parent_id is not None:
        if not isinstance(parent_id, str):
            raise HTTPException(status_code=400, detail="parent_id: строка или null")
        _agent(parent_id)

    raw = payload.get("agents")
    if raw is None:
        single = payload.get("agent")
        if single is None:
            raise HTTPException(
                status_code=400, detail="нужен ключ agents (список конфигов) или agent (конфиг)"
            )
        raw = [single]
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
    specs = [_parse_spec(item, f"agents[{i}].") for i, item in enumerate(raw)]
    context_lengths = await _context_lengths()
    agents = REGISTRY.create_many(specs, parent_id=parent_id, context_lengths=context_lengths)
    return {
        "created": len(agents),
        "spawn_ms": round((time.perf_counter() - started) * 1000, 2),
        "live": len(REGISTRY),
        "agents": [a.as_dict() for a in agents],
    }


@app.get("/api/agents")
async def list_agents(parent: str = "", children_only: bool = False) -> dict:
    agents = (
        REGISTRY.list(parent_id=parent or None, only_children=True)
        if children_only or parent
        else REGISTRY.list()
    )
    return {
        "live": len(REGISTRY),
        "max_agents": REGISTRY.max_agents,
        "evicted": REGISTRY.evicted,
        "agents": [a.as_dict() for a in agents],
    }


@app.get("/api/sessions")
async def list_sessions(limit: int = 500) -> dict:
    """Сохранённые сессии — не только живые в процессе.

    Это и есть ответ дня на экране: список диалогов, который переживает
    перезапуск. Живые помечены `live`, у остальных в памяти сейчас никого,
    но открыть их можно — обращение к `/api/agents/{id}` поднимет сессию.
    """
    sessions = REGISTRY.sessions(limit=max(1, min(int(limit), 1000)))
    return {
        "live": len(REGISTRY),
        "max_agents": REGISTRY.max_agents,
        "evicted": REGISTRY.evicted,
        "stored": len(sessions),
        "sessions": [
            {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "label": row["label"],
                "model": (row["config"] or {}).get("model", ""),
                # Действующее окно, а не поле конфига: у сессии с дефолтом там
                # null, а память у неё при этом есть, и на экране это враньё.
                "history_limit": effective_history_limit(
                    (row["config"] or {}).get("history_limit")
                ),
                "history_len": row["history_len"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "live": row["live"],
            }
            for row in sessions
        ],
    }


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict:
    """Конфиг агента со стенограммой: стартовый промпт и весь диалог."""
    return _agent(agent_id).as_dict(with_transcript=True)


@app.patch("/api/agents/{agent_id}")
async def patch_agent(agent_id: str, payload: dict = Body(...)) -> dict:
    """Смена модели и семплирования на живом агенте.

    Дропдаун модели правит именно это: тело сообщения схлопнулось до текста,
    и подмешать в него model больше нельзя.
    """
    agent = _agent(agent_id)
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400, detail=f"тело: объект с полями {', '.join(_OVERRIDABLE)}"
        )
    unknown = [key for key in payload if key not in _OVERRIDABLE]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"менять можно только {', '.join(_OVERRIDABLE)}, а не {', '.join(sorted(unknown))}",
        )
    if agent.busy:
        raise HTTPException(
            status_code=409,
            detail=f"агент {agent_id} занят: смена модели посреди ответа исказила бы метрики",
        )

    sampling = _sampling_fields(payload)
    if "model" in payload:
        agent.spec.model = _model_field(payload)
        agent.context_length = (await _context_lengths()).get(agent.spec.model)
        agent.overrides["model"] = agent.spec.model
    for name in ("temperature", "max_tokens"):
        if name in payload:
            setattr(agent.spec, name, sampling[name])
            agent.overrides[name] = sampling[name]
    # Выбор пользователя — часть сессии: без записи он не пережил бы рестарт,
    # и колонка поднялась бы на модели из day.py.
    agent.save_config()
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
    for child in REGISTRY.children(agent_id):
        child.cancel()
    return {"cancelled": agent_id, "was_busy": agent.busy}


@app.post("/api/scenarios/{index}/agents")
async def spawn_scenario_agents(index: int, parent: str = "", overrides: str = "") -> dict:
    """Спавнит субагентов по колонкам сценария — до «Старта».

    Колонка становится настоящей сессией сразу при выборе сценария: с ней можно
    переписываться ещё до прогона. Прошлый набор того же родителя убивается —
    иначе реестр течёт за пару минут записи.
    """
    scenario = _scenario(index)
    patch = _parse_overrides(overrides, scenario.sessions)
    parent_agent = _agent(parent) if parent else None
    agents = await _spawn_columns(scenario, patch, parent_agent)
    return {
        "scenario": index,
        "parent": parent or None,
        "live": len(REGISTRY),
        "agents": [a.as_dict() for a in agents],
    }


def _previous_columns(parent: Agent | None) -> list[Agent]:
    """Набор субагентов прошлого прогона — тот, который сейчас заменят."""
    if parent is not None:
        return REGISTRY.children(parent.id)
    return [a for a in (REGISTRY.get(i) for i in _ORPHAN_RUN) if a is not None]


def _carried_overrides(scenario: Scenario, previous: list[Agent]) -> dict[str, dict]:
    """Что пользователь сменил руками у прошлого набора колонок.

    «Старт» спавнит свежий набор вместо предыдущего — иначе реестр течёт за
    пару минут записи. Но выбор в дропдауне живёт именно на предспавненном
    агенте: он правится через PATCH, а в теле сообщения модели больше нет.
    Без переноса «Старт» молча откатывал бы колонку на модель из day.py,
    и смена модели работала бы ровно до нажатия кнопки.
    """
    labels = {session.label for session in scenario.sessions}
    return {
        agent.spec.label: dict(agent.overrides)
        for agent in previous
        if agent.overrides and agent.spec.label in labels
    }


async def _spawn_columns(
    scenario: Scenario, patch: dict[str, dict], parent: Agent | None
) -> list[Agent]:
    """Свежий набор субагентов по колонкам сценария вместо предыдущего."""
    previous = _previous_columns(parent)
    carried = _carried_overrides(scenario, previous)

    if parent is not None:
        REGISTRY.kill_children(parent.id)
    else:
        # Прогон без родителя (прямой GET /api/run) — набор всё равно один:
        # предыдущий убиваем сами, иначе он останется висеть навсегда.
        for agent_id in list(_ORPHAN_RUN):
            REGISTRY.kill(agent_id)
        _ORPHAN_RUN.clear()

    # Явный overrides из запроса сильнее перенесённого: клиент, который
    # прислал модель в query, знает про неё больше, чем прошлый набор.
    merged = {
        label: {**carried.get(label, {}), **patch.get(label, {})}
        for label in set(carried) | set(patch)
    }

    specs = [replace(s, **merged.get(s.label, {})) for s in scenario.sessions]
    context_lengths = await _context_lengths()
    agents = REGISTRY.create_many(
        specs,
        parent_id=parent.id if parent is not None else None,
        context_lengths=context_lengths,
    )
    for agent in agents:
        # Свежий набор помнит выбор пользователя так же, как помнил прошлый:
        # иначе он потерялся бы на втором «Старте».
        agent.overrides = dict(merged.get(agent.spec.label, {}))
        if agent.overrides:
            agent.save_config()
    if parent is None:
        _ORPHAN_RUN.extend(a.id for a in agents)
    return agents


_ORPHAN_RUN: list[str] = []
"""Агенты последнего прогона без родителя: колонки и судья.

Нужен только чтобы их убить. У прогона с родителем эту роль играет
`parent_id`, а безродительскому `GET /api/run/{index}` привязаться не к чему,
и без этого списка каждый такой прогон оставлял бы в реестре и колонки,
и судью."""


# --- чат с агентом ------------------------------------------------------------


@app.post("/api/agents/{agent_id}/messages")
async def send_message(agent_id: str, request: Request, payload: dict = Body(...)) -> StreamingResponse:
    """Сообщение агенту. Тело — только текст: ленту диалога хранит агент.

    Строка, начинающаяся со слэша, — команда. Сегодня она одна: `/прогон`
    спавнит субагентов по колонкам сценария и стримит их работу тем же
    потоком событий, что и `GET /api/run/{index}`.
    """
    agent = _agent(agent_id)

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="тело: объект {\"text\": \"...\"}")
    unknown = [key for key in payload if key != "text"]
    if unknown:
        # Ленту клиент больше не шлёт. Молча проигнорировать messages нельзя:
        # старый клиент считал бы, что диалог продолжается, а он бы начинался
        # заново на каждом сообщении.
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
    text = text.strip()

    try:
        # Бронь снимается в finally генератора событий — и на нормальном
        # завершении, и на обрыве клиента.
        agent.reserve()
    except AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        command, prompt_text = commands.parse(text)
        _reject_unknown_command(command)
        index = _command_scenario(command)
    except HTTPException:
        agent.release()
        raise
    if command is not None:
        return _stream(lambda: _command_run(agent, index, command.raw), request, agent.release)

    if not has_key():
        agent.release()
        raise HTTPException(
            status_code=503,
            detail="OPENROUTER_API_KEY не найден: скопируйте .env.example в .env и впишите ключ",
        )

    return _stream(lambda: _chat_events(agent, prompt_text), request, agent.release)


def _reject_unknown_command(command) -> None:
    if command is not None and not commands.is_run(command):
        raise HTTPException(
            status_code=400,
            detail=f"неизвестная команда «/{command.name}». {commands.help_text(SCENARIOS)}",
        )


def _command_scenario(command) -> int:
    if command is None:
        return -1
    try:
        return commands.resolve_scenario(command.arg, SCENARIOS)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _chat_events(agent: Agent, text: str) -> AsyncIterator[dict]:
    """Обмен агента с моделью, переложенный в события стенда.

    Имена событий те же, что у колонки прогона: клиент рисует ответ агента
    и ответ колонки одним и тем же кодом.
    """
    try:
        stream = agent.ask(text)
        async with contextlib.aclosing(stream):
            async for event in stream:
                yield _agent_event(event, agent)
    except AgentBusyError as exc:
        yield {"event": "error", "agent": agent.id, "message": str(exc), "metrics": None}
    except (MissingKeyError, StoreBusyError) as exc:
        # У обеих текст уже человеческий — имя класса перед ним только мешает.
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


_CHAT_EVENT_NAMES = {
    "start": "start",
    "repeat_error": "error",
    "error": "error",
}
"""Переименование событий агента в события стенда.

Совпадающие имена (delta, metrics, done, repeat_*) намеренно не перечислены:
их клиент разбирает тем же кодом, что и события колонки прогона.
"""


def _agent_event(event: dict, agent: Agent) -> dict:
    """Событие агента → событие стенда. Имена событий те же, что были."""
    out = {key: value for key, value in event.items() if key != "type"}
    out["event"] = _CHAT_EVENT_NAMES.get(event["type"], event["type"])
    out["agent"] = agent.id
    return out


# --- прогон сценария ----------------------------------------------------------


@dataclass
class _Outcome:
    """Чем закончилась колонка. Нужно тем, кто ждёт её вывод по depends_on."""

    text: str = ""
    """Последний успешный ответ: его подставляет depends_on."""

    texts: list[str] = field(default_factory=list)
    """Все успешные ответы серии. При repeats=1 — список из одного элемента."""

    ok: bool = False
    finish_reason: str | None = None
    error: str | None = None
    metrics: dict | None = None
    """Метрики последнего успешного прогона."""


def _donor_problem(donor_label: str, donor: _Outcome | None) -> str | None:
    """Почему зависимую колонку запускать нельзя. None — можно.

    Без этой проверки в модель уходила пустая подстановка: донор упал, а
    зависимая колонка всё равно стартовала и отвечала на промпт, из которого
    вырезали половину. На записи это выглядит как «техника не сработала»,
    хотя не сработал вызов, — и стоит денег.
    """
    if donor is None or not donor.ok:
        detail = f": {donor.error}" if donor is not None and donor.error else ""
        return f"колонка пропущена: донор «{donor_label}» не отдал ответ{detail}"
    if not donor.text.strip():
        return f"колонка пропущена: донор «{donor_label}» вернул пустой ответ"
    return None


def _substitute(messages: list[dict], value: str) -> list[dict]:
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and "{{depends_on}}" in content:
            message = {**message, "content": content.replace("{{depends_on}}", value)}
        out.append(message)
    return out


async def _run_session(
    agent: Agent,
    queue: asyncio.Queue,
    results: dict[str, _Outcome],
    ready: dict[str, asyncio.Event],
) -> None:
    """Одна колонка прогона. Оркестрация та же, что была; вызов делает агент."""
    session = agent.spec
    label = session.label
    outcome = _Outcome()
    # Колонка занята на всё время своей дорожки, включая ожидание depends_on.
    # Ждущий субагент не держит lock и формально не занят — а значит, потолок
    # реестра вправе выгрузить его прямо из-под идущего прогона, и дальше
    # с той же сессией работали бы два объекта сразу.
    reserved = False
    try:
        agent.reserve()
        reserved = True
        if session.depends_on:
            waiter = ready.get(session.depends_on)
            if waiter is None:
                outcome.error = f"сессии «{session.depends_on}» нет в сценарии"
                await queue.put(
                    {
                        "event": "session_error",
                        "session": label,
                        "agent": agent.id,
                        "message": f"depends_on: сессии «{session.depends_on}» нет в сценарии",
                    }
                )
                return
            await queue.put(
                {
                    "event": "session_waiting",
                    "session": label,
                    "agent": agent.id,
                    "on": session.depends_on,
                }
            )
            await waiter.wait()

        donor: _Outcome | None = None
        if session.depends_on:
            donor = results.get(session.depends_on)
            problem = _donor_problem(session.depends_on, donor)
            if problem is not None:
                # В модель не идём вовсе: подставлять нечего.
                outcome.error = problem
                await queue.put(
                    {
                        "event": "session_error",
                        "session": label,
                        "agent": agent.id,
                        "message": problem,
                        "reason": "depends_on_failed",
                        "on": session.depends_on,
                    }
                )
                return
            # Подстановка правит стартовый промпт самого агента: дальше с этой
            # колонкой можно переписываться, и она будет помнить итоговый промпт,
            # а не шаблон с {{depends_on}} — в том числе после перезапуска,
            # поэтому итог сразу уезжает в базу.
            agent.seed_messages = _substitute(agent.seed_messages, donor.text)
            agent.save_config()

        total = max(1, session.repeats)
        # При repeats=1 поток остаётся ровно таким, каким был до появления
        # повторов: ни repeat-событий, ни поля repeat. Сценарии, которые
        # повторов не просили, не должны ничего заметить.
        multi = total > 1

        texts: list[str] = []
        last_metrics: dict | None = None
        failure: str | None = None

        stream = agent.ask()
        async with contextlib.aclosing(stream):
            async for chunk in stream:
                kind = chunk["type"]

                if kind == "start":
                    start_event = {
                        "event": "session_start",
                        "session": label,
                        "agent": agent.id,
                        # По resolved_messages клиент перерисовывает ленту чата:
                        # для колонки с depends_on это единственный момент, когда
                        # виден итоговый промпт после подстановки соседней колонки.
                        "resolved_messages": [
                            {"role": m.get("role", "?"), "content": m.get("content", "")}
                            for m in chunk["resolved_messages"]
                        ],
                    }
                    if multi:
                        # Клиенту нужна длина серии заранее: он подписывает блоки
                        # «прогон N из M» с первого же прогона.
                        start_event["repeats"] = total
                    if donor is not None:
                        # Обрыв донора по max_tokens: подставили урезанный промпт —
                        # UI помечает это в подписи под лентой.
                        start_event["donor"] = {
                            "label": session.depends_on,
                            "finish_reason": donor.finish_reason,
                            "truncated": donor.finish_reason == "length",
                        }
                    await queue.put(start_event)

                elif kind == "repeat_start":
                    if multi:
                        await queue.put(
                            {
                                "event": "repeat_start",
                                "session": label,
                                "agent": agent.id,
                                "repeat": chunk["repeat"],
                                "repeats": total,
                            }
                        )

                elif kind in ("delta", "metrics"):
                    event = {
                        "event": kind,
                        "session": label,
                        "agent": agent.id,
                        "metrics": chunk["metrics"],
                    }
                    if kind == "delta":
                        event["text"] = chunk["text"]
                    if multi:
                        event["repeat"] = chunk["repeat"]
                    await queue.put(event)

                elif kind == "repeat_error":
                    failure = chunk["message"]
                    event = {
                        "event": "repeat_error" if multi else "session_error",
                        "session": label,
                        "agent": agent.id,
                        "message": chunk["message"],
                        "metrics": chunk["metrics"],
                    }
                    if multi:
                        event["repeat"] = chunk["repeat"]
                    await queue.put(event)

                elif kind == "repeat_done":
                    if multi:
                        await queue.put(
                            {
                                "event": "repeat_done",
                                "session": label,
                                "agent": agent.id,
                                "repeat": chunk["repeat"],
                                "text": chunk["text"],
                                "metrics": chunk["metrics"],
                            }
                        )

                elif kind == "error":
                    outcome = _Outcome(error=chunk["message"])
                    await queue.put(
                        {
                            "event": "session_error",
                            "session": label,
                            "agent": agent.id,
                            "message": chunk["message"],
                        }
                    )
                    return

                elif kind == "done":
                    texts = list(chunk["texts"])
                    last_metrics = chunk["metrics"]
                    failure = chunk.get("error") or failure

        outcome = _Outcome(
            text=texts[-1] if texts else "",
            texts=list(texts),
            ok=bool(texts),
            finish_reason=(last_metrics or {}).get("finish_reason"),
            error=failure,
            metrics=last_metrics,
        )
        # Финальные текст и метрики приезжают этим же событием. Для серии
        # это последний удавшийся прогон, а полный список ответов и доля
        # уникальных едут рядом — сумму по серии клиент считает по repeat_done.
        done_event = {
            "event": "session_done",
            "session": label,
            "agent": agent.id,
            "text": outcome.text,
            "metrics": last_metrics,
        }
        if multi:
            done_event["repeats"] = total
            done_event["texts"] = list(texts)
            done_event["unique"] = len({t.strip() for t in texts})
        await queue.put(done_event)
    except (MissingKeyError, StoreBusyError) as exc:
        outcome = _Outcome(error=str(exc))
        await queue.put(
            {"event": "session_error", "session": label, "agent": agent.id, "message": str(exc)}
        )
    except asyncio.CancelledError:
        outcome = _Outcome(error="прогон прерван")
        raise
    except Exception as exc:  # noqa: BLE001 — колонка падает одна, прогон продолжается
        outcome = _Outcome(error=f"{type(exc).__name__}: {exc}")
        await queue.put(
            {
                "event": "session_error",
                "session": label,
                "agent": agent.id,
                "message": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        if reserved:
            agent.release()
        # Исход пишем всегда: зависимая колонка должна узнать и об успехе,
        # и о падении, а не гадать, почему записи нет.
        results[label] = outcome
        event = ready.get(label)
        if event is not None:
            event.set()


# --- модель-судья: тоже агент, но свежий на каждый прогон ---------------------

# Дефолтная модель судьи. Заметно крупнее подопытных (в колонках дней стоят
# mini / lite / small / 8b), поддерживает temperature — без этого вызов с
# provider.require_parameters=true просто не пройдёт, — не :free и не :batch.
# Один вызов на прогон, поэтому цена флагмана здесь не проблема.
JUDGE_MODEL = "openai/gpt-4o"
JUDGE_TEMPERATURE = 0.2
"""Судье нужна повторяемость вердикта, а не творчество."""

JUDGE_MAX_TOKENS = 1000
"""Потолок на всякий случай: вердикт должен помещаться в кадр рядом со сводкой."""

_JUDGE_SYSTEM = (
    "Ты — независимый судья на стенде сравнения языковых моделей. "
    "Тебе дают вопросы задания, описание демонстрации и ответы нескольких колонок — "
    "каждая со своими параметрами и метриками. "
    "Ответь по каждому вопросу коротко и по существу, опираясь только на приведённые "
    "ответы и метрики, и закончи одним абзацем — вердиктом. "
    "Пиши по-русски, без вступлений, без пересказа задания и без выдумывания того, "
    "чего в данных нет. Колонку, помеченную как не отработавшая, не оценивай: "
    "скажи, что данных по ней нет."
)

JUDGE_SPEC = AgentSpec(
    label="Судья",
    model=JUDGE_MODEL,
    messages=[],
    system=_JUDGE_SYSTEM,
    temperature=JUDGE_TEMPERATURE,
    max_tokens=JUDGE_MAX_TOKENS,
    # Свежесть судьи обеспечивает спавн на каждый прогон, а не нулевая память:
    # прошлые вердикты ему не достаются просто потому, что это другой агент.
    # Свой собственный вердикт он помнить обязан — иначе на вопрос «почему ты
    # так решил» он отвечал бы, не видя ни данных колонок, ни того, что сам
    # написал. Шесть сообщений — вердикт и пара уточняющих обменов.
    history_limit=6,
    note="Судит другая модель: один вызов после всех колонок.",
)


def _judge_column_params(session: AgentSpec) -> str:
    """Только то, чем колонка отличается от соседних, — судье это и сравнивать."""
    parts = [f"модель={session.model}"]
    if session.temperature is not None:
        parts.append(f"temperature={session.temperature}")
    if session.max_tokens is not None:
        parts.append(f"max_tokens={session.max_tokens}")
    if session.stop:
        parts.append(f"stop={session.stop}")
    if session.response_format is not None:
        parts.append(f"response_format={session.response_format}")
    if session.history_limit is not None:
        parts.append(f"history_limit={session.history_limit}")
    return ", ".join(parts)


def _judge_column_answers(outcome: _Outcome) -> str:
    """Судье уходят все ответы серии: без них не ответить про разнообразие."""
    texts = [t.strip() for t in outcome.texts if t.strip()]
    if len(texts) <= 1:
        return "Ответ:\n" + (texts[0] if texts else "")
    unique = len(set(texts))
    head = (
        f"Прогонов: {len(texts)}, уникальных ответов: {unique} из {len(texts)} "
        "(совпадение считается по точному тексту).\n"
        "Ответы по прогонам:"
    )
    body = "\n".join(f"--- прогон {i} ---\n{t}" for i, t in enumerate(texts, 1))
    return f"{head}\n{body}"


def _judge_column_metrics(outcome: _Outcome) -> str:
    metrics = outcome.metrics or {}
    elapsed = metrics.get("elapsed_ms")
    tokens = metrics.get("completion_tokens") or metrics.get("tokens_out")
    cost = metrics.get("cost_usd")
    parts = []
    if elapsed is not None:
        parts.append(f"время {elapsed / 1000:.2f} с")
    if tokens is not None:
        parts.append(f"токенов в ответе {tokens}")
    if cost is not None:
        parts.append(f"стоимость ${cost:.6f}")
    if outcome.finish_reason:
        parts.append(f"finish_reason={outcome.finish_reason}")
    line = ", ".join(parts) or "метрик нет"
    return f"{line} (последний прогон серии)" if len(outcome.texts) > 1 else line


def _judge_question(
    scenario: Scenario, sessions: list[AgentSpec], results: dict[str, _Outcome]
) -> str:
    """Вопрос судье. Системная инструкция у него в конфиге, здесь только данные."""
    questions = "\n".join(f"{i}. {q}" for i, q in enumerate(scenario.judge_questions, 1))
    # Судье уходят description сценария, вопросы дня и результаты колонок —
    # но не промпты колонок и не разбор из README.md. Разбор содержит
    # эталонный ответ и предсказание, какая колонка ошибётся: отдать его
    # судье — значит показать ему ответ до того, как он посмотрит на данные.
    blocks = [
        f"Сценарий: {scenario.title}",
        f"Что демонстрируем: {scenario.description}",
        "",
        "Вопросы задания:",
        questions,
        "",
    ]
    for session in sessions:
        outcome = results.get(session.label) or _Outcome()
        head = f"### Колонка «{session.label}»"
        if not outcome.ok or not outcome.text.strip():
            reason = outcome.error or "ответ пустой"
            blocks.append(f"{head}\nНЕ ОТРАБОТАЛА: {reason}. Вердикт по ней не выноси.\n")
            continue
        blocks.append(
            f"{head}\n"
            + (f"Чем отличается: {session.note}\n" if session.note else "")
            + f"Параметры: {_judge_column_params(session)}\n"
            + f"Метрики: {_judge_column_metrics(outcome)}\n"
            + _judge_column_answers(outcome)
            + "\n"
        )
    return "\n".join(blocks).strip()


async def _run_judge(
    scenario: Scenario,
    sessions: list[AgentSpec],
    results: dict[str, _Outcome],
    context_lengths: dict[str, int],
    parent: Agent | None,
) -> AsyncIterator[dict]:
    """Один вызов агента-судьи после всех колонок. Ошибка судьи прогон не ломает.

    Судья — обычный инстанс в реестре, и спавнится он свежим на каждый прогон:
    иначе он потащил бы в контекст прошлые вердикты. Побочная выгода — его
    видно в списке агентов, и у него можно спросить «почему ты так решил».
    """
    answered = [
        s
        for s in sessions
        if (results.get(s.label) or _Outcome()).ok and (results.get(s.label) or _Outcome()).text.strip()
    ]
    if not answered:
        yield {
            "event": "judge_skipped",
            "message": "ни одна колонка не отдала ответ — судить нечего, вызова не было",
        }
        return

    model = scenario.judge_model or JUDGE_MODEL
    # Смысл механизма — что судит другая модель. Совпадение не запрещаем,
    # но показываем: зритель должен видеть, что судья судит сам себя.
    conflicts = sorted({s.label for s in sessions if s.model == model})

    judge = REGISTRY.create(
        replace(JUDGE_SPEC, model=model),
        parent_id=parent.id if parent is not None else None,
        context_length=context_lengths.get(model),
    )
    if parent is None:
        # Привязаться не к чему: без этого каждый безродительский прогон
        # оставлял бы в реестре по судье навсегда.
        _ORPHAN_RUN.append(judge.id)
    yield {
        "event": "judge_start",
        "model": model,
        "agent": judge.id,
        "questions": list(scenario.judge_questions),
        "conflicts": conflicts,
    }

    failed = False
    try:
        stream = judge.ask(_judge_question(scenario, sessions, results))
        async with contextlib.aclosing(stream):
            async for chunk in stream:
                kind = chunk["type"]
                if kind == "delta":
                    yield {
                        "event": "judge_delta",
                        "agent": judge.id,
                        "text": chunk["text"],
                        "metrics": chunk["metrics"],
                    }
                elif kind == "metrics":
                    yield {"event": "judge_metrics", "agent": judge.id, "metrics": chunk["metrics"]}
                elif kind in ("repeat_error", "error"):
                    failed = True
                    yield {
                        "event": "judge_error",
                        "agent": judge.id,
                        "message": chunk["message"],
                        "metrics": chunk.get("metrics"),
                    }
                elif kind == "done":
                    if failed and not chunk["text"]:
                        # Вердикта нет: пустой judge_done затёр бы уже показанную
                        # ошибку строкой «готово».
                        continue
                    yield {
                        "event": "judge_done",
                        "agent": judge.id,
                        "text": chunk["text"],
                        "metrics": chunk["metrics"],
                    }
    except MissingKeyError as exc:
        yield {"event": "judge_error", "agent": judge.id, "message": str(exc), "metrics": None}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — падает только вердикт, прогон уже состоялся
        yield {
            "event": "judge_error",
            "agent": judge.id,
            "message": f"{type(exc).__name__}: {exc}",
            "metrics": None,
        }


async def _cancel_sessions(tasks: list[asyncio.Task]) -> None:
    """Гасит незавершённые сессии прогона.

    Вызывается, когда поток событий закрылся: пользователь закрыл вкладку,
    перезагрузил страницу или перевыбрал сценарий. Без этого задачи продолжают
    качать ответ из OpenRouter до конца — стенд платит за токены, которых никто
    не увидит, а на днях с provider.allow_fallbacks=false брошенный вызов ещё и
    занимает провайдера, к которому пойдёт следующий дубль записи.
    """
    unfinished = [task for task in tasks if not task.done()]
    if not unfinished:
        return
    for task in unfinished:
        task.cancel()
    # cancel() уже разослан: вызовы закроются, даже если нас самих отменяют
    # и дождаться завершения не дадут.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*unfinished, return_exceptions=True)


def _scenario(index: int) -> Scenario:
    if not 0 <= index < len(SCENARIOS):
        raise HTTPException(
            status_code=404,
            detail=f"сценария {index} нет: в дне их {len(SCENARIOS)}",
        )
    return SCENARIOS[index]


async def _run_events(
    index: int, patch: dict[str, dict], parent: Agent | None
) -> AsyncIterator[dict]:
    """Поток событий прогона: run_start → колонки → вердикт → run_done.

    Один и тот же генератор обслуживает и `GET /api/run/{index}`, и `/прогон`
    в чате: контракт событий у них общий, различается только кто родитель.
    """
    scenario = _scenario(index)
    agents = await _spawn_columns(scenario, patch, parent)
    sessions = [a.spec for a in agents]
    context_lengths = await _context_lengths()

    queue: asyncio.Queue = asyncio.Queue()
    results: dict[str, _Outcome] = {}
    ready = {a.spec.label: asyncio.Event() for a in agents}
    started = time.monotonic()

    tasks = [asyncio.create_task(_run_session(a, queue, results, ready)) for a in agents]
    # Всё, что ниже, — под finally: закрытие потока обязано погасить вызовы.
    try:
        yield {
            "event": "run_start",
            "scenario": index,
            "title": scenario.title,
            "layout": scenario.layout,
            "parent": parent.id if parent is not None else None,
            "sessions": [asdict(s) for s in sessions],
            "agents": [{"session": a.spec.label, "agent": a.id} for a in agents],
        }

        while True:
            if queue.empty() and all(task.done() for task in tasks):
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield event

        # wall-clock снимается до судьи: сводка меряет прогон колонок,
        # а не время, которое сверху потратил вердикт.
        wall_clock_ms = round((time.monotonic() - started) * 1000, 1)

        # Судья — один вызов после всех колонок. На прерванном прогоне сюда
        # не доходим: генератор отменяют, и деньги на вердикт по неполным
        # данным не тратятся.
        if scenario.judge_questions:
            async for event in _run_judge(scenario, sessions, results, context_lengths, parent):
                yield event

        yield {"event": "run_done", "wall_clock_ms": wall_clock_ms}
    finally:
        await _cancel_sessions(tasks)


@app.get("/api/run/{index}")
async def run_scenario(
    request: Request, index: int, overrides: str = "", parent: str = ""
) -> StreamingResponse:
    scenario = _scenario(index)
    # overrides: {"<label колонки>": {"model": "...", "temperature": 0.7}}
    patch = _parse_overrides(overrides, scenario.sessions)
    parent_agent = _agent(parent) if parent else None
    return _stream(lambda: _run_events(index, patch, parent_agent), request)


# --- `/прогон` в чате ---------------------------------------------------------


def _run_summary(scenario: Scenario, results: dict[str, _Outcome]) -> str:
    """Краткая сводка прогона — её родитель дописывает себе в историю.

    Без неё следующий вопрос в чате не видел бы, что прогон вообще был:
    субагенты помнят свои ответы, а родитель — нет.
    """
    lines = [f"Прогон сценария «{scenario.title}». Итоги по колонкам:"]
    for label, outcome in results.items():
        if not outcome.ok:
            lines.append(f"— «{label}»: не отработала ({outcome.error or 'ответ пустой'}).")
            continue
        head = outcome.text.strip().replace("\n", " ")
        if len(head) > 300:
            head = head[:300] + "…"
        metrics = outcome.metrics or {}
        cost = metrics.get("cost_usd")
        tail = f" (${cost:.6f})" if cost is not None else ""
        lines.append(f"— «{label}»{tail}: {head}")
    return "\n".join(lines)


async def _command_run(parent: Agent, index: int, raw: str) -> AsyncIterator[dict]:
    """`/прогон` из чата: субагенты по колонкам плюс сводка в историю родителя."""
    scenario = _scenario(index)
    try:
        async with parent.hold():
            yield {
                "event": "command_start",
                "agent": parent.id,
                "command": "прогон",
                "scenario": index,
                "title": scenario.title,
            }

            results: dict[str, _Outcome] = {}
            texts: dict[str, str] = {}
            stream = _run_events(index, {}, parent)
            async with contextlib.aclosing(stream):
                async for event in stream:
                    name = event.get("event")
                    if name == "session_done":
                        texts[event["session"]] = event.get("text") or ""
                    elif name == "session_error":
                        results.setdefault(
                            event["session"], _Outcome(error=event.get("message") or "")
                        )
                    yield event

            for label, text in texts.items():
                results[label] = _Outcome(text=text, texts=[text], ok=bool(text.strip()))

            summary = _run_summary(scenario, results)
            parent.remember_exchange(raw, summary)
            yield {"event": "command_done", "agent": parent.id, "summary": summary}
    except AgentBusyError as exc:
        yield {"event": "error", "agent": parent.id, "message": str(exc), "metrics": None}


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
