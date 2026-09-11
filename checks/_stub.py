"""Заглушка стрима к модели: проверки идут без сети и без ключа.

Подменяет `stream_completion` в `app.agent`, записывает каждый вызов (модель
и весь промпт целиком) и отдаёт детерминированный ответ чанками. По записанным
вызовам и проверяется главное: что именно агент отправил в модель.
"""

from __future__ import annotations

import asyncio
from typing import Callable

CALLS: list[dict] = []
"""Все вызовы к «модели»: {"model", "label", "messages", "payload"}.

`payload` собирается настоящим `build_payload`, а не пересказывается: проверки
обязаны смотреть на то самое тело, которое ушло бы в OpenRouter.
"""

ACTIVE = {"now": 0, "closed": 0}
"""Сколько стримов открыто прямо сейчас и сколько закрыто досрочно."""


def reset() -> None:
    CALLS.clear()
    ACTIVE.update(now=0, closed=0)


def _default_reply(messages: list[dict], index: int) -> str:
    last = messages[-1].get("content", "") if messages else ""
    return f"ответ#{index} на: {last[:60]}"


def make(
    reply: Callable[[list[dict], int], str] | str | None = None,
    *,
    fail: bool = False,
    chunks: int = 4,
    delay: float = 0.0,
    reasoning: str = "",
):
    """Собирает заглушку `stream_completion`.

    reply — текст ответа или функция (messages, номер вызова) → текст.
    fail — вместо ответа отдать событие error, как это делает HTTP 4xx.
    delay — пауза между чанками: нужна проверке обрыва, чтобы успеть оборвать.
    reasoning — рассуждение, которое модель присылает отдельным полем дельты.
    """
    from app.llm import Metrics, build_payload

    async def fake_stream_completion(session, *, prompt_override=None, context_length=None):
        messages = list(prompt_override or [])
        index = len(CALLS)
        CALLS.append(
            {
                "model": session.model,
                "label": session.label,
                "messages": [dict(m) for m in messages],
                "payload": build_payload(session, prompt_override),
            }
        )

        if callable(reply):
            text = reply(messages, index)
        elif isinstance(reply, str):
            text = reply
        else:
            text = _default_reply(messages, index)

        # Метрики собираем настоящим датаклассом, а не словарём по памяти:
        # иначе новое поле появится в app/llm.py, а заглушка о нём не узнает,
        # и проверка будет смотреть в вымышленный набор ключей.
        tokens = max(1, len(text) // 4)
        metrics = Metrics(
            ttft_ms=1.0,
            # Рассуждение приходит раньше ответа — на нём и стоит первый токен.
            first_token_ms=0.5 if reasoning else 1.0,
            elapsed_ms=2.0,
            tokens_out=tokens,
            tokens_per_second=10.0,
            prompt_tokens=sum(len(m.get("content", "")) for m in messages) // 4,
            completion_tokens=tokens,
            total_tokens=100,
            cost_usd=0.000123,
            finish_reason="stop",
            model=session.model,
            provider="stub",
            context_length=context_length,
        ).as_dict()

        ACTIVE["now"] += 1
        finished = False
        try:
            if fail:
                metrics = {**metrics, "error": "HTTP 402: заглушка", "finish_reason": None}
                yield {"type": "error", "message": "HTTP 402: заглушка", "metrics": metrics}
                finished = True
                return

            if reasoning:
                yield {"type": "reasoning", "text": reasoning, "metrics": metrics}

            size = max(1, len(text) // max(1, chunks))
            for start in range(0, len(text), size):
                if delay:
                    await asyncio.sleep(delay)
                yield {"type": "delta", "text": text[start : start + size], "metrics": metrics}
            yield {"type": "metrics", "metrics": metrics}
            yield {"type": "done", "text": text, "reasoning": reasoning, "metrics": metrics}
            finished = True
        finally:
            ACTIVE["now"] -= 1
            if not finished:
                # Генератор закрыли на середине: именно так выглядит погашенный
                # вызов, когда клиент ушёл со страницы.
                ACTIVE["closed"] += 1

    return fake_stream_completion


def install(**kwargs) -> None:
    """Подменяет stream_completion в app.agent — единственном месте, откуда
    его зовут."""
    import app.agent

    app.agent.stream_completion = make(**kwargs)


def install_offline() -> None:
    """Убирает из проверок всё, что ходит в сеть, кроме самого стрима.

    Каталог моделей — живой HTTP-запрос; ключ подменяем на «есть», иначе
    ручка сообщения отдаст 503 раньше, чем дойдёт до агента.
    """
    import app.catalog as catalog
    import app.main as main

    async def no_catalog():
        return []

    catalog.fetch_models = no_catalog
    main.has_key = lambda: True
