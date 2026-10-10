"""OpenAI-compatible streaming, actual usage and bounded tool frames."""

from __future__ import annotations

import asyncio
import contextlib
from contextvars import ContextVar
from copy import deepcopy
import json
import time
import weakref
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator

import httpx


from shared_models import endpoint, key as model_key, generation_payload
from .schema import AgentSpec

_SPEED_WINDOW_SECONDS = 5.0

_TIMEOUT = httpx.Timeout(180.0, connect=20.0)

DEFAULT_MAX_CONCURRENCY = 16
"""Сколько вызовов к модели идёт одновременно, в общем процессе."""


def max_concurrency() -> int:
    """Fixed concurrency; excess calls wait on the shared semaphore."""
    return DEFAULT_MAX_CONCURRENCY


# Клиент и семафор привязаны к циклу событий, в котором их создали: у httpx
# внутри пул соединений этого цикла, а у asyncio.Semaphore — его ожидающие.
# Ключ — сам цикл, слабой ссылкой, чтобы завершённый цикл не держался в памяти.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)
_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def shared_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        limit = max_concurrency()
        client = httpx.AsyncClient(
            timeout=_TIMEOUT,
            limits=httpx.Limits(
                max_connections=max(10, limit * 2),
                max_keepalive_connections=max(10, limit),
            ),
        )
        _clients[loop] = client
    return client


def call_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max_concurrency())
        _semaphores[loop] = semaphore
    return semaphore


async def aclose() -> None:
    """Закрывает общий клиент текущего цикла. Зовётся на остановке приложения."""
    loop = asyncio.get_running_loop()
    client = _clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


@dataclass
class Metrics:
    """Живая статистика одного вызова к модели."""

    ttft_ms: float | None = None
    """Время до первого токена **ответа**. Токены рассуждения его не двигают."""

    first_token_ms: float | None = None
    """Время до первого токена вообще — рассуждения или ответа.

    На обычной модели совпадает с `ttft_ms`; на думающей `ttft_ms` наступает
    позже, когда она додумала, и включал бы всё размышление.
    """

    elapsed_ms: float = 0.0
    tokens_out: int = 0
    tokens_per_second: float = 0.0

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None

    finish_reason: str | None = None
    model: str | None = None
    provider: str | None = None
    context_length: int | None = None
    context_fill_pct: float | None = None

    error: str | None = None

    _ROUND = {
        "ttft_ms": 1,
        "first_token_ms": 1,
        "elapsed_ms": 1,
        "tokens_per_second": 2,
        "context_fill_pct": 2,
    }
    """Поля, которые округляются по дороге наружу. Остальные едут как есть."""

    def as_dict(self) -> dict:
        """Все поля, а не перечисленные руками: иначе новое поле появилось бы
        здесь, а наружу не поехало, и плитка молча показывала бы прочерк."""
        data = asdict(self)
        for name, digits in self._ROUND.items():
            if data[name] is not None:
                data[name] = round(data[name], digits)
        return data


@dataclass
class _SpeedTracker:
    """Скользящее окно по чанкам: (время, накопленные токены)."""

    points: deque = field(default_factory=lambda: deque(maxlen=512))

    def add(self, now: float, tokens: int) -> float:
        self.points.append((now, tokens))
        while len(self.points) > 2 and now - self.points[0][0] > _SPEED_WINDOW_SECONDS:
            self.points.popleft()
        if len(self.points) < 2:
            return 0.0
        t0, n0 = self.points[0]
        t1, n1 = self.points[-1]
        span = t1 - t0
        return (n1 - n0) / span if span > 0 else 0.0


SAMPLING_FIELDS = (
    "temperature",
    "max_tokens",
    "top_p",
    "top_k",
    "min_p",
    "repetition_penalty",
    "presence_penalty",
    "frequency_penalty",
)
"""Параметры сэмплирования, которые уходят в тело запроса как есть."""


