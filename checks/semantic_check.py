"""Neutral offline generation: real HTTP boundary, exact slices and validated cache."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from rag.documents import digest
from rag.semantic import SemanticConfig, semantic_chunks


def document(text):
    blocks, start = [], 0
    for paragraph in text.splitlines(keepends=True):
        blocks.append({"start": start, "end": start + len(paragraph), "section": "neutral"})
        start += len(paragraph)
    return {"document_id": digest(text), "text": text, "content_hash": digest(text),
            "source": "neutral.html", "title": "Neutral", "blocks": blocks}


@patch.dict(os.environ, {"RAG_CHUNKING_API_KEY": "", "OPENROUTER_API_KEY": "", "RAG_EMBEDDING_API_KEY": ""})
def check_semantic():
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
        if mode["value"] == "oversize":
            ids = [len(ids)]
        content = json.dumps({"end_unit_ids": ids})
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
            assert request["max_tokens"] <= 4096
        before = len(requests)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "rotated-neutral-key"}):
            same, cached = semantic_chunks([doc], config, 90, 15, root=root, client=client)
        assert same == chunks and len(requests) == before and cached["calls"] == 0
        assert cached["usage"] == {} and cached["cached"] == 1
        assert cached["cost_usd"] == 0 and cached["model"] == "actual-neutral-model"
        cache_path = root / cached["trace_files"][0]
        saved = json.loads(cache_path.read_text())
        saved["spans"][0][1] += 1
        cache_path.write_text(json.dumps(saved))
        rebuilt, fresh = semantic_chunks([doc], config, 90, 15, root=root, client=client)
        assert rebuilt == chunks and fresh["calls"] > 0
        for changed in [replace(config, model="other-neutral-model"), replace(config, base_url="http://another.test/v1")]:
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
        for bad in ("unknown", "duplicate", "missing", "oversize", "malformed", "truncated", "usage", "http", "transport", "reflected", "escaped-reflected"):
            mode["value"] = bad
            with tempfile.TemporaryDirectory() as rejected, patch.dict(os.environ, {"OPENROUTER_API_KEY": "  credential-injected-secret  "}):
                try:
                    semantic_chunks([short], config, 35, 5, root=rejected, client=client)
                except ValueError as error:
                    assert "credential-injected-secret" not in str(error)
                else:
                    raise AssertionError(bad)
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
        with tempfile.TemporaryDirectory() as authorized, patch.dict(os.environ, {"OPENROUTER_API_KEY": "  fallback-neutral  ", "RAG_CHUNKING_API_KEY": "preferred-neutral"}):
            semantic_chunks([short], config, 35, 5, root=authorized, client=client)
            assert auth[-1] == "Bearer fallback-neutral"
            assert "preferred-neutral" not in next((Path(authorized) / "semantic-cache").glob("*.json")).read_text()
        local_config = replace(config, auth_mode="omlx")
        with tempfile.TemporaryDirectory() as local, patch.dict(os.environ, {
                "OPENROUTER_API_KEY": "chat-neutral", "RAG_EMBEDDING_API_KEY": "  local-neutral  ",
                "RAG_CHUNKING_API_KEY": "ignored-legacy-neutral"}):
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
                       for secret in ("chat-neutral", "local-neutral", "ignored-legacy-neutral"))
        try:
            replace(config, auth_mode="unknown")
        except ValueError:
            pass
        else:
            raise AssertionError("Unknown auth mode accepted")
    print("semantic checks passed")


if __name__ == "__main__":
    check_semantic()
