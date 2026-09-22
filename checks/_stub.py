"""Заглушка стрима к модели: проверки идут без сети и без ключа.

Подменяет `stream_completion` в `app.agent`, записывает каждый вызов и отдаёт
детерминированный ответ чанками. По записанным вызовам и проверяется главное:
что именно агент отправил в модель.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields as dataclass_fields
from typing import Callable

CALLS: list[dict] = []
"""Все вызовы к «модели»: {"model", "label", "messages", "payload"}. `payload`
собирается настоящим `build_payload`, а не пересказывается: проверки обязаны
смотреть на то самое тело, которое ушло бы в OpenRouter."""

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
    reasoning: str = "",
    usage: Callable[[int], dict] | dict | None = None,
    tool_calls: Callable[[list[dict], int], list | None] | list | None = None,
):
    """Собирает заглушку `stream_completion`.

    reply — текст ответа или функция (messages, номер вызова) → текст.
    fail — вместо ответа отдать событие error, как это делает HTTP 4xx.
    delay — пауза между чанками: нужна проверке обрыва, чтобы успеть оборвать.
    reasoning — рассуждение отдельным полем дельты.
    usage — словарь полей `Metrics` или функция (номер вызова) → словарь.
        Без него у каждого ответа одни числа, и сумма по чату сошлась бы
        на любом коде. `None` в поле законен: так провайдер молчит о цифре.
    tool_calls — список или функция (messages, номер вызова) → список. Уже
        собранные, а не обрывками по `index`: склейку стережёт своя проверка
        на настоящем `stream_completion`.
    """
    from app.llm import Metrics, build_payload

    known = {f.name for f in dataclass_fields(Metrics)}

    async def fake_stream_completion(
        session, *, prompt_override=None, context_length=None, tools=None, tool_choice=None
    ):
        messages = list(prompt_override or [])
        index = len(CALLS)
        CALLS.append(
            {
                "model": session.model,
                "label": session.label,
                "messages": [dict(m) for m in messages],
                # Инструменты и принуждение к вызову уезжают в настоящий
                # `build_payload`: проверки смотрят в записанное тело
                # и спрашивают его «объявлены ли `tools` и какие, есть ли
                # `tool_choice`», а пересказ заглушки отвечал бы за себя.
                "payload": build_payload(
                    session, prompt_override, tools=tools, tool_choice=tool_choice
                ),
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
            # `content` бывает `None` — так едет ход ассистента, целиком
            # ушедший на вызовы. Упади заглушка здесь, обмен получил бы
            # «ошибку транспорта» там, где транспорт ни при чём.
            prompt_tokens=sum(len(m.get("content") or "") for m in messages) // 4,
            completion_tokens=tokens,
            total_tokens=100,
            cost_usd=0.000123,
            finish_reason="stop",
            model=session.model,
            provider="stub",
            context_length=context_length,
        )

        # Заданный usage кладётся в тот же датакласс, а не поверх словаря:
        # опечатка в имени поля обязана падать здесь, а не тихо доезжать
        # до проверки новым ключом, которого нет ни у одной настоящей метрики.
        if usage is not None:
            values = usage(index) if callable(usage) else usage
            unknown = set(values) - known
            assert not unknown, f"в usage поля, которых нет в Metrics: {sorted(unknown)}"
            for name, value in values.items():
                setattr(metrics, name, value)

        metrics = metrics.as_dict()

        ACTIVE["now"] += 1
        ACTIVE["peak"] = max(ACTIVE["peak"], ACTIVE["now"])
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

            # Вызовы — после текста и до метрик, ровно там, где их отдаёт
            # настоящий транспорт: причину обмена провайдер называет на
            # последнем содержательном куске. Форма события та же.
            calls = tool_calls(messages, index) if callable(tool_calls) else tool_calls
            if calls:
                metrics = {**metrics, "finish_reason": "tool_calls"}
                yield {
                    "type": "tool_calls",
                    "calls": [dict(call) for call in calls],
                    "metrics": metrics,
                }

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


def use_temp_db(path: str | None = None) -> str:
    """Уводит базу во временный файл — до импорта app.*, иначе проверки писали
    бы в рабочую базу. Без аргумента каждый процесс получает **свежий**
    каталог, даже если AGENT_DB_PATH унаследован; явный путь нужен проверке
    перезапуска, где два процесса обязаны видеть один файл."""
    import os
    import tempfile

    if path is None:
        path = os.path.join(tempfile.mkdtemp(prefix="checks-db-"), "agents.db")
    os.environ["AGENT_DB_PATH"] = path
    return path


def install_offline(db_path: str | None = None) -> None:
    """Убирает из проверок всё, что ходит в сеть, кроме самого стрима. Ключ
    подменяем на «есть», иначе ручка отдаст 503 раньше, чем дойдёт до агента.
    Заодно уводит базу во временный файл: импорт app.main поднимает реестр."""
    use_temp_db(db_path)

    import app.catalog as catalog
    import app.main as main

    async def no_catalog():
        return []

    catalog.fetch_models = no_catalog
    main.has_key = lambda: True
