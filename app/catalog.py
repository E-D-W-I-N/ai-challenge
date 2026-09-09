"""Каталог моделей OpenRouter: /api/v1/models.

714 КБ и ~431 запись, ключа НЕ требует — дропдаун живой ещё до того, как
появится .env. Кэшируется в процессе с TTL.

Каталог отдаётся целиком, без отбора: какая модель годится под разговор,
решает пользователь. Отбор здесь был, пока клиент сравнивал модели между
собой, — в чате прятать от пользователя половину списка незачем.

А вот **данные** о модели каталог отдаёт все, какие есть: панель по ним
предупреждает, что заданный параметр выбранная модель не потянет. Без этого
`provider.require_parameters=true` молча выкосил бы провайдеров, и вместо
ответа пришла бы невнятная ошибка.
"""

from __future__ import annotations

import time

import httpx

from .config import OPENROUTER_BASE_URL

# Ручного сброса кэша нет намеренно: кнопки в UI не было, а каталог сам
# обновится по TTL. Понадобится — вернём вместе с кнопкой, а не отдельным
# параметром, который некому нажать.
_TTL_SECONDS = 15 * 60
_cache: dict[str, object] = {"fetched_at": 0.0, "models": []}

# Семейства, которые обрезают температуру на 1.0 и возвращают 400 на 1.2,
# при этом честно перечисляя "temperature" в supported_parameters. Одного
# supported_parameters мало: по нему такая модель выглядит подходящей,
# и предупредить о потолке больше нечем.
TEMPERATURE_CAPPED_PREFIXES = ("anthropic/",)

TEMPERATURE_CAP = 1.0
"""Потолок температуры у семейств из TEMPERATURE_CAPPED_PREFIXES."""


async def fetch_models() -> list[dict]:
    now = time.monotonic()
    if _cache["models"] and now - float(_cache["fetched_at"]) < _TTL_SECONDS:
        return list(_cache["models"])  # type: ignore[arg-type]

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{OPENROUTER_BASE_URL}/models")
        response.raise_for_status()
        payload = response.json()

    models = [_normalize(m) for m in payload.get("data", [])]
    models.sort(key=lambda m: m["id"])
    _cache["models"] = models
    _cache["fetched_at"] = now
    return list(models)


def _price(pricing: dict, key: str) -> float:
    try:
        return float(pricing.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalize(raw: dict) -> dict:
    pricing = raw.get("pricing") or {}
    model_id = raw.get("id", "")
    prompt_price = _price(pricing, "prompt")
    completion_price = _price(pricing, "completion")
    return {
        "id": model_id,
        "name": raw.get("name") or model_id,
        "context_length": raw.get("context_length") or 0,
        # По нему панель предупреждает, что заданный параметр модель
        # не заявляет: с provider.require_parameters=true это не мелочь,
        # а разница между ответом и ошибкой без объяснений.
        "supported_parameters": raw.get("supported_parameters") or [],
        # цены за 1M токенов — то, в чём их привычно читать
        "prompt_price_per_m": round(prompt_price * 1_000_000, 4),
        "completion_price_per_m": round(completion_price * 1_000_000, 4),
        "is_free": model_id.endswith(":free") or (prompt_price == 0 and completion_price == 0),
        "temperature_capped": model_id.startswith(TEMPERATURE_CAPPED_PREFIXES),
        "temperature_cap": TEMPERATURE_CAP if model_id.startswith(TEMPERATURE_CAPPED_PREFIXES) else None,
    }
