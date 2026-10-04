"""Neutral fixtures exercise full preparation over real offline HTTP and durable stages."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import threading

import httpx
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rag.documents import ingest, load_corpus, corpus_fingerprint, write_json
from rag.preparation import PreparationConfig, Preparer, _payload
from rag.index import Operation, stage_chunks, load_chunks
from rag.__main__ import main


@patch.dict(os.environ, {"OPENROUTER_API_KEY": " offline-chat-key ", "RAG_EMBEDDING_API_KEY": " offline-local-key ",
                         "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"})
def check_preparation():
    assert PreparationConfig().timeout_seconds == 600
    default = PreparationConfig()
    assert default.model == "openai/gpt-6-luna"
    assert _payload("<p>Neutral</p>", default)["reasoning"] == {"effort": "none"}
    assert default.payload_version == "preparation-reasoning-v2"
    for alternate in (replace(default, model="unverified"), replace(default, provider="compatible"), replace(default, base_url="http://neutral.test/v1", provider="compatible")):
        assert "reasoning" not in _payload("<p>Neutral</p>", alternate)
    for invalid in (True, False, 0, -1, 3601, float("nan"), float("inf"), "600"):
        try:
            PreparationConfig(timeout_seconds=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid timeout accepted: {invalid}")
    from app.rag_api import StageRequest
    for invalid in (True, 0, 3601, float("nan"), float("inf")):
        try:
            StageRequest(preparation_timeout_seconds=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid API timeout accepted")
    # Inspect the actual httpx timeout extensions without waiting or live network.
    timeouts = []
    def completion(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "title": "Neutral", "blocks": [{"text": "Neutral text", "kind": "paragraph", "section": "Topic"}]})}}]})
    real_client = httpx.Client
    def client_factory(*, timeout, trust_env):
        return real_client(transport=httpx.MockTransport(completion), timeout=timeout, trust_env=False)
    with tempfile.TemporaryDirectory() as directory, patch("rag.preparation.httpx.Client", client_factory):
        preparer = Preparer(directory, PreparationConfig(timeout_seconds=3600))
        preparer.prepare("<p>Neutral text</p>", "neutral://timeout")
        assert timeouts == [{"connect": 3600, "read": 3600, "write": 3600, "pool": 3600}]
        assert preparer.report["config"]["timeout_seconds"] == 3600
    calls, mode = [], {"value": "ok", "title": "Temperature measurements", "section": "Measured values", "cost": .005}
    blocks = [{"text": "Measured values", "kind": "heading", "section": "Measured values"},
              {"text": "Sensor A | 12.5 °C\nSensor B | 18 °C", "kind": "table_row", "section": "Measured values"}]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((payload, self.headers.get("Authorization")))
            result = {"title": mode["title"], "blocks": [{**b, "section": mode["section"]} for b in blocks]}
            if mode["value"] == "invalid":
                result["blocks"][0]["section"] = ""
            if mode["value"] == "reflected":
                result["title"] = os.environ["OPENROUTER_API_KEY"].strip()
            response = {"model": "actual-neutral-model", "choices": [{"finish_reason": "length" if mode["value"] == "length" else "stop",
                        "message": {"content": json.dumps(result)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 70, "total_tokens": 170, "cost": mode["cost"]}}
            status = 200
            if mode["value"] == "http":
                status, response = 403, {"error": {"message": self.headers.get("Authorization")}}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = '<html><head><title>Raw neutral</title><script>ignore()</script></head><body><nav>Links</nav><table><tr><td>12.5 °C</td><td>18 °C</td></tr></table></body></html>'
            html = root / "neutral.html"
            html.write_text(raw)
            inputs = [{"path": str(html), "source": "neutral://temperature"}]
            config = PreparationConfig(f"http://127.0.0.1:{server.server_port}/v1", "requested-neutral", provider="compatible")
            ingest(inputs, root)
            assert not calls  # Default remains fully programmatic.
            legacy = load_corpus(root)
            legacy["version"] = 1
            legacy["fingerprint"] = corpus_fingerprint(legacy["documents"], 1)
            write_json(root / "corpus.json", legacy)
            assert load_corpus(root)["version"] == 1
            with Operation(root, "ingest") as operation:
                report = ingest(inputs, root, preparation_strategy="llm", preparation_config=config, operation=operation)
            assert len(calls) == 1 and calls[0][1] == "Bearer offline-local-key"
            assert json.loads(calls[0][0]["messages"][1]["content"])["html"] == raw
            assert calls[0][0]["max_tokens"] == 32768
            assert report["preparation"]["usage"]["total_tokens"] == 170
            assert report["preparation"]["cost_usd"] == .005 and report["preparation"]["model"] == "actual-neutral-model"
            corpus = load_corpus(root)
            doc = corpus["documents"][0]
            assert doc["text"] == "\n\n".join(b["text"] for b in blocks)
            assert doc["title"] == mode["title"] and all(doc["text"][b["start"]:b["end"]] == b["text"] for b in doc["blocks"])
            stage_chunks(root, "structural", 100, 0)
            assert load_chunks(root)[1]["chunks"][0]["title"] == mode["title"]
            os.environ["OPENROUTER_API_KEY"] = " rotated-neutral-key "
            cached = ingest(inputs, root, preparation_strategy="llm", preparation_config=config)["preparation"]
            assert len(calls) == 1 and cached["cached"] == 1 and cached["cost_usd"] == 0 and cached["usage"] == {}
            # Rejection before HTTP reports no calls and no charged cost.
            credential_html = root / "credential.html"
            credential_html.write_text("<p>rotated-neutral-key</p>")
            try:
                ingest([{"path": str(credential_html)}], root, preparation_strategy="llm", preparation_config=config)
            except ValueError:
                pass
            preflight = json.loads((root / "ingest-report.json").read_text())["preparation"]
            assert preflight["calls"] == 0 and preflight["cost_usd"] == 0
            for kind in ("invalid", "length", "http", "reflected"):
                previous = (root / "corpus.json").read_bytes()
                mode["value"] = kind
                different = replace(config, model=kind)
                try:
                    ingest(inputs, root, preparation_strategy="llm", preparation_config=different)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Invalid preparation accepted")
                assert (root / "corpus.json").read_bytes() == previous
                failure = json.loads((root / "ingest-report.json").read_text())
                assert failure["preparation"]["calls"] == 1
                if kind in {"invalid", "length"}:
                    assert failure["preparation"]["usage"]["total_tokens"] == 170
                    assert failure["preparation"]["cost_usd"] == .005
                else:
                    assert failure["preparation"]["cost_usd"] is None
                error = failure["errors"][0]["error"]
                assert "key" not in error and len(error) < 600
            assert len(list((root / "preparation-cache").glob("*.json"))) == 1
            for file in (root / "preparation-cache").glob("*.json"):
                assert "Authorization" not in file.read_text() and "neutral-key" not in file.read_text()
            before = len(calls)
            try:
                ingest(inputs, root, preparation_strategy="llm", preparation_config=replace(config, max_html_characters=1))
            except ValueError:
                pass
            assert len(calls) == before
            mode.update(value="ok", title="Updated title", section="Updated section", cost=None)
            report = ingest(inputs, root, preparation_strategy="llm", preparation_config=replace(config, model="metadata"))
            assert report["preparation"]["cost_usd"] is None
            updated = load_corpus(root)
            assert updated["documents"][0]["text"] == doc["text"] and updated["fingerprint"] != corpus["fingerprint"]
            try:
                load_chunks(root)
            except ValueError:
                pass
            else:
                raise AssertionError("Metadata changes retained old chunks")
            local = replace(config, provider="compatible", model="another-neutral")
            ingest(inputs, root, preparation_strategy="llm", preparation_config=local)
            assert calls[-1][1] == "Bearer offline-local-key"
            manifest = root / "manifest.json"
            write_json(manifest, inputs)
            with contextlib.redirect_stdout(io.StringIO()):
                assert main(["--root", str(root), "--compatible-base-url", local.base_url, "ingest", "--manifest", str(manifest), "--preparation-strategy", "llm",
                             "--preparation-model", local.model,
                             "--preparation-provider", "compatible"]) == 0
            assert len(calls) == before + 2  # metadata call + compatible server; CLI is a cache hit.
            with contextlib.redirect_stdout(io.StringIO()):
                assert main(["--root", str(root), "--compatible-base-url", local.base_url, "ingest", "--manifest", str(manifest), "--preparation-strategy", "llm",
                             "--preparation-model", local.model,
                             "--preparation-provider", "compatible", "--preparation-timeout", "1200"]) == 0
            assert load_corpus(root)["preparation_config"]["timeout_seconds"] == 1200
            assert len(calls) == before + 3
            # Metadata bounds include sections, even when text itself would fit.
            previous = (root / "corpus.json").read_bytes()
            mode["section"] = "s" * 300
            try:
                ingest(inputs, root, preparation_strategy="llm", preparation_config=replace(config, max_output_characters=200))
            except ValueError:
                pass
            else:
                raise AssertionError("Unbounded metadata accepted")
            assert (root / "corpus.json").read_bytes() == previous
            mode["section"] = "New section"
            from app.rag_api import StageRequest, start
            from rag.index import Index
            body = StageRequest(preparation_strategy="llm", preparation_model=config.model, preparation_provider="compatible")
            assert body.preparation_strategy == "llm" and body.strategy == "fixed"
            # Dispatch the actual API worker with an operator-owned neutral manifest.
            write_json(root / "inputs.json", inputs)
            with patch("app.rag_api.Index", lambda: Index(root)), patch("app.model_settings.settings", return_value={"compatible_base_url": config.base_url}):
                before = len(calls)
                acknowledgement = start("ingest", StageRequest(use_manifest=True, preparation_strategy="llm",
                    preparation_model="api-neutral", preparation_provider="compatible",
                    preparation_timeout_seconds=1700))
                assert acknowledgement["state"] == "running"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = Index(root).status()
                    if status["operation"]["state"] != "running":
                        break
                    time.sleep(.01)
                assert status["operation"]["state"] == "complete", status
                assert len(calls) == before + 1 and calls[-1][1] == "Bearer offline-local-key"
                assert load_corpus(root)["preparation_strategy"] == "llm"
                assert load_corpus(root)["preparation_config"]["timeout_seconds"] == 1700
                assert status["operation"]["preparation_report"]["config"]["timeout_seconds"] == 1700
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    check_preparation()