def build_payload(
    session: AgentSpec,
    messages: list[dict] | None = None,
    *,
    tools: list[dict] | None = None,
) -> dict:
    """Тело запроса к общему серверу моделей.

    Промпт приходит снаружи: собирает его агент, из слепка конфига. Инструменты
    — тоже снаружи и тем же порядком: что объявить модели, решает вызывающий,
    а не слепок. Пустой список — это «не объявлять ничего», и ключа в теле
    не будет: `tools: []` у части провайдеров значит другое, чем его отсутствие.

    `tool_choice` не отправляется вовсе: звать инструмент или ответить словами
    — решение модели, и принуждать её к вызову нам незачем.
    """
    if not session.model.strip():
        raise ValueError("Выберите модель в настройках чата.")
    payload: dict = {"model": session.model, "messages": messages or [], "stream": True}
    # Незаданный параметр не отправляется вовсе — ни как null, ни как ноль:
    # отправить 0 вместо «не отправлять» — это другой запрос, а с
    # Незаданные параметры остаются на стороне модели.
    for name in SAMPLING_FIELDS:
        value = getattr(session, name, None)
        if value is not None:
            payload[name] = value

    if session.stop:
        payload["stop"] = session.stop
    if session.response_format is not None:
        payload["response_format"] = session.response_format
    if tools:
        payload["tools"] = tools

    payload.update(session.extra_body or {})
    from .store import redact
    return redact(generation_payload(payload, reasoning_enabled=session.reasoning_enabled))


_request_capture = ContextVar("request_capture", default=None)


@contextlib.contextmanager
def capture_requests():
    """Collect exact outbound JSON bodies in order, without headers or URLs."""
    bodies = []
    token = _request_capture.set(bodies)
    try:
        yield bodies
    finally:
        _request_capture.reset(token)


def record_request(payload: dict) -> None:
    bodies = _request_capture.get()
    if bodies is not None:
        bodies.append(deepcopy(payload))


def collected_calls(calls: dict[int, dict]) -> list[dict]:
    """Накопленные вызовы — списком по возрастанию `index`.

    Порядок в ответе задаёт `index`, а не порядок приезда: куски двух вызовов
    приходят вперемешку, и второй вправе начаться раньше, чем кончится первый.

    Пустой или отсутствующий `id` заменяется на `call_{index}`: в ответном
    сообщении `tool_call_id` обязателен, а провайдер вправе его не прислать —
    и тогда единственное, чем вызовы различимы, это их номер.

    Аргументы отдаются строкой, как приехали: разбор JSON — дело вызывающего,
    и он вправе оказаться битым. Транспорт на этом падать не должен.
    """
    return [
        {
            "id": call["id"] or f"call_{index}",
            "name": call["name"],
            "arguments": call["arguments"],
        }
        for index, call in sorted(calls.items())
    ]


class MissingKeyError(RuntimeError):
    pass


