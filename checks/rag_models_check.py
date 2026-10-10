"""Bounded unified provider catalogues over offline HTTP, no model inference."""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException


def check_rag_models():
    from app import rag_models
    from shared_models import Connection
    calls = []
    state = {"status": 200, "payload": {"data": [{"id": "neutral-generation"}, {"id": "name-embedding-is-not-type"}]}}
    original = httpx.AsyncClient
    def respond(request):
        calls.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(state["status"], json=state["payload"])
    def client(*args, **kwargs):
        return original(*args, **kwargs, transport=httpx.MockTransport(respond))
    async def scenarios():
        with patch.object(rag_models.httpx, "AsyncClient", client), patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": " neutral-compatible-secret ", "OPENROUTER_API_KEY": "neutral-router-secret"}):
            result = await rag_models.models(Connection("http://neutral.test/v1", "neutral-compatible-secret"), "embedding")
            assert result["models"] == [{"id": "name-embedding-is-not-type"}, {"id": "neutral-generation"}]
            assert calls[-1] == ("http://neutral.test/v1/models", "Bearer neutral-compatible-secret")
            state["payload"] = {"data": [{"id": "neutral-vector", "type": "embedding"}, {"id": "neutral-generation", "type": "llm"}]}
            assert (await rag_models.models(Connection("http://neutral.test/v1", "neutral-compatible-secret"), "embedding"))["models"] == [{"id": "neutral-vector"}]
            assert (await rag_models.models(Connection("http://neutral.test/v1", "neutral-compatible-secret")))["models"] == [{"id": "neutral-generation"}]
            for malformed in ({"bad": []}, {"data": [{"id": "neutral-compatible-secret"}]},
                              {"data": [{"id": "neutral%2dcompatible%2dsecret"}]},
                              {"data": [{"id": "x" * 513}]}, {"data": [None]}):
                state["payload"] = malformed
                try: await rag_models.models(Connection("http://neutral.test/v1", "neutral-compatible-secret"))
                except HTTPException as error:
                    assert error.status_code == 502 and "secret" not in error.detail
                else: raise AssertionError("Malformed catalogue accepted")
            state.update(status=401, payload={"error": "neutral-compatible-secret"})
            try: await rag_models.models(Connection("http://neutral.test/v1", "neutral-compatible-secret"))
            except HTTPException as error: assert error.status_code == 502 and "HTTP 401" in error.detail and "secret" not in error.detail
            else: raise AssertionError("HTTP failure accepted")
    asyncio.run(scenarios())
    return "shared catalogue IDs/types/unknown metadata; common /models; bounded URL/auth failures"
