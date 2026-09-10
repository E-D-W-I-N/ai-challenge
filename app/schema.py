"""Конфиг агента: всё, чем один чат отличается от другого.

Сотня агентов с разными конфигами — это сотня `AgentSpec`, а не сотня
процессов. Датакласс намеренно без методов: `replace(spec, ...)` обязан
оставаться дешёвым.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentSpec:
    """Конфиг одного агента.

    Всё, чем один агент отличается от другого при спавне: модель, параметры
    сэмплирования, системный промпт и место в списке слева.
    """

    label: str
    """Имя агента в списке слева."""

    model: str
    """id модели OpenRouter, например "meta-llama/llama-3.1-8b-instruct"."""

    system: str = ""
    """Системный промпт агента. Дом у него ровно один — это поле."""

    # --- параметры сэмплирования ------------------------------------------
    #
    # Все до одного необязательные, и None значит «не отправлять параметр»,
    # а не «отправить ноль». Пустое поле в панели справа — это None.
    # Помнить про provider.require_parameters=true: каждый заданный параметр
    # сужает список провайдеров, готовых обслужить вызов.

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repetition_penalty: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None

    stop: list[str] | None = None
    response_format: dict | None = None

    extra_body: dict = field(default_factory=dict)
    """Дополнительные поля запроса (seed, provider.order и т.п.).

    Мержится поверх тела. `provider.require_parameters` уже стоит в
    app/llm.py — можно дополнить, переопределять не нужно.
    """
