"""Конфиг агента: всё, чем один чат отличается от другого.

Сотня агентов с разными конфигами — это сотня `AgentSpec`, а не сотня
процессов. Датакласс намеренно без методов: `replace(spec, ...)` обязан
оставаться дешёвым.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentSpec:
    label: str
    model: str
    """id модели OpenRouter, например "meta-llama/llama-3.1-8b-instruct"."""

    system: str = ""
    """Системный промпт. Дом у него ровно один — это поле."""

    # Параметры сэмплирования. None значит «не отправлять параметр», а не
    # «отправить ноль»: с provider.require_parameters=true каждый заданный
    # параметр сужает список провайдеров, готовых обслужить вызов.
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
    """Дополнительные поля запроса (seed, provider.order и т.п.). Мержится
    поверх тела; `provider.require_parameters` уже стоит в app/llm.py, там же
    `plugins` мержатся по `id`: выключенный `context-compression` уцелеет
    рядом с вашим, а названный тем же `id` — победит."""
