"""Стриминг OpenRouter + сбор метрик. Без стриминга нет ни TTFT,
ни живого счётчика скорости.

Общее правило для всех вызовов — provider.require_parameters = true.
Без него OpenRouter вправе увести запрос к провайдеру, который молча
проигнорирует temperature или stop, и день покажет неправду.

HTTP-клиент на процесс один, и одновременных вызовов не больше
`LLM_MAX_CONCURRENCY`: клиент внутри каждого вызова — это на сотне агентов
сотня пулов соединений и сотня одновременных запросов к OpenRouter.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import weakref
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator

import httpx

from .config import OPENROUTER_BASE_URL, api_key, attribution_headers
from .schema import AgentSpec

_SPEED_WINDOW_SECONDS = 5.0

_TIMEOUT = httpx.Timeout(180.0, connect=20.0)

DEFAULT_MAX_CONCURRENCY = 16
"""Сколько вызовов к модели идёт одновременно, если LLM_MAX_CONCURRENCY не задан."""


def max_concurrency() -> int:
    """LLM_MAX_CONCURRENCY. Лишние вызовы не падают, а ждут на семафоре."""
    raw = os.environ.get("LLM_MAX_CONCURRENCY", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENCY
    return max(1, value)


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


def build_payload(session: AgentSpec, messages: list[dict]) -> dict:
    """Тело запроса к OpenRouter. require_parameters — на каждом вызове.

    Промпт приходит снаружи: собирает его агент, из слепка конфига.
    """
    payload: dict = {
        "model": session.model,
        "messages": messages,
        "stream": True,
        # Просим OpenRouter вернуть usage в финальном чанке: cost и reasoning_tokens
        "usage": {"include": True},
        "provider": {"require_parameters": True},
    }
    # Незаданный параметр не отправляется вовсе — ни как null, ни как ноль:
    # отправить 0 вместо «не отправлять» — это другой запрос, а с
    # provider.require_parameters=true ещё и другой список провайдеров.
    for name in SAMPLING_FIELDS:
        value = getattr(session, name, None)
        if value is not None:
            payload[name] = value

    if session.stop:
        payload["stop"] = session.stop
    if session.response_format is not None:
        payload["response_format"] = session.response_format

    for key, value in (session.extra_body or {}).items():
        if key == "provider" and isinstance(value, dict):
            payload["provider"] = {**payload["provider"], **value}
        else:
            payload[key] = value
    return payload


class MissingKeyError(RuntimeError):
    pass


async def stream_completion(
    session: AgentSpec, *, prompt_override: list[dict], context_length: int | None
) -> AsyncIterator[dict]:
    """События {"type": "delta"|"reasoning"|"metrics"|"done"|"error", ...}: метрики
    обновляются по мере генерации, финальный usage приходит последним чанком."""
    key = api_key()
    if key is None:
        raise MissingKeyError(
            "OPENROUTER_API_KEY не найден. Скопируйте .env.example в .env и впишите ключ."
        )

    payload = build_payload(session, prompt_override)
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        **attribution_headers(),
    }

    metrics = Metrics(model=session.model, context_length=context_length)
    speed = _SpeedTracker()
    started = time.monotonic()
    text_parts: list[str] = []
    reasoning_parts: list[str] = []

    try:
        # Клиент общий на процесс, а семафор держится на всё время стрима:
        # ограничивать надо одновременные вызовы, а не их старты.
        async with call_slots():
            client = shared_client()
            async with client.stream(
                "POST",
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    metrics.error = f"HTTP {response.status_code}: {body[:600]}"
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

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}

                        # Рассуждение приходит отдельным полем дельты и в ответ
                        # не входит. В счётчик токенов не идёт — его считает
                        # usage.completion_tokens_details.reasoning_tokens,
                        # и удваивать эту цифру своей оценкой нельзя.
                        thought = delta.get("reasoning") or ""
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
                        if choice.get("finish_reason"):
                            metrics.finish_reason = choice["finish_reason"]

                    usage = chunk.get("usage")
                    if usage:
                        _apply_usage(metrics, usage)
                        yield {"type": "metrics", "metrics": metrics.as_dict()}

    except httpx.HTTPError as exc:
        metrics.error = f"{type(exc).__name__}: {exc}"
        metrics.elapsed_ms = (time.monotonic() - started) * 1000
        yield {"type": "error", "message": metrics.error, "metrics": metrics.as_dict()}
        return

    metrics.elapsed_ms = (time.monotonic() - started) * 1000
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
