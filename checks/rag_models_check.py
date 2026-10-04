"""Neutral HTTP fixtures for the generation model picker; never inference."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def check_rag_models():
    calls = []
    state = {"status": 200, "payload": {"data": [{"id": "neutral-llm"}, {"id": "embedding-name-is-not-a-type"}, {"id": "neutral-llm"}]}}
    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state["payload"]).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": "  neutral-local-key  ", "OPENROUTER_API_KEY": "  neutral-router-key  ",
                "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
            from app.rag_api import router
            from app import rag_models
            app = FastAPI(); app.include_router(router)
            with TestClient(app) as api:
                params = {"auth_mode": "omlx", "base_url": base + "/"}
                response = api.get("/api/rag/models", params=params)
                assert response.status_code == 200, response.text
                assert response.json() == {"models": [{"id": "embedding-name-is-not-a-type"}, {"id": "neutral-llm"}], "total": 2}
                assert calls == [("/v1/models", "Bearer neutral-local-key")]
                with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": " \t "}):
                    assert api.get("/api/rag/models", params=params).status_code == 200
                    assert calls[-1] == ("/v1/models", None)
                with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                    assert api.get("/api/rag/models", params={**params, "auth_mode": "openrouter"}).status_code == 200
                    assert calls[-1] == ("/v1/models", "Bearer neutral-router-key")
                # Custom OpenRouter follows chat proxy policy, unlike local oMLX.
                response = api.get("/api/rag/models", params={**params, "auth_mode": "openrouter"})
                assert response.status_code == 502 and "neutral-router-key" not in response.text
                models = [{"id": "neutral-router", "prompt_price_per_m": 1.2, "completion_price_per_m": 3.4}]
                with patch.object(rag_models.catalog, "fetch_models", AsyncMock(return_value=models)) as public:
                    response = api.get("/api/rag/models", params={"auth_mode": "openrouter", "base_url": "https://openrouter.ai/api/v1/"})
                    assert response.json() == {"models": models, "total": 1}
                    public.assert_awaited_once()
                before = len(calls)
                for invalid in ["file:///neutral", "http://user:pass@127.0.0.1/v1", "http://127.0.0.1:999999/v1", base + "?key=neutral", base + "#fragment"]:
                    assert api.get("/api/rag/models", params={**params, "base_url": invalid}).status_code == 422
                assert len(calls) == before
                state.update(status=401, payload={"error": "neutral-local-key " + "x" * 20000})
                response = api.get("/api/rag/models", params=params)
                assert response.status_code == 502 and "HTTP 401" in response.text and len(response.text) < 300
                assert "neutral-local-key" not in response.text
                state.update(status=200, payload={"data": []})
                assert api.get("/api/rag/models", params=params).json() == {"models": [], "total": 0}
                for invalid in [{"bad": []}, {"data": [{"id": "neutral-local-key"}]}, {"data": [{"id": "x" * 513}]}, {"data": [None]}]:
                    state["payload"] = invalid
                    response = api.get("/api/rag/models", params=params)
                    assert response.status_code == 502 and "neutral-local-key" not in response.text
                state["payload"] = {"data": [{"id": "x" * (2 * 1024 * 1024)}]}
                response = api.get("/api/rag/models", params=params)
                assert response.status_code == 502 and len(response.text) < 300
        return "runtime auth/proxy/catalog reuse; actual GET IDs and bounded safe failures"
    finally:
        server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    print(check_rag_models())
