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

    # Управление контекстом. Идиом тот же, что у сэмплирования: None — «не
    # делать», а не «делать с нулём». `keep_last is None` значит, что резать
    # нечем и в модель уезжает вся история, как в Дне 8. Булева переключателя
    # нет намеренно: в Python True — это int, и разбор полей его отбрасывает
    # (`_optional_field`, app/main.py).
    strategy: str = "full"
    """Чем управляется контекст: `full` — вся история, `window` — последние
    `keep_last` реплик, остальное отброшено, `summary` — сводка начала плюс
    хвост. Строка, а не `Literal` и не `bool`: `Literal` не по зубам разбору
    round-trip проверки, а `bool` отбрасывается разбором полей. Значение не
    из списка ручки не пропустят (`_choice_field`, app/main.py), а
    записанное чужой версией сервера читается как `full` — обрезка бывает
    только выбранная."""

    keep_last: int | None = None
    """Сколько последних реплик уезжает в модель как есть — хвост окна и хвост
    сводки это одно и то же число."""

    compress_every: int | None = None
    """Порог: сводка пересобирается, когда несвёрнутых реплик старше окна
    накопилось столько. None — сжатие не запускается никогда."""

    extra_body: dict = field(default_factory=dict)
    """Дополнительные поля запроса (seed, provider.order и т.п.). Мержится
    поверх тела; `provider.require_parameters` уже стоит в app/llm.py, там же
    `plugins` мержатся по `id`: выключенный `context-compression` уцелеет
    рядом с вашим, а названный тем же `id` — победит."""


STRATEGIES = ("full", "window", "summary")
"""Чем управлять контекстом — список целиком. Порядок тот же, в каком стратегии
стоят в переключателе панели: от «ничего не делаем» к «делаем больше всего»."""

CONTEXT_NUMBERS = ("keep_last", "compress_every")
"""Числовая половина управления контекстом: разбирается как сэмплирование —
целое или null, где null значит «не делать»."""

CONTEXT_FIELDS = ("strategy", *CONTEXT_NUMBERS)
"""Поля управления контекстом. В тело запроса они не уезжают ни одним ключом:
сжатие наше, а не провайдерское, — его плагин выключен на каждом вызове.
Стратегия первой: она решает, значат ли что-нибудь два числа за ней."""
