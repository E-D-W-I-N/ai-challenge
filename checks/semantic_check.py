"""Neutral offline generation: real HTTP boundary, exact slices and validated cache."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rag.documents import digest
from rag.semantic import SemanticConfig, _boundaries, _http_error, _payload, _normalize_boundary_ids, semantic_chunks


def document(text):
    blocks, start = [], 0
    for paragraph in text.splitlines(keepends=True):
        blocks.append({"start": start, "end": start + len(paragraph), "section": "neutral"})
        start += len(paragraph)
    return {"document_id": digest(text), "text": text, "content_hash": digest(text),
            "source": "neutral.html", "title": "Neutral", "blocks": blocks}


@patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "RAG_EMBEDDING_API_KEY": ""})
def check_semantic():
    units = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 60), (60, 70)]
    default = SemanticConfig()
    assert default.model == "openai/gpt-6-luna"
    short_payload = _payload("x", [(0, 1)], default, 10)
    assert short_payload["reasoning"] == {"effort": "none"} and short_payload["max_tokens"] == 9216
    full_payload = _payload("x" * 256, [(i, i + 1) for i in range(256)], default, 10)
    assert full_payload["max_tokens"] == 11328
    for alternative in (replace(default, model="unverified-model"), replace(default, provider="compatible"),
                        replace(default, base_url="http://neutral.test/v1", provider="compatible")):
        assert "reasoning" not in _payload("x", [(0, 1)], alternative, 10)
    # The requested end at 30 survives even though greedy whole-document
    # packing would choose 40. Subsequent semantic group is capped separately.
    spans = _boundaries({"end_unit_ids": [3, 7]}, units, 20)
    assert spans == [(0, 20), (20, 30), (30, 50), (50, 70)]
    for repaired in ([3, 3, 7], [7, 3], [7, 3, 3], [3]):
        assert _boundaries({"end_unit_ids": repaired}, units, 20) == spans
    assert _normalize_boundary_ids({"end_unit_ids": [3, 3, 7]}, 7) == {
        "model_end_unit_ids": [3, 3, 7], "normalized_end_unit_ids": [3, 7], "reordered": False,
        "duplicates_removed": 1, "terminal_added": False}
    for ids in ([3, 8], [0, 7], [True, 7], [1.0, 7], ["3", 7], [], None):
        try:
            _boundaries({"end_unit_ids": ids}, units, 20)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Invalid boundaries accepted: {ids}")
    requests, mode, auth = [], {"value": "ok", "cost": 0.00125}, []
    def server(request):
        payload = json.loads(request.content)
        requests.append(payload)
        auth.append(request.headers.get("Authorization"))
        if mode["value"] == "http":
            return httpx.Response(401, json={"error": request.headers.get("Authorization")})
        if mode["value"] == "transport":
            raise httpx.ConnectError("credential-injected-secret", request=request)
        units = json.loads(payload["messages"][1]["content"])["units"]
        ids = [u["id"] for u in units]
        if mode["value"] == "unknown":
            ids.append(len(ids) + 1)
        if mode["value"] == "duplicate":
            ids = [1, 1, len(ids)]
        if mode["value"] == "missing":
            ids = ids[:-1]
        if mode["value"] == "unordered":
            ids = list(reversed(ids))
        if mode["value"] == "empty":
            ids = []
        if mode["value"] == "wrong-type":
            ids = ["1"]
        if mode["value"] == "zero":
            ids = [0]
        if mode["value"] == "oversize":
            ids = [len(ids)]
        content = json.dumps({"end_unit_ids": ids})
        if mode["value"] == "null-content":
            content = None
        if mode["value"] == "malformed":
            content = "```json\n" + content
        usage = {"prompt_tokens": 51, "completion_tokens": 9, "total_tokens": 60}
        if mode["value"] == "usage":
            usage["prompt_tokens"] = True
        if mode["cost"] != "missing":
            usage["cost"] = mode["cost"]
        body = {"model": "actual-neutral-model", "choices": [{"finish_reason":
            "length" if mode["value"] == "truncated" else "stop", "message": {"content": content}}], "usage": usage}
        if mode["value"] == "reflected":
            body["provider_metadata"] = {"headers": {"Authorization": request.headers.get("Authorization")}}
        if mode["value"] == "escaped-reflected":
            body["choices"][0]["message"]["content"] = json.dumps({"end_unit_ids": ids,
                "unexpected": "credential-injected-secret"}).replace("credential", "\\u0063redential")
        return httpx.Response(200, json=body)
    with tempfile.TemporaryDirectory() as temporary, httpx.Client(transport=httpx.MockTransport(server)) as client:
        root = Path(temporary)
        config = SemanticConfig(base_url="http://neutral.test/v1")
        text = "Alpha topic.\n\nBeta topic has more detail.\n" + "Long neutral paragraph. " * 1200
        doc = document(text)
        chunks, report = semantic_chunks([doc], config, size=90, overlap=15, root=root, client=client)
        assert report["calls"] > 1 and report["computed"] == 1
        assert report["model"] == "actual-neutral-model"
        assert math.isclose(report["cost_usd"], 0.00125 * report["calls"])
        assert report["usage"]["prompt_tokens"] == 51 * report["calls"]
        assert report["usage"]["completion_tokens"] == 9 * report["calls"]
        assert auth == [None] * report["calls"]
        assert all(c["text"] == text[c["start"]:c["end"]] and len(c["text"]) <= 90 for c in chunks)
        assert chunks[0]["start"] == 0 and chunks[-1]["end"] == len(text)
        assert len({c["chunk_id"] for c in chunks}) == len(chunks)
        assert all(a["end"] - b["start"] == min(15, a["end"]) for a, b in zip(chunks, chunks[1:]))
        for request in requests:
            units = json.loads(request["messages"][1]["content"])["units"]
            assert sum(len(u["text"]) for u in units) <= 12000 and len(units) <= 256
            assert request["max_tokens"] == 8192 + max(1024, 64 + len(units) * 12)
            assert request["reasoning"] == {"effort": "none"}
        before = len(requests)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "rotated-neutral-key"}):
            same, cached = semantic_chunks([doc], config, 90, 15, root=root, client=client)
        assert same == chunks and len(requests) == before and cached["calls"] == 0
        assert cached["usage"] == {} and cached["cached"] == 1
        assert cached["cost_usd"] == 0 and cached["model"] == "actual-neutral-model"
        cache_path = root / cached["trace_files"][0]
        saved = json.loads(cache_path.read_text())
        assert saved["identity"]["config"]["payload_version"] == "boundary-normalization-v3"
        saved["spans"][0][1] += 1
        cache_path.write_text(json.dumps(saved))
        rebuilt, fresh = semantic_chunks([doc], config, 90, 15, root=root, client=client)
        assert rebuilt == chunks and fresh["calls"] > 0
        for changed in [replace(config, model="other-neutral-model"), replace(config, base_url="http://another.test/v1", provider="compatible")]:
            assert semantic_chunks([doc], changed, 90, 15, root=root, client=client)[1]["calls"] > 0
        assert semantic_chunks([document(text + "Changed neutral ending.")], config, 90, 15, root=root, client=client)[1]["calls"] > 0
        assert semantic_chunks([doc], config, 100, 15, root=root, client=client)[1]["calls"] > 0
        # Complete zero costs are valid; unavailable or malformed charges are never zero.
        for cost in (0, "missing", True, -1, "0.1", 10 ** 400):
            mode["cost"] = cost
            with tempfile.TemporaryDirectory() as accounting:
                _, billed = semantic_chunks([doc], config, 90, 15, root=accounting, client=client)
                assert billed["cost_usd"] == (0 if type(cost) is int and cost == 0 else None)
        partial_calls = []
        def partial(request):
            partial_calls.append(request)
            mode["cost"] = 0.00125 if len(partial_calls) == 1 else "missing"
            return server(request)
        with tempfile.TemporaryDirectory() as accounting, httpx.Client(transport=httpx.MockTransport(partial)) as partial_client:
            _, billed = semantic_chunks([doc], config, 90, 15, root=accounting, client=partial_client)
            assert billed["calls"] > 1 and billed["cost_usd"] is None
        mode["cost"] = 0.00125
        short = document("First short topic.\nSecond short topic.\nThird short topic.\n")
        # A valid oversized semantic group keeps its end and every source byte;
        # deterministic extra boundaries require no extra paid request.
        mode["value"] = "oversize"
        with tempfile.TemporaryDirectory() as split_root:
            before = len(requests)
            bounded, normalized = semantic_chunks([short], config, 35, 5, root=split_root, client=client)
            assert len(requests) == before + 1 and normalized["size_splits"] == len(bounded) - 1 > 0
            assert bounded[0]["start"] == 0 and bounded[-1]["end"] == len(short["text"])
            assert all(c["text"] == short["text"][c["start"]:c["end"]] and len(c["text"]) <= 35 for c in bounded)
            assert all(a["end"] - b["start"] == 5 for a, b in zip(bounded, bounded[1:]))
            same, cached = semantic_chunks([short], config, 35, 5, root=split_root, client=client)
            assert same == bounded and cached["size_splits"] == normalized["size_splits"]
            assert len(requests) == before + 1 and cached["calls"] == 0 and cached["cost_usd"] == 0
        # Real default-Luna HTTP payload with benign model contract variations.
        for repaired, expected in (("missing", {"normalized_rounds": 1, "reordered_rounds": 0, "duplicate_ids_removed": 0, "terminal_cuts_added": 1}),
                                   ("duplicate", {"normalized_rounds": 1, "reordered_rounds": 0, "duplicate_ids_removed": 1, "terminal_cuts_added": 0}),
                                   ("unordered", {"normalized_rounds": 1, "reordered_rounds": 1, "duplicate_ids_removed": 0, "terminal_cuts_added": 0})):
            mode["value"] = repaired
            with tempfile.TemporaryDirectory() as repaired_root:
                before = len(requests)
                bounded, repaired_report = semantic_chunks([short], default, 35, 5, root=repaired_root, client=client)
                assert len(requests) == before + 1 and repaired_report["calls"] == 1
                actual_request = requests[-1]
                supplied = json.loads(actual_request["messages"][-1]["content"])
                assert actual_request["model"] == "openai/gpt-6-luna" and actual_request["reasoning"] == {"effort": "none"}
                assert supplied["total_units"] == supplied["last_unit_id"] == len(supplied["units"])
                assert repaired_report["boundary_normalization"] == expected and repaired_report["size_splits"] >= 0
                assert repaired_report["usage"]["total_tokens"] == 60 and repaired_report["cost_usd"] == 0.00125
                assert bounded[0]["start"] == 0 and bounded[-1]["end"] == len(short["text"])
                assert all(c["text"] == short["text"][c["start"]:c["end"]] and len(c["text"]) <= 35 for c in bounded)
                assert all(a["end"] - b["start"] == 5 for a, b in zip(bounded, bounded[1:]))
                record_path = Path(repaired_root) / repaired_report["trace_files"][-1]
                record = json.loads(record_path.read_text())
                round_ = record["rounds"][0]
                metadata = round_["boundary_normalization"]
                assert json.loads(round_["response"]["choices"][0]["message"]["content"])["end_unit_ids"] == metadata["model_end_unit_ids"]
                assert set(metadata["model_end_unit_ids"]) <= set(metadata["normalized_end_unit_ids"])
                ends = {c["end"] for c in bounded}
                assert all(supplied["units"][i - 1]["id"] == i for i in metadata["normalized_end_unit_ids"])
                offset = 0
                unit_ends = {}
                for u in supplied["units"]:
                    offset += len(u["text"]); unit_ends[u["id"]] = offset
                assert all(unit_ends[i] in ends for i in metadata["model_end_unit_ids"])
                raw_trace = Path(repaired_root) / repaired_report["trace_files"][0]
                original_trace = raw_trace.read_bytes()
                same, cached = semantic_chunks([short], default, 35, 5, root=repaired_root, client=client)
                assert same == bounded and len(requests) == before + 1 and cached["calls"] == 0
                assert cached["usage"] == {} and cached["cost_usd"] == 0
                assert cached["boundary_normalization"] == expected and cached["size_splits"] == repaired_report["size_splits"]
                assert raw_trace.read_bytes() == original_trace
                # Cached corrections are recomputed from actual response, never trusted.
                metadata["normalized_end_unit_ids"] = [999]
                record_path.write_text(json.dumps(record))
                recomputed, fresh = semantic_chunks([short], default, 35, 5, root=repaired_root, client=client)
                assert recomputed == bounded and len(requests) == before + 2 and fresh["calls"] == 1
        # Invalid IDs cannot replace already durable chunks, vectors, or published SQLite.
        from rag.documents import ingest
        from rag.embeddings import EmbeddingConfig
        from rag.index import stage_chunks, stage_embeddings, save_index
        def embedding(request):
            body = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1, 2, 3]} for i in range(len(body["input"]))]})
        with tempfile.TemporaryDirectory() as durable, httpx.Client(transport=httpx.MockTransport(embedding)) as embedding_client:
            durable = Path(durable)
            source = durable / "neutral.html"; source.write_text("<article><h1>Neutral</h1><p>" + "Neutral source words. " * 15 + "</p></article>")
            ingest([{"path": str(source), "source": "https://example.test/neutral"}], durable)
            stage_chunks(durable, "fixed", 120, 10)
            stage_embeddings(durable, EmbeddingConfig("http://neutral.test/v1", "offline-vector"), client=embedding_client)
            save_index(durable)
            previous = {name: (durable / name).read_bytes() for name in ("corpus.json", "chunks.json", "vectors.json", "index.sqlite")}
            mode["value"] = "unknown"; before = len(requests)
            try:
                stage_chunks(durable, "semantic", 120, 10, semantic_config=default, client=client)
            except ValueError as error:
                assert "window 1" in str(error) and "must be within" in str(error)
            else:
                raise AssertionError("Accepted out-of-range boundary IDs")
            assert len(requests) == before + 1
            assert all((durable / name).read_bytes() == content for name, content in previous.items())
            progress = json.loads((durable / "progress.json").read_text())
            assert progress["state"] == "error" and progress["semantic_report"]["calls"] == 1
            assert progress["semantic_report"]["usage"]["total_tokens"] == 60 and progress["semantic_report"]["cost_usd"] == 0.00125
        expected_errors = {"unknown": "must be within", "zero": "must be within", "empty": "must not be empty", "wrong-type": "must be integers", "malformed": "content is invalid JSON", "truncated": "token limit", "usage": "usage accounting",
                           "null-content": "must contain JSON text"}
        for bad in ("unknown", "zero", "empty", "wrong-type", "malformed", "truncated", "usage", "null-content", "http", "transport", "reflected", "escaped-reflected"):
            mode["value"] = bad
            with tempfile.TemporaryDirectory() as rejected, patch.dict(os.environ, {"OPENROUTER_API_KEY": "  credential-injected-secret  "}):
                from types import SimpleNamespace
                captured = {}
                operation = SimpleNamespace(update=lambda **fields: captured.update(fields))
                before = len(requests)
                try:
                    semantic_chunks([short], config, 35, 5, root=rejected, client=client, operation=operation)
                except ValueError as error:
                    assert "credential-injected-secret" not in str(error)
                    if bad in expected_errors:
                        assert expected_errors[bad] in str(error), str(error)
                    if bad in {"unknown", "zero", "empty", "wrong-type"}:
                        assert "window 1" in str(error) and "expected IDs 1.." in str(error)
                else:
                    raise AssertionError(bad)
                assert len(requests) == before + 1  # Every error ends after one paid attempt.
                if bad in {"truncated", "unknown", "zero", "empty", "wrong-type"}:
                    actual = captured["semantic_report"]
                    assert actual["calls"] == 1 and actual["model"] == "actual-neutral-model"
                    assert actual["usage"] == {"prompt_tokens": 51, "completion_tokens": 9, "total_tokens": 60}
                    assert actual["cost_usd"] == 0.00125
                artifacts = list((Path(rejected) / "semantic-cache").glob("*.json"))
                assert not [p for p in artifacts if not p.name.startswith("round-")]
                assert all("credential-injected-secret" not in p.read_text() for p in artifacts)
                if bad in {"reflected", "escaped-reflected"}:
                    assert not artifacts
        mode["value"] = "ok"
        with tempfile.TemporaryDirectory() as rejected, patch.dict(os.environ, {"OPENROUTER_API_KEY": "  credential-injected-secret  "}):
            before = len(requests)
            try:
                semantic_chunks([document("Text credential-injected-secret end.")], config, 100, 0, root=rejected, client=client)
            except ValueError as error:
                assert "credential-injected-secret" not in str(error)
            else:
                raise AssertionError("request credential guard")
            assert len(requests) == before and not list((Path(rejected) / "semantic-cache").glob("*.json"))
        with tempfile.TemporaryDirectory() as authorized, patch.dict(os.environ, {"OPENROUTER_API_KEY": "fallback-neutral"}):
            semantic_chunks([short], config, 35, 5, root=authorized, client=client)
            assert auth[-1] == "Bearer fallback-neutral"
        with tempfile.TemporaryDirectory() as authorized, patch.dict(os.environ, {"OPENROUTER_API_KEY": "  fallback-neutral  "}):
            semantic_chunks([short], config, 35, 5, root=authorized, client=client)
            assert auth[-1] == "Bearer fallback-neutral"
        with tempfile.TemporaryDirectory() as unauthenticated, patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "  ", "RAG_EMBEDDING_API_KEY": " "}):
            semantic_chunks([short], config, 35, 5, root=unauthenticated, client=client)
            assert auth[-1] is None
        local_config = replace(config, provider="compatible")
        with tempfile.TemporaryDirectory() as local, patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "chat-neutral", "RAG_EMBEDDING_API_KEY": "  local-neutral  "}):
            local_chunks, _ = semantic_chunks([short], local_config, 35, 5, root=local, client=client)
            assert auth[-1] == "Bearer local-neutral"
            before = len(requests)
            with patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": "rotated-local-neutral"}):
                repeated, cached = semantic_chunks([short], local_config, 35, 5, root=local, client=client)
            assert repeated == local_chunks and len(requests) == before and cached["calls"] == 0
            with tempfile.TemporaryDirectory() as rotated, patch.dict(os.environ, {"RAG_EMBEDDING_API_KEY": "rotated-local-neutral"}):
                semantic_chunks([short], local_config, 35, 5, root=rotated, client=client)
                assert auth[-1] == "Bearer rotated-local-neutral"
            assert all(secret not in path.read_text() for path in (Path(local) / "semantic-cache").glob("*.json")
                       for secret in ("chat-neutral", "local-neutral"))
        try:
            replace(config, provider="unknown")
        except ValueError:
            pass
        else:
            raise AssertionError("Unknown auth mode accepted")
    check_http_diagnostics()
    print("semantic checks passed")


def check_http_diagnostics():
    """Both providers over offline HTTP transport; canonical routing and bounded diagnostics."""
    from rag.semantic import _call
    from shared_models import OPENROUTER_BASE_URL
    observed = []
    state = {"body": {"error": {"code": "guardrail_violation", "message": "Model denied by guardrail"}}}
    def transport(request):
        observed.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(403, json=state["body"])
    with httpx.Client(transport=httpx.MockTransport(transport)) as client, patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "  offline-chat-secret  ", "RAG_EMBEDDING_API_KEY": "  offline-local-secret  "}):
        for provider in ("openrouter", "compatible"):
            config = SemanticConfig(base_url="http://neutral.test/v1", provider=provider)
            fixtures = [
                ({"error": {"code": "guardrail_violation", "message": "Model denied by guardrail"}}, "guardrail_violation"),
                ({"error": {"message": "Denied\n by\tguardrail"}}, "Denied by guardrail"),
                ({"error": {"code": 403, "message": "x" * 1000}}, "; code 403: " + "x" * 400),
                ("<html>Cloudflare denial</html>", None),
                ({"error": {"message": "<html>blocked</html>"}}, None),
                ({"error": {"message": "Bearer unrecognized-secret"}}, None),
                ({"error": {"code": "sk-unknown-fixture-key", "message": "Denied"}}, None),
                ({"error": {"message": "sk-\x00unknown-fixture-key"}}, None),
            ]
            for secret in ("offline-chat-secret", "  offline-chat-secret  ", "offline-local-secret"):
                fixtures.append(({"error": {"message": secret}}, None))
                escaped = "".join("\\u%04x" % ord(c) for c in secret)
                for reflected in (escaped, escaped.replace("\\", "\\\\")):
                    fixtures.append(({"error": {"code": 403, "message": "Credential " + reflected}}, None))
                    fixtures.append(({"error": {"code": reflected, "message": "Denied"}}, None))
                for control in ("\x00", "\x1b", "\u200b"):
                    reflected = secret[:7] + control + secret[7:]
                    fixtures.append(({"error": {"code": 403, "message": reflected}}, None))
                    fixtures.append(({"error": {"code": reflected, "message": "Denied"}}, None))
            for body, hint in fixtures:
                state["body"] = body
                try:
                    _call(client, config, {"model": config.model, "messages": []})
                except ValueError as error:
                    message = str(error)
                    assert "status 403" in message and len(message) < 520
                    assert "offline-local-secret" not in message and "offline-chat-secret" not in message
                    assert (hint is not None and hint in message) or (hint is None and message == "Semantic HTTP error: status 403")
                else:
                    raise AssertionError("HTTP failure accepted")
            expected = OPENROUTER_BASE_URL if provider == "openrouter" else "http://neutral.test/v1"
            expected_key = "offline-chat-secret" if provider == "openrouter" else "offline-local-secret"
            assert observed[-1] == (expected + "/chat/completions", "Bearer " + expected_key)


if __name__ == "__main__":
    check_semantic()
