"""Конфиг агента: всё, чем один чат отличается от другого.

Датакласс без методов: `replace(spec, ...)` обязан оставаться дешёвым.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentSpec:
    label: str
    model: str
    """id модели OpenRouter, например "meta-llama/llama-3.1-8b-instruct"."""

    system: str = ""

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

    strategy: str = "full"
    """Сколько реплик уезжает в модель дословно: `full` — вся история,
    `window` — последние `keep_last`, `summary` — сводка плюс `keep_last`.

    Строка, а не `bool` и не `Literal`: `bool` отбрасывается разбором полей
    (в Python `True` это `int`), `Literal` валит round-trip проверку конфига.
    Чужое значение ручки не пропустят, записанное — читается как `full`."""

    workflow: str = "off"
    """Ведёт ли агент план задачи: `off` или `plan`. Не пункт переключателя
    обрезки: план ведётся при любой стратегии. Строка — доводы те же, что
    у `strategy`; незнакомое значение читается как `off` (`Agent.plan_on`)."""

    keep_last: int | None = None
    """Сколько последних реплик уезжает как есть — хвост окна и хвост сводки
    это одно и то же число."""

    compress_every: int | None = None
    """Порог пересборки сводки: столько несвёрнутых реплик старше окна.
    None — сжатие не запускается никогда."""

    extra_body: dict = field(default_factory=dict)
    """Дополнительные поля запроса (seed, provider.order и т.п.). Мержится
    поверх тела; `plugins` — по `id`, так что выключенный
    `context-compression` уцелеет рядом с вашим."""


STRATEGIES = ("full", "window", "summary")
"""Порядок тот же, что в переключателе панели: от «ничего не делаем»
к «делаем больше всего»."""

WORKFLOWS = ("off", "plan")
"""Рядом со `STRATEGIES`, а не внутри: вопросы разные — сколько реплик уедет
дословно и ведётся ли список шагов."""

CONTEXT_NUMBERS = ("keep_last", "compress_every")
"""Разбираются как сэмплирование: целое или null, где null — «не делать»."""

MEMORY_KINDS = ("profile", "decision", "knowledge")
"""Типы записи долговременной памяти: о собеседнике, решение, факт."""

MEMORY_LABELS = {"profile": "о собеседнике", "decision": "решение", "knowledge": "факт"}
"""Одна карта на врезку и на вкладку: второй таблицей подписи разъехались бы
молча — в промпте одно слово, на экране другое."""

WORKING_KINDS = ("goal", "limit", "decision", "question")
"""Типы записи рабочей памяти. Взяты из модели слоя: он хранит состояние
задачи."""

WORKING_LABELS = {
    "goal": "цель",
    "limit": "ограничение",
    "decision": "решение",
    "question": "открытый вопрос",
}
"""Одна карта на врезку, на ответ модели и на вкладку — довод тот же, что
у `MEMORY_LABELS`."""

CONTEXT_FIELDS = ("strategy", "workflow", *CONTEXT_NUMBERS)
"""В тело запроса не уезжают ни одним ключом: сжатие наше. Стратегия первой —
она решает, значат ли что-нибудь два числа за ней. Список перечисляют
`spec_as_dict`, `PATCHABLE` и разбор тела, а не поля по одному."""

PROFILE_FIELDS = ("style", "format", "context")
"""Профиль: как отвечать (стиль), чем отвечать (формат), над чем человек
работает (контекст). Свободный текст, а не список готовых значений. Не поле
`AgentSpec`: профиль один на всю базу, как долговременная память."""

PROFILE_LABELS = {"style": "стиль", "format": "формат", "context": "контекст"}
"""Одна карта на врезку и на вкладку — довод тот же, что у `MEMORY_LABELS`."""
