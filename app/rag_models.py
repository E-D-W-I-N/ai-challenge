"""Read-only model catalogues; credentials stay at the HTTP boundary."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from rag.semantic import SemanticConfig
from . import catalog, config

_MAX_BYTES = 2 * 1024 * 1024
_MAX_MODELS = 5000


async def models(auth_mode: str, base_url: str, purpose: str = "generation") -> dict:
    if purpose not in {"generation", "embedding"}:
        raise HTTPException(422, "Неизвестное назначение модели.")
    try:
        SemanticConfig(base_url=base_url, model="catalogue", auth_mode=auth_mode)
        parsed = urlparse(base_url)
        if not parsed.hostname:
            raise ValueError("Missing host")
        parsed.port  # Validate range before any transport receives the URL.
    except ValueError:
        raise HTTPException(422, "URL сервера должен быть HTTP(S), без авторизации, параметров и фрагмента") from None
    endpoint = base_url.rstrip("/")
    if auth_mode == "openrouter" and endpoint == config.OPENROUTER_BASE_URL:
        try:
            rows = await catalog.fetch_models()
        except Exception:
            raise HTTPException(502, "Каталог OpenRouter недоступен. Попробуйте обновить список позже.") from None
        return {"models": rows, "total": len(rows)}

    key = (os.environ.get("RAG_EMBEDDING_API_KEY", "").strip() if auth_mode == "omlx" else config.api_key()) or ""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        # OpenRouter follows the same proxy policy as chat; local oMLX bypasses it.
        async with httpx.AsyncClient(timeout=15, trust_env=auth_mode != "omlx", follow_redirects=False) as client:
            async with client.stream("GET", f"{endpoint}/models", headers=headers) as response:
                if not response.is_success:
                    raise HTTPException(502, f"Каталог моделей недоступен: сервер вернул HTTP {response.status_code}.")
                content = bytearray()
                async for part in response.aiter_bytes(chunk_size=65536):
                    content.extend(part)
                    if len(content) > _MAX_BYTES:
                        raise HTTPException(502, "Ответ каталога моделей слишком большой.")
        payload = json.loads(content)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) > _MAX_MODELS:
            raise ValueError("Invalid catalogue")
        secrets = [value for value in (key, os.environ.get("RAG_EMBEDDING_API_KEY", ""), os.environ.get("OPENROUTER_API_KEY", "")) if value.strip()]
        identifiers = {}
        for row in data:
            identifier = row.get("id") if isinstance(row, dict) else None
            if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 512:
                raise ValueError("Invalid model ID")
            if any(secret in identifier or secret.strip() in identifier for secret in secrets):
                raise ValueError("Invalid model ID")
            model_type = row.get("model_type", row.get("type"))
            model_type = model_type.strip().lower() if isinstance(model_type, str) and model_type.strip() else None
            if identifier in identifiers and identifiers[identifier] != model_type:
                model_type = None
            identifiers[identifier] = model_type
        # Standard oMLX has no type metadata. Incomplete extension metadata is
        # insufficient to hide IDs; only filter a fully typed catalogue.
        typed = bool(identifiers) and all(value in {"embedding", "embeddings", "llm", "vlm", "reranker", "audio_stt", "audio_tts", "audio_sts"}
                                             for value in identifiers.values())
        rows = [{"id": identifier} for identifier in sorted(identifiers)
                if purpose != "embedding" or not typed or identifiers[identifier] in {"embedding", "embeddings"}]
        return {"models": rows, "total": len(rows)}
    except HTTPException:
        raise
    except httpx.InvalidURL:
        raise HTTPException(422, "Некорректный URL сервера моделей.") from None
    except httpx.TimeoutException:
        raise HTTPException(502, "Сервер не ответил на запрос списка моделей. Попробуйте обновить список.") from None
    except httpx.RequestError:
        raise HTTPException(502, "Не удалось подключиться к каталогу моделей. Проверьте URL сервера.") from None
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(502, "Сервер вернул некорректный каталог моделей.") from None
