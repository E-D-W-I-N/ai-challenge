"""Common catalog adapter and explicit upstream metadata."""
import math


async def fetch_models(purpose="generation"):
    from .model_settings import current_connection
    from .rag_models import models
    return (await models(current_connection(), purpose))["models"]


def normalize(raw):
    result = {"id": raw["id"]}
    context = raw.get("context_length")
    if type(context) is int and context > 0:
        result["context_length"] = context
    pricing = raw.get("pricing")
    if isinstance(pricing, dict):
        for name in ("prompt", "completion"):
            try:
                value = float(pricing[name])
                if not isinstance(pricing[name], bool) and math.isfinite(value) and value >= 0:
                    result[name + "_price_per_m"] = value * 1000000
            except (KeyError, ValueError, TypeError, OverflowError):
                pass
    return result