async def stream_completion(
    session: AgentSpec,
    *,
    prompt_override: list[dict] | None = None,
    context_length: int | None = None,
    tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """События {"type": "delta"|"reasoning"|"tool_calls"|"metrics"|"done"|"error", ...}:
    метрики обновляются по мере генерации, финальный usage приходит последним чанком.

    `tool_calls` — новый тип события, и получают его только те, кто инструменты
    объявил: без `tools` накопитель остаётся пустым и события не бывает вовсе.
    Перебирающие события обязаны переживать незнакомый тип молча.
    """
    base_url = endpoint()
    key = model_key()
    payload = build_payload(session, prompt_override, tools=tools)
    headers = {
        **({"Authorization": f"Bearer {key}"} if key else {}),
        "Content-Type": "application/json",
    }

    metrics = Metrics(model=session.model, context_length=context_length or None)
    speed = _SpeedTracker()
    started = time.monotonic()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: dict[int, dict] = {}
    announced = False
    reasoning_violation = False

    try:
        # Клиент общий на процесс, а семафор держится на всё время стрима:
        # ограничивать надо одновременные вызовы, а не их старты.
        async with call_slots():
            client = shared_client()
            record_request(payload)
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    # Provider bodies can reflect Authorization in encoded forms.
                    # Fixed diagnostics never copy upstream text into SSE/metrics.
                    messages = {
                        400: "Сервер модели отклонил параметры запроса.",
                        401: "Сервер модели отклонил авторизацию. Проверьте серверный ключ.",
                        402: "Недостаточно средств у провайдера модели.",
                        403: "Сервер модели запретил доступ. Проверьте права серверного ключа.",
                        404: "Модель или маршрут сервера не найден.",
                        429: "Превышен лимит запросов. Попробуйте позже.",
                    }
                    description = messages.get(response.status_code, "Сервер модели временно недоступен." if response.status_code >= 500 else "Сервер модели отклонил запрос.")
                    metrics.error = f"HTTP {response.status_code}: {description}"
                    metrics.elapsed_ms = (time.monotonic() - started) * 1000
                    yield {"type": "error", "message": metrics.error, "metrics": metrics.as_dict()}
                    return

                async for line in response.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue

                    now = time.monotonic()
                    metrics.elapsed_ms = (now - started) * 1000

                    if chunk.get("provider"):
                        metrics.provider = chunk["provider"]
                    if chunk.get("model"):
                        metrics.model = chunk["model"]

                    from shared_models import reports_reasoning
                    if not session.reasoning_enabled and reports_reasoning(chunk):
                        reasoning_violation = True
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}

                        # Рассуждение приходит отдельным полем дельты и в ответ
                        # не входит. В счётчик токенов не идёт — его считает
                        # usage.completion_tokens_details.reasoning_tokens,
                        # и удваивать эту цифру своей оценкой нельзя.
                        if reasoning_violation:
                            if choice.get("finish_reason"):
                                metrics.finish_reason = choice["finish_reason"]
                            continue
                        thought = delta.get("reasoning") or delta.get("reasoning_content") or ""
                        if thought:
                            if metrics.first_token_ms is None:
                                metrics.first_token_ms = (now - started) * 1000
                            reasoning_parts.append(thought)
                            yield {
                                "type": "reasoning",
                                "text": thought,
                                "metrics": metrics.as_dict(),
                            }

                        piece = delta.get("content") or ""
                        if piece:
                            if metrics.ttft_ms is None:
                                metrics.ttft_ms = (now - started) * 1000
                            if metrics.first_token_ms is None:
                                metrics.first_token_ms = metrics.ttft_ms
                            text_parts.append(piece)
                            # оценка «на глаз», пока не пришёл usage: ~4 символа на токен
                            metrics.tokens_out = max(
                                metrics.tokens_out + 1, len("".join(text_parts)) // 4
                            )
                            metrics.tokens_per_second = speed.add(now, metrics.tokens_out)
                            yield {
                                "type": "delta",
                                "text": piece,
                                "metrics": metrics.as_dict(),
                            }
                        # Вызовы копятся **рядом** с текстом, а не вместо него:
                        # у одних моделей `content` приезжает пустым, у других
                        # перед вызовом идут слова. Ветвление `if/else` одно
                        # из двух теряло бы молча.
                        #
                        # У куска гарантирован только `index`: `id`, `type`
                        # и `function.name` вправе отсутствовать в любом
                        # отдельном куске, а `arguments` приезжают обрывками.
                        # Поэтому имя и `id` берём при первом появлении и не
                        # затираем пустыми потом, а аргументы дописываем
                        # строкой. JSON здесь не разбираем вовсе.
                        for fragment in delta.get("tool_calls") or []:
                            index = fragment.get("index")
                            if not isinstance(index, int):
                                # Куска без номера быть не должно — он
                                # единственное обязательное поле фрагмента,
                                # а сортировать смесь чисел со строками нечем.
                                continue
                            slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if fragment.get("id") and not slot["id"]:
                                slot["id"] = fragment["id"]
                            function = fragment.get("function") or {}
                            if function.get("name") and not slot["name"]:
                                slot["name"] = function["name"]
                            slot["arguments"] += function.get("arguments") or ""

                        if choice.get("finish_reason"):
                            metrics.finish_reason = choice["finish_reason"]
                            # `finish_reason: "tool_calls"` приезжает дважды —
                            # на последнем содержательном куске и ещё раз на
                            # куске с usage. Событие обязано быть одно: два
                            # объявления дали бы следующему слою два оборота
                            # вместо одного, то есть двойной вызов инструмента.
                            if (
                                choice["finish_reason"] == "tool_calls"
                                and calls
                                and not announced
                                and session.reasoning_enabled
                            ):
                                announced = True
                                yield {
                                    "type": "tool_calls",
                                    "calls": collected_calls(calls),
                                    "metrics": metrics.as_dict(),
                                }

                    usage = chunk.get("usage")
                    if usage:
                        _apply_usage(metrics, usage)
                        yield {"type": "metrics", "metrics": metrics.as_dict()}

    except httpx.HTTPError as exc:
        # Exception strings may contain URLs, headers or reflected credentials.
        if isinstance(exc, httpx.TimeoutException):
            metrics.error = "Сервер модели не ответил вовремя. Попробуйте позже."
        elif isinstance(exc, httpx.NetworkError):
            metrics.error = "Не удалось подключиться к серверу модели. Проверьте сеть."
        elif isinstance(exc, httpx.ProtocolError):
            metrics.error = "Нарушен протокол связи с сервером модели. Попробуйте позже."
        else:
            metrics.error = "Ошибка связи с сервером модели. Попробуйте позже."
        metrics.elapsed_ms = (time.monotonic() - started) * 1000
        yield {"type": "error", "message": metrics.error, "metrics": metrics.as_dict()}
        return

    metrics.elapsed_ms = (time.monotonic() - started) * 1000
    # Страховка: вызовы накопились, а объявления не было — провайдер назвал
    # причиной `stop` или не назвал её совсем. Опираться на одну `finish_reason`
    # нельзя: модель без поддержки инструментов OpenRouter подменяет шаблоном,
    # и наличие вызовов говорит о них надёжнее, чем слово про причину. Событие
    # уходит здесь, до `done`: `done` — конец обмена, и после него слушателю
    # уже нечего делать с вызовом.
    if calls and not announced and not reasoning_violation:
        yield {
            "type": "tool_calls",
            "calls": collected_calls(calls),
            "metrics": metrics.as_dict(),
        }
    if reasoning_violation:
        metrics.error = "Сервер модели вернул reasoning при выключенной настройке. Проверьте поддержку reasoning_effort."
        yield {"type": "error", "message": metrics.error, "metrics": metrics.as_dict()}
        text_parts.clear()
        reasoning_parts.clear()
        calls.clear()
    yield {
        "type": "done",
        "text": "".join(text_parts),
        "reasoning": "".join(reasoning_parts),
        "metrics": metrics.as_dict(),
    }


