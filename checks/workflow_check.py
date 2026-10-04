"""Neutral staged durability, publication, deletion, async lock and API boundaries."""
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import httpx

from rag import workflow
from rag.artifacts import clear, available
from rag.documents import ingest, write_json
from rag.embeddings import EmbeddingConfig
from rag.index import Index, Operation, stage_chunks, stage_embeddings, save_index


def check_workflow():
    from checks import _stub
    _stub.install_offline()
    from app.main import app
    from fastapi.testclient import TestClient
    calls = []
    def embed(request):
        import json
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"model": body["model"], "data": [{"index": i, "embedding": [1, i + 1, 2]} for i in range(len(body["input"]))]})
    with tempfile.TemporaryDirectory(prefix="workflow-check-") as temporary:
        root = Path(temporary)
        html = root / "neutral.html"
        html.write_text('<article><h1>Neutral sample</h1><p>' + 'A neutral paragraph for stage checks. ' * 60 + '</p></article>', encoding="utf-8")
        ingest([{"path": str(html), "source": "https://example.test/neutral"}], root)
        with httpx.Client(transport=httpx.MockTransport(embed)) as client:
            config = EmbeddingConfig("http://127.0.0.1:8005/v1", "offline-test")
            stage_chunks(root, "fixed", 400, 40)
            first = workflow.stages(root)
            assert first["chunks"]["size"] == 400 and first["embeddings"] is None
            assert not (root / "index.sqlite").exists()
            stage_embeddings(root, config, client=client)
            assert not (root / "index.sqlite").exists()
            before_calls = len(calls)
            saved = save_index(root)
            assert len(calls) == before_calls
            old = (root / "index.sqlite").read_bytes()
            stage_chunks(root, "fixed", 450, 40)
            assert workflow.stages(root)["embeddings"] is None
            try:
                save_index(root)
                raise AssertionError("Published stale vectors")
            except ValueError:
                pass
            assert (root / "index.sqlite").read_bytes() == old
            stage_embeddings(root, config, client=client)
            with Operation(root, "delete_embeddings") as operation:
                clear(root, "embeddings", operation)
            assert workflow.stages(root)["chunks"] and not workflow.stages(root)["embeddings"]
            assert Index(root).status()["index"] is None
            before_calls = len(calls)
            stage_embeddings(root, config, client=client)
            assert len(calls) > before_calls
            save_index(root)
            # Crash after tombstone commit, before cleanup: old bytes never reappear.
            write_json(root / "tombstone.json", {"files": ["index.sqlite", "vectors.json", "chunks.json"]})
            assert (root / "index.sqlite").exists() and Index(root).status()["index"] is None
            assert workflow.stages(root)["chunks"] is None
            stage_chunks(root, "fixed", 450, 40)
            assert available(root, "chunks.json") and not available(root, "vectors.json")
            with Operation(root, "delete_chunks") as operation:
                clear(root, "chunks", operation)
            assert workflow.stages(root)["corpus"] and not workflow.stages(root)["chunks"]
        # A real valid semantic cache survives denied cleanup physically, but stays invisible.
        semantic_calls = []
        def semantic(request):
            import json
            semantic_calls.append(request)
            units = json.loads(json.loads(request.content)["messages"][-1]["content"])["units"]
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"end_unit_ids": [item["id"] for item in units]})}}],
                "model": "actual-offline-boundaries", "usage": {"total_tokens": 17, "cost": 0.002}})
        with httpx.Client(transport=httpx.MockTransport(semantic)) as client:
            stage_chunks(root, "semantic", 400, 40, client=client)
            cache_count = len(semantic_calls)
            durable = workflow.stages(root)["chunks"]["report"]
            assert durable["model"] == "actual-offline-boundaries" and durable["usage"]["total_tokens"] == 17 * cache_count
            assert abs(durable["cost_usd"] - 0.002 * cache_count) < 1e-12
            with patch("shutil.rmtree", side_effect=OSError("offline cleanup denied")):
                try:
                    with Operation(root, "delete_chunks") as operation:
                        clear(root, "chunks", operation)
                    raise AssertionError("Accepted failed physical cleanup")
                except OSError:
                    pass
                try:
                    stage_chunks(root, "semantic", 400, 40, client=client)
                    raise AssertionError("Revived deleted semantic cache after failed cleanup")
                except OSError:
                    pass
            assert workflow.stages(root)["chunks"] is None
            assert not available(root, "semantic-cache") and (root / "semantic-cache").exists()
            assert len(semantic_calls) == cache_count
            retry = stage_chunks(root, "semantic", 400, 40, client=client)
            assert available(root, "semantic-cache") and len(semantic_calls) > cache_count and retry["report"]["cached"] == 0
        # Failed initial progress must not retain the writer lock.
        with patch("rag.index.write_json", side_effect=OSError("offline progress denied")):
            try:
                Operation(root, "chunks").__enter__()
                raise AssertionError("Accepted failed enter")
            except OSError:
                pass
        with Operation(root, "chunks"):
            pass
        entered, release = threading.Event(), threading.Event()
        original = stage_chunks
        def paused(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)
        with patch.dict(os.environ, {"RAG_DIR": str(root), "MCP_DISABLED": "1", "RAG_EMBEDDING_API_KEY": "offline-key"}), TestClient(app) as api:
            with patch("app.rag_api.threading.Thread", side_effect=RuntimeError("offline start denied")):
                assert api.post("/api/rag/operations/chunks", json={"size": 400, "overlap": 40}).status_code == 503
            with Operation(root, "chunks"):
                pass
            with patch("app.rag_api.stage_chunks", side_effect=paused):
                response = api.post("/api/rag/operations/chunks", json={"size": 400, "overlap": 40})
                assert response.status_code == 202 and entered.wait(2)
                assert api.get("/api/rag/status").json()["operation"]["state"] == "running"
                assert api.post("/api/rag/operations/chunks", json={}).status_code == 409
                assert api.delete("/api/rag/stages/chunks").status_code == 409
                assert api.post("/api/rag/operations/ingest", json={"path": str(html)}).status_code == 422
                assert api.get("/api/rag/documents?working=true&limit=1").json()["items"][0]["document_id"]
                release.set()
            # Joining worker through writer lock release, bounded wait only in this fixture.
            import time
            for _ in range(100):
                if api.get("/api/rag/status").json()["operation"]["state"] != "running":
                    break
                time.sleep(.01)
            assert api.get("/api/rag/status").json()["stages"]["chunks"]
            assert api.delete("/api/rag/stages/chunks").status_code == 200
            assert api.get("/api/rag/status").json()["stages"]["chunks"] is None
            assert api.post("/api/rag/operations/chunks", json={"size": 200, "overlap": 200}).status_code == 422
            assert api.post("/api/rag/operations/ingest", json={"urls": ["file:///tmp/sample"]}).status_code == 409
        return "stages/fingerprint/no premature publish; save without HTTP; deletion/recompute/tombstone; async writer/API isolation"


if __name__ == "__main__":
    print(check_workflow())
