"""Compact real HTTP/CLI/SQLite/inspector boundaries, neutral temporary inputs."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.chunks import chunk_documents
from rag.documents import ingest, normalize_html, write_json
from rag.embeddings import EmbeddingConfig, Embeddings
from rag.index import Index, Operation, build_index


@patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": ""})
def check_rag():
    calls, authorization, mode = [], [], {"value": "ok", "key": None}
    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            assert self.path == "/v1/embeddings"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            authorization.append(self.headers.get("Authorization"))
            if mode["key"] is not None and self.headers.get("Authorization") != f'Bearer {mode["key"]}':
                self.send_response(401); self.end_headers()
                self.wfile.write(str(self.headers.get("Authorization")).encode())
                return
            rows = []
            for i, text in enumerate(body["input"]):
                raw = hashlib.sha256(text.encode()).digest()
                vector = [raw[0] + 1, raw[1] + 1, raw[2] + 1]
                if mode["value"] == "zero":
                    vector = [0, 0, 0]
                elif mode["value"] == "dimension" or (mode["value"] == "mixed" and i == 1):
                    vector.append(1)
                rows.append({"index": i, "embedding": vector})
            if mode["value"] == "indices":
                rows[-1]["index"] = -1
            elif mode["value"] == "count":
                rows.pop()
            elif mode["value"] == "nan":
                rows[-1]["embedding"][0] = float("nan")
            response = json.dumps({"model": body["model"], "data": list(reversed(rows))}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(response)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="rag-check-") as temporary:
            root = Path(temporary)
            # Legacy malformed adjacent paragraphs, wide article cell, centered
            # headings, navigation, two-cell table/list and oversized paragraph.
            html = '<html><head><meta charset="windows-1251"><title>Нейтральный архив</title><style>secret-style</style></head><body><table><tr><td width="1002"><table><tr><td width="200"><a>one</a><a>two</a><a>three</a></td><td width="805"><p align="center"><font><strong>Раздел А</strong></font></p><p>Описание нейтрального образца.<p>' + 'Длинное описание свойств. ' * 100 + '</p><ul><li>Первый пункт<li>Второй пункт</ul><table><tr><td><p>Параметр</p></td><td><p>Значение</p></td></tr><tr><td>масса</td><td>42</td></tr></table><h2>Раздел Б</h2><p>Другой факт.</p></td></tr></table></td></tr></table><script>evil()</script><footer>copyright</footer></body></html>'
            source = root / "neutral.html"; source.write_bytes(html.encode("cp1251"))
            doc = normalize_html(source.read_bytes(), "https://example.test/article")
            assert "one" not in doc["text"] and "evil" not in doc["text"] and "secret-style" not in doc["text"]
            assert doc["title"] == "Нейтральный архив"
            assert "Параметр | Значение" in doc["text"] and "масса | 42" in doc["text"]
            assert any(b["kind"] == "heading" and b["section"] == "Раздел А" for b in doc["blocks"])
            for strategy in ("fixed", "structural"):
                chunks = chunk_documents([doc], strategy)
                assert chunks == chunk_documents([doc], strategy)
                assert all(c["text"] == doc["text"][c["start"]:c["end"]] and len(c["text"]) <= 1200 for c in chunks)
                assert len({c["chunk_id"] for c in chunks}) == len(chunks)
                if strategy == "fixed":
                    assert chunks[0]["text"][-180:] == chunks[1]["text"][:180]
                else:
                    assert not any("Раздел А" in c["section"] and "Раздел Б" in c["section"] for c in chunks)
                    assert any("Первый пункт" in c["text"] and "Второй пункт" in c["text"] for c in chunks)
            wrapped = normalize_html(b'<article><font><p align="center"><strong>Stage one</strong></p><span><table><tr><td>00:01</td><td><p>Operation alpha</p></td></tr></table></span><p align="center"><strong>Alloy section</strong></p><font><table><tr><td>M1</td><td>4.4</td><td>42</td></tr></table></font></font></article>', 'https://example.test/wrapped')
            rows = [b for b in wrapped["blocks"] if b["kind"] == "table_row"]
            assert [(b["text"], b["section"]) for b in rows] == [('00:01 | Operation alpha', 'Stage one'), ('M1 | 4.4 | 42', 'Alloy section')]
            assert [b["text"] for b in wrapped["blocks"] if b["kind"] == "heading"] == ['Stage one', 'Alloy section']
            inputs = [{"path": str(source), "source": doc["source"]}]
            report = ingest(inputs, root)
            assert report["words"] == doc["words"] and report["documents"] == 1
            corpus_bytes = (root / "corpus.json").read_bytes()
            try:
                ingest(inputs + [{"path": str(root / "missing.html")}], root)
                raise AssertionError("Partial corpus published")
            except ValueError:
                pass
            assert (root / "corpus.json").read_bytes() == corpus_bytes
            assert len(json.loads((root / "ingest-report.json").read_text())["errors"]) == 1
            try:
                normalize_html(b'<frameset><frame src="body.html"></frameset>', 'https://example.test/shell')
                raise AssertionError("Frameset accepted as article")
            except ValueError:
                pass
            config = EmbeddingConfig(f"http://127.0.0.1:{server.server_port}/v1", "offline-test")
            # CLI's actual HTTP traffic and durable index survive process exit.
            command = [sys.executable, "-m", "rag", "--root", str(root), "index", "--base-url", config.base_url, "--model", config.model, "--batch-size", "2"]
            fake_keys = ("offline-auth-first", "offline-auth-rotated", "offline-auth-rejected")
            mode["key"] = fake_keys[0]
            with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": fake_keys[0]}):
                result = subprocess.run(command, capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
            assert result.returncode == 0, result.stderr
            assert authorization and set(authorization) == {f"Bearer {fake_keys[0]}"}
            metadata = json.loads(result.stdout); index = Index(root)
            assert index.status()["state"] == "ready" and metadata["dimension"] == 3
            initial_calls = len(calls)
            mode["key"] = fake_keys[1]
            with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": fake_keys[1]}):
                cached = build_index(root, config)
            assert len(calls) == initial_calls and cached["computed"] == 0 and cached["cached"] == cached["chunks"]
            assert cached["embedding_fingerprint"] == metadata["embedding_fingerprint"] == config.fingerprint()
            with httpx.Client(trust_env=False) as client:
                embedder = Embeddings(config, client)
                with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": fake_keys[0]}):
                    mode["key"] = fake_keys[0]
                    embedder.embed(["runtime first"])
                with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": fake_keys[1]}):
                    mode["key"] = fake_keys[1]
                    embedder.embed(["runtime rotated"])
            assert authorization[-2:] == [f"Bearer {key}" for key in fake_keys[:2]]
            old_bytes = (root / "index.sqlite").read_bytes()
            with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": fake_keys[2]}):
                denied = subprocess.run(command + ["--revision", "unauthorized"], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
            assert denied.returncode == 1 and "401" in denied.stderr
            assert (root / "index.sqlite").read_bytes() == old_bytes
            assert index.status()["state"] == "error" and "401" in index.status()["operation"]["error"]
            for key in fake_keys:
                assert key not in result.stdout + result.stderr + denied.stdout + denied.stderr + repr(config) + repr(embedder) + json.dumps(index.status())
                assert all(key.encode() not in path.read_bytes() for path in root.rglob("*") if path.is_file())
            # Errors must remain safe even when a transport reflects the header.
            def failed_transport(request):
                raise httpx.ConnectError(request.headers["Authorization"], request=request)
            with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": fake_keys[2]}), httpx.Client(transport=httpx.MockTransport(failed_transport)) as client:
                try:
                    Embeddings(config, client).embed(["transport failure"])
                    raise AssertionError("Transport failure accepted")
                except ValueError as error:
                    assert str(error) == "Embedding HTTP request failed"
            for invalid_key in ("offline-auth-\u2603", "offline-auth\r\nreflected"):
                with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": invalid_key}):
                    try:
                        Embeddings(config).embed(["invalid header"])
                        raise AssertionError("Invalid header accepted")
                    except ValueError as error:
                        assert str(error) == "Embedding HTTP request failed"
            mode["key"] = None
            Embeddings(config).embed(["empty key"])
            with patch.dict(os.environ):
                os.environ.pop("RAG_EMBEDDING_API_KEY", None)
                Embeddings(config).embed(["missing key"])
            assert authorization[-2:] == [None, None]
            update = Operation.update
            def fail_final_progress(operation, **values):
                if values.get("state") == "ready":
                    raise OSError("injected final progress fsync failure")
                return update(operation, **values)
            with patch.object(Operation, "update", fail_final_progress):
                published = build_index(root, config)
            assert published["index_id"] != cached["index_id"] and published["warning"]
            status = index.status()
            assert status["state"] == "ready" and status["operation"]["state"] == "ready"
            assert status["operation"]["warning"] and status["index"]["index_id"] == published["index_id"]
            cached = published
            first = index.chunks(doc["document_id"])[0]
            assert "vector" not in first
            saved = index.chunk(first["chunk_id"], True)
            assert abs(sum(v*v for v in saved["vector"]) - 1) < 1e-9
            hits = index.search(saved["text"], 2)
            assert hits[0]["chunk_id"] == saved["chunk_id"] and abs(hits[0]["score"] - 1) < 1e-9
            try:
                index.search("question", config=replace(config, revision="different"))
                raise AssertionError("Fingerprint mismatch accepted")
            except ValueError:
                pass
            for failure in ("zero", "indices", "count", "nan", "mixed"):
                mode["value"] = failure
                try:
                    Embeddings(config).embed(["alpha", "beta"])
                    raise AssertionError(f"Invalid {failure} response accepted")
                except ValueError:
                    pass
            mode["value"] = "ok"
            compare = subprocess.run([sys.executable, "-m", "rag", "--root", str(root), "compare", "--base-url", config.base_url, "--model", config.model], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
            assert compare.returncode == 0, compare.stderr
            comparison = json.loads((root / "comparison.json").read_text())
            assert set(comparison["strategies"]) == {"fixed", "structural"}
            assert comparison["strategies"]["fixed"]["max_characters"] == 1200
            assert index.metadata()["index_id"] == cached["index_id"]
            assert Index(root / "comparisons" / "fixed").metadata()["strategy"] == "fixed"
            # Mismatched fixed expected dimension, preserving old bytes on failure.
            old_bytes = (root / "index.sqlite").read_bytes()
            mode["value"] = "dimension"
            try:
                build_index(root, replace(config, revision="failed", dimensions=3))
                raise AssertionError("Dimension mismatch accepted")
            except ValueError:
                pass
            assert (root / "index.sqlite").read_bytes() == old_bytes
            assert index.status()["state"] == "error" and index.status()["index"]["index_id"] == cached["index_id"]
            assert index.status()["index"]["state"] == "ready"
            mode["value"] = "ok"
            refreshed = build_index(root, replace(config, revision="changed"))
            assert refreshed["computed"] > 0  # full config fingerprint cache boundary
            with Operation(root, "index"):
                assert index.status()["state"] == "running"
                try:
                    build_index(root, config)
                    raise AssertionError("Concurrent rebuild accepted")
                except ValueError as error:
                    assert "Another" in str(error)
            write_json(root / "progress.json", {"state": "running", "pid": os.getpid()})
            assert index.status()["state"] == "interrupted" and index.status()["index"]
            # Read-only routes use operator root, no chat/API key or LLM.
            from checks import _stub
            _stub.install_offline()
            from app.main import app
            from fastapi.testclient import TestClient
            write_json(root / "progress.json", {"state": "ready"})
            with patch("rag.index.storage_root", lambda: root), patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), TestClient(app) as api:
                status = api.get("/api/rag/status").json()
                assert all(key not in json.dumps(status) for key in fake_keys)
                assert status["index"]["rows"]["chunks"] == refreshed["chunks"]
                documents = api.get("/api/rag/documents?limit=1").json()["items"]
                assert len(documents) == 1 and "text" not in documents[0]
                assert api.get("/api/rag/documents?limit=101").status_code == 422
                route = f'/api/rag/chunks/{saved["chunk_id"]}'
                assert "vector" not in api.get(route).json()
                assert len(api.get(route + "?vector=true").json()["vector"]) == 3
                assert api.get("/api/rag/chunks/missing").status_code == 404
            # New corpus makes old index stale and retrieval refuses it.
            source.write_bytes(html.replace("Другой факт.", "Обновлённый факт.").encode("cp1251"))
            ingest(inputs, root)
            assert index.status()["state"] == "stale"
            try:
                index.search("question")
                raise AssertionError("Stale index used")
            except ValueError:
                pass
            return "CP1251/layout/chunks; HTTP CLI/auth rotation/cache/cosine; atomic failure/lock/status; bounded API"
    finally:
        server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    print(check_rag())
