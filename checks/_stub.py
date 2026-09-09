"""Заглушка стрима к модели: проверки идут без сети и без ключа.

Живых вызовов к LLM в проверках нет — ни одного. Заглушка подменяет
`stream_completion` там, куда его импортировали, записывает каждый вызов
(модель и весь промпт целиком) и отдаёт детерминированный ответ чанками.

По записанным вызовам и проверяется главное: что именно агент отправил
в модель — окно памяти, подстановку depends_on, отсутствие ленты от клиента.
"""

from __future__ import annotations

import asyncio
from typing import Callable

CALLS: list[dict] = []
"""Все вызовы к «модели» за проверку: {"model", "label", "messages"}."""

ACTIVE = {"now": 0, "peak": 0, "closed": 0}
"""Сколько стримов открыто прямо сейчас, максимум и сколько закрыто досрочно."""


def reset() -> None:
    CALLS.clear()
    ACTIVE.update(now=0, peak=0, closed=0)


def _default_reply(messages: list[dict], index: int) -> str:
    last = messages[-1].get("content", "") if messages else ""
    return f"ответ#{index} на: {last[:60]}"


def make(
    reply: Callable[[list[dict], int], str] | str | None = None,
    *,
    fail: bool = False,
    chunks: int = 4,
    delay: float = 0.0,
):
    """Собирает заглушку `stream_completion`.

    reply — текст ответа или функция (messages, номер вызова) → текст.
    fail — вместо ответа отдать событие error, как это делает HTTP 4xx.
    delay — пауза между чанками: нужна проверке обрыва, чтобы успеть оборвать.
    """

    async def fake_stream_completion(session, *, prompt_override=None, context_length=None):
        messages = list(prompt_override if prompt_override is not None else session.messages)
        index = len(CALLS)
        CALLS.append(
            {
                "model": session.model,
                "label": session.label,
                "messages": [dict(m) for m in messages],
            }
        )

        if callable(reply):
            text = reply(messages, index)
        elif isinstance(reply, str):
            text = reply
        else:
            text = _default_reply(messages, index)

        metrics = {
            "ttft_ms": 1.0,
            "elapsed_ms": 2.0,
            "tokens_out": max(1, len(text) // 4),
            "tokens_per_second": 10.0,
            "prompt_tokens": sum(len(m.get("content", "")) for m in messages) // 4,
            "completion_tokens": max(1, len(text) // 4),
            "total_tokens": 100,
            "reasoning_tokens": None,
            "cost_usd": 0.000123,
            "finish_reason": "stop",
            "model": session.model,
            "provider": "stub",
            "context_length": context_length,
            "context_fill_pct": None,
            "error": None,
        }

        ACTIVE["now"] += 1
        ACTIVE["peak"] = max(ACTIVE["peak"], ACTIVE["now"])
        finished = False
        try:
            if fail:
                metrics = {**metrics, "error": "HTTP 402: заглушка", "finish_reason": None}
                yield {"type": "error", "message": "HTTP 402: заглушка", "metrics": metrics}
                finished = True
                return

            size = max(1, len(text) // max(1, chunks))
            for start in range(0, len(text), size):
                if delay:
                    await asyncio.sleep(delay)
                yield {"type": "delta", "text": text[start : start + size], "metrics": metrics}
            yield {"type": "metrics", "metrics": metrics}
            yield {"type": "done", "text": text, "metrics": metrics}
            finished = True
        finally:
            ACTIVE["now"] -= 1
            if not finished:
                # Генератор закрыли на середине: именно так выглядит погашенный
                # вызов, когда клиент ушёл со страницы.
                ACTIVE["closed"] += 1

    return fake_stream_completion


def install(module_names=("app.agent",), **kwargs) -> None:
    """Подменяет stream_completion в перечисленных модулях."""
    import importlib

    fake = make(**kwargs)
    for name in module_names:
        module = importlib.import_module(name)
        module.stream_completion = fake


def use_temp_db(path: str | None = None) -> str:
    """Уводит базу агентов во временный файл — до импорта app.*.

    Без этого проверки писали бы в рабочую базу стенда: реестр открывает
    хранилище на импорте, а путь берёт из AGENT_DB_PATH один раз.

    Без аргумента каждый процесс получает **свежий** каталог, даже если
    AGENT_DB_PATH унаследован от родителя: иначе `spawn_100.py`, запущенный
    подпроцессом, писал бы сотню сессий в базу проверок, и следующий агент
    родителя поднял бы чужую историю по совпавшему id. Явный путь нужен
    проверке перезапуска — там два процесса обязаны видеть один файл.
    """
    import os
    import tempfile

    if path is None:
        path = os.path.join(tempfile.mkdtemp(prefix="checks-db-"), "agents.db")
    os.environ["AGENT_DB_PATH"] = path
    return path


def install_offline(db_path: str | None = None) -> None:
    """Убирает из проверок всё, что ходит в сеть, кроме самого стрима.

    Каталог моделей — живой HTTP-запрос к OpenRouter; ключа он не требует,
    но проверки обязаны работать без сети вовсе. Ключ подменяем на «есть»,
    иначе ручка сообщения отдаст 503 раньше, чем дойдёт до агента.

    Заодно уводит базу во временный файл: импорт app.main поднимает реестр,
    а тот открывает хранилище.
    """
    use_temp_db(db_path)

    import app.catalog as catalog
    import app.main as main

    async def no_catalog():
        return []

    catalog.fetch_models = no_catalog
    main.has_key = lambda: True
