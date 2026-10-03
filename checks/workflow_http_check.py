"""Actual offline HTTP and standalone CLI exercise semantic → embed → save."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from rag.documents import ingest
from rag.index import Index


def check_workflow_http():
    calls, mode = [], {"invalid": False}
    class Server(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path == "/v1/chat/completions":
                assert self.headers.get("Authorization") == "Bearer offline-chunk-key"
                units = json.loads(body["messages"][-1]["content"])["units"]
                ids = [999] if mode["invalid"] else [item["id"] for item in units]
                result = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"end_unit_ids": ids})}}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            elif self.path == "/v1/embeddings":
                assert self.headers.get("Authorization") == "Bearer offline-embed-key"
                result = {"model": body["model"], "data": [{"index": i, "embedding": [1, i + 1, 2]} for i in range(len(body["input"]))]}
            else:
                raise AssertionError("Unexpected outbound path")
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps(result).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="workflow-http-") as temporary, patch.dict(os.environ, {
                "RAG_CHUNKING_API_KEY": "offline-chunk-key", "RAG_EMBEDDING_API_KEY": "offline-embed-key"}):
            root = Path(temporary)
            source = root / "neutral.html"
            source.write_text('<article><h1>Neutral</h1><p>' + 'Neutral sample text. ' * 100 + '</p><p>Next neutral topic.</p></article>', encoding="utf-8")
            ingest([{"path": str(source), "source": "https://example.test/neutral"}], root)
            base = f"http://127.0.0.1:{server.server_port}/v1"
            def cli(*args, success=True):
                result = subprocess.run([sys.executable, "-m", "rag", "--root", str(root), *args], capture_output=True, text=True,
                                        cwd=Path(__file__).resolve().parent.parent)
                assert result.returncode == (0 if success else 1), result.stderr
                assert "offline-chunk-key" not in result.stdout + result.stderr and "offline-embed-key" not in result.stdout + result.stderr
                return json.loads(result.stdout) if success else result.stderr
            split = ("chunks", "--strategy", "semantic", "--size", "400", "--overlap", "40", "--semantic-base-url", base)
            first = cli(*split)
            assert first["report"]["calls"] > 0 and first["report"]["usage"]["prompt_tokens"] > 0
            assert not (root / "index.sqlite").exists()
            count = len(calls)
            cached = cli(*split)
            assert len(calls) == count and cached["report"]["calls"] == 0 and cached["report"]["usage"] == {}
            assert cached["fingerprint"] == first["fingerprint"]
            cli("embed", "--base-url", base, "--model", "offline-test")
            assert not (root / "index.sqlite").exists()
            count = len(calls)
            cli("save")
            assert len(calls) == count and Index(root).metadata()["strategy"] == "semantic"
            old_index = (root / "index.sqlite").read_bytes()
            old_chunks = (root / "chunks.json").read_bytes()
            mode["invalid"] = True
            assert "Semantic" in cli(*split, "--semantic-model", "offline-invalid", success=False)
            assert (root / "index.sqlite").read_bytes() == old_index and (root / "chunks.json").read_bytes() == old_chunks
            mode["invalid"] = False
            cli("clear", "chunks")
            assert (root / "corpus.json").exists() and not (root / "semantic-cache").exists()
            count = len(calls)
            cli(*split)
            assert len(calls) > count
            for path in root.rglob("*"):
                if path.is_file():
                    assert b"offline-chunk-key" not in path.read_bytes() and b"offline-embed-key" not in path.read_bytes()
        return "actual offline semantic/embedding HTTP + CLI; segmentation cache identity; save no model; invalid boundary preserves index; clear recomputes"
    finally:
        server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    print(check_workflow_http())
