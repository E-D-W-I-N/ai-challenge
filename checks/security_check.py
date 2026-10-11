"""Offline fake-key boundaries for chat errors and credential-bearing RAG requests."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from shared_models import Connection, bind_connection
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def check_security():
    from app import llm, rag_api, rag_models, model_settings
    from app.schema import AgentSpec

    keys = ["offline-selected-private", "offline-unused-local", "offline-unused-legacy"]
    reflected = [*keys, "  " + keys[0] + "  ", "sk-or-v1-NEUTRAL-NOT-A-REAL-KEY"]
    reflected += ["".join(f"\\u{ord(char):04x}" for char in keys[0]),
                  "".join(f"%{ord(char):02X}" for char in keys[0]),
                  "".join(f"%25{ord(char):02X}" for char in keys[0])]
    upstream_text = "upstream-private-marker " + json.dumps(reflected) + "\n" + "neutral " * 10000
    selected = AgentSpec(label="offline security", model="neutral/model")

    async def failure_events(status=None, exception=None):
        sent, consumed = [], []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                consumed.append(True)
                yield upstream_text.encode()
        def upstream(request):
            sent.append(request.headers.get("Authorization"))
            if exception is not None:
                raise exception(upstream_text, request=request)
            return httpx.Response(status, stream=Body())
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            with patch.object(llm, "shared_client", return_value=client), patch.object(llm, "model_key", return_value=keys[0]):
                events = [event async for event in llm.stream_completion(selected, prompt_override=[])]
        return events, sent, consumed

    with bind_connection(Connection(api_key=keys[1])):
        for code, hint in [(401, "авторизацию"), (403, "доступ"), (429, "лимит"), (503, "недоступен")]:
            events, sent, consumed = asyncio.run(failure_events(status=code))
            assert sent == ["Bearer " + keys[0]] and consumed == []
            assert len(events) == 1 and events[0]["type"] == "error"
            error = events[0]
            assert error["message"] == error["metrics"]["error"] and f"HTTP {code}" in error["message"] and hint in error["message"]
            wire = json.dumps(events, ensure_ascii=False)
            assert len(error["message"]) < 200 and all(value not in wire for value in reflected) and "upstream-private-marker" not in wire
        for exception, hint in [(httpx.ReadTimeout, "вовремя"), (httpx.ConnectError, "подключиться"), (httpx.RemoteProtocolError, "протокол")]:
            events, sent, _ = asyncio.run(failure_events(exception=exception))
            assert sent == ["Bearer " + keys[0]] and len(events) == 1 and events[0]["type"] == "error"
            assert events[0]["message"] == events[0]["metrics"]["error"] and hint in events[0]["message"]
            wire = json.dumps(events, ensure_ascii=False)
            assert len(events[0]["message"]) < 200 and all(value not in wire for value in reflected) and "upstream-private-marker" not in wire

        outbound = []
        original_client = httpx.AsyncClient
        def catalogue(request):
            outbound.append((str(request.url), request.headers.get("Authorization")))
            return httpx.Response(200, json={"data": [{"id": "neutral-generation"}]})
        def client_with_fixture(*args, **kwargs):
            return original_client(*args, **kwargs, transport=httpx.MockTransport(catalogue))
        from app import main
        app = FastAPI(); app.include_router(rag_api.router)
        app.add_api_route("/api/models", main.list_models, methods=["GET"])
        main.REGISTRY.store.save_model_settings({"base_url": "https://neutral-upstream.test/v1"})
        params = {}
        denied = [
            {"Origin": "https://unrelated.test", "Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
            {"Origin": "null"}, {"Origin": ""}, {"Origin": "not-an-origin"},
            {"Origin": "http://testserver:81"}, {"Origin": "http://testserver:0"}, {"Origin": "https://testserver"},
            {"Origin": "http://user:pass@testserver"}, {"Origin": "http://%74estserver"},
            {"Origin": "http://testserver/path"}, {"Origin": "http://testserver?x=1"},
            {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
            [("Origin", "http://testserver"), ("Origin", "https://unrelated.test")],
        ]
        with patch.object(rag_models.httpx, "AsyncClient", client_with_fixture), patch.object(model_settings, "current_connection", return_value=Connection("https://neutral-upstream.test/v1", keys[1])) as key_reader, \
                patch.object(rag_api, "Index") as index, patch.object(rag_api, "Operation") as operation, TestClient(app) as api:
            for headers in denied:
                response = api.get("/api/models", params=params, headers=headers)
                assert response.status_code == 403 and all(key not in response.text for key in keys)
                assert api.post("/api/rag/operations/chunks", json={"strategy": "semantic"}, headers=headers).status_code == 403
                assert api.post("/api/rag/operations/embeddings", content="{}", headers={**dict(headers), "Content-Type": "text/plain"}).status_code == 403
                assert api.delete("/api/rag/stages/index", headers=headers).status_code == 403
            assert outbound == [] and key_reader.call_count == 0 and index.call_count == 0 and operation.call_count == 0
            for headers in [{}, {"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"},
                            {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}, {"Origin": "HTTP://TESTSERVER:80"}]:
                response = api.get("/api/models", params=params, headers=headers)
                assert response.status_code == 200 and response.json()["models"] == [{"id": "neutral-generation"}]
            assert len(outbound) == 5 and key_reader.call_count == 5 and all(auth == "Bearer " + keys[1] for _, auth in outbound)
            # Trusted requests still reach the original route, without launching work
            # for an unknown operation/delete kind.
            assert api.post("/api/rag/operations/unknown", json={}, headers={"Origin": "http://testserver"}).status_code == 404
            assert api.delete("/api/rag/stages/unknown", headers={"Origin": "http://testserver"}).status_code == 404
        for base, origin, host in [("http://localhost:8000", "http://localhost:8000", "localhost:8000"),
                                   ("https://localhost", "https://LOCALHOST:443", "localhost"),
                                   ("http://testserver", "http://[::1]:8000", "[::1]:8000")]:
            with patch.object(rag_models.httpx, "AsyncClient", client_with_fixture), TestClient(app, base_url=base) as api:
                assert api.get("/api/models", params=params, headers={"Host": host, "Origin": origin, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    return "chat status/category errors omit raw/encoded secrets; RAG origin guard precedes keys and effects"


if __name__ == "__main__":
    print(check_security())
