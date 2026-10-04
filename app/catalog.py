"""Каталог моделей OpenRouter: /api/v1/models, ~431 запись, ключа НЕ требует.

Каталог отдаётся целиком, без отбора: какая модель годится, решает
пользователь. А вот **данные** о модели отдаются все — по ним панель
предупреждает, что заданный параметр модель не потянет. Без этого
`provider.require_parameters=true` молча выкосил бы провайдеров.
"""

from __future__ import annotations

import time
import math

import httpx

from .config import OPENROUTER_BASE_URL

_TTL_SECONDS = 15 * 60
_cache: dict[str, object] = {"fetched_at": 0.0, "models": []}

# Семейства, которые обрезают температуру на 1.0 и возвращают 400 на 1.2,
# при этом честно перечисляя "temperature" в supported_parameters: по нему
# такая модель выглядит подходящей, и предупредить о потолке больше нечем.
TEMPERATURE_CAPPED_PREFIXES = ("anthropic/",)
TEMPERATURE_CAP = 1.0


async def fetch_models(purpose: str = "generation") -> list[dict]:
    if purpose == "embedding":
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            from .config import api_key
            api_key()
            from shared_models import key as model_key
            key = model_key("openrouter")
            response = await client.get(f"{OPENROUTER_BASE_URL}/embeddings/models", headers={"Authorization": f"Bearer {key}"} if key else {})
            response.raise_for_status()
            payload = response.json()
        return sorted((_normalize(m) for m in payload.get("data", [])), key=lambda m: m["id"])
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


def _price(pricing: dict, key: str) -> float | None:
    raw = pricing.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _normalize(raw: dict) -> dict:
    pricing = raw.get("pricing") or {}
    model_id = raw.get("id", "")
    prompt_price = _price(pricing, "prompt")
    completion_price = _price(pricing, "completion")
    capped = model_id.startswith(TEMPERATURE_CAPPED_PREFIXES)
    return {
        "id": model_id,
        "context_length": raw.get("context_length") if type(raw.get("context_length")) is int and raw["context_length"] > 0 else None,
        "supported_parameters": raw.get("supported_parameters") or [],
        # цены за 1M токенов — то, в чём их привычно читать
        "prompt_price_per_m": round(prompt_price * 1_000_000, 4) if prompt_price is not None else None,
        "completion_price_per_m": round(completion_price * 1_000_000, 4) if completion_price is not None else None,
        "temperature_capped": capped,
        "temperature_cap": TEMPERATURE_CAP if capped else None,
    }