def _apply_usage(metrics: Metrics, usage: dict) -> None:
    metrics.prompt_tokens = usage.get("prompt_tokens")
    metrics.completion_tokens = usage.get("completion_tokens")
    metrics.total_tokens = usage.get("total_tokens")
    if metrics.total_tokens is None and None not in (
        metrics.prompt_tokens,
        metrics.completion_tokens,
    ):
        # Провайдер вправе смолчать о сумме, назвав обе части. Складываем её
        # здесь, один раз и до записи в историю: сложи её потом браузер — итог
        # чата (он считается по `total_tokens`) и строка под ответом разошлись бы
        # молча, и на экране оказались бы два разных «всего».
        metrics.total_tokens = metrics.prompt_tokens + metrics.completion_tokens
    if metrics.completion_tokens:
        metrics.tokens_out = int(metrics.completion_tokens)

    details = usage.get("completion_tokens_details") or {}
    metrics.reasoning_tokens = details.get("reasoning_tokens")

    cost = usage.get("cost")
    if cost is not None:
        try:
            metrics.cost_usd = round(float(cost), 8)
        except (TypeError, ValueError):
            metrics.cost_usd = None
    # usage.cost_details.upstream_inference_cost равен 0 без BYOK — намеренно не берём.

    if metrics.context_length and metrics.total_tokens:
        metrics.context_fill_pct = 100.0 * metrics.total_tokens / metrics.context_length
