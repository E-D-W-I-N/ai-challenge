"""Neutral offline generation: real HTTP boundary, exact slices and validated cache."""
from __future__ import annotations

import json
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


@patch.dict(os.environ, {"RAG_CHUNKING_API_KEY": "", "OPENROUTER_API_KEY": ""})
def check_semantic():
    requests, mode, auth = [], {"value": "ok"}, []
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
        return httpx.Response(200, json={"model": payload["model"], "choices": [{"finish_reason":
            "length" if mode["value"] == "truncated" else "stop", "message": {"content": content}}], "usage": usage})
    with tempfile.TemporaryDirectory() as temporary, httpx.Client(transport=httpx.MockTransport(server)) as client:
        root = Path(temporary)
        config = SemanticConfig(base_url="http://neutral.test/v1")
        text = "Alpha topic.\n\nBeta topic has more detail.\n" + "Long neutral paragraph. " * 1200
        doc = document(text)
        chunks, report = semantic_chunks([doc], config, size=90, overlap=15, root=root, client=client)
        assert report["calls"] > 1 and report["computed"] == 1
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
        with patch.dict(os.environ, {"RAG_CHUNKING_API_KEY": "rotated-neutral-key"}):
            same, cached = semantic_chunks([doc], config, 90, 15, root=root, client=client)
        assert same == chunks and len(requests) == before and cached["calls"] == 0
        assert cached["usage"] == {} and cached["cached"] == 1
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
        short = document("First short topic.\nSecond short topic.\nThird short topic.\n")
        for bad in ("unknown", "duplicate", "missing", "oversize", "malformed", "truncated", "usage", "http", "transport"):
            mode["value"] = bad
            with tempfile.TemporaryDirectory() as rejected, patch.dict(os.environ, {"RAG_CHUNKING_API_KEY": "credential-injected-secret"}):
                try:
                    semantic_chunks([short], config, 35, 5, root=rejected, client=client)
                except ValueError as error:
                    assert "credential-injected-secret" not in str(error)
                else:
                    raise AssertionError(bad)
                assert not [p for p in (Path(rejected) / "semantic-cache").glob("*.json") if not p.name.startswith("round-")]
        mode["value"] = "ok"
        with tempfile.TemporaryDirectory() as authorized, patch.dict(os.environ, {"OPENROUTER_API_KEY": "fallback-neutral"}):
            semantic_chunks([short], config, 35, 5, root=authorized, client=client)
            assert auth[-1] == "Bearer fallback-neutral"
        with tempfile.TemporaryDirectory() as authorized, patch.dict(os.environ, {"OPENROUTER_API_KEY": "fallback-neutral", "RAG_CHUNKING_API_KEY": "preferred-neutral"}):
            semantic_chunks([short], config, 35, 5, root=authorized, client=client)
            assert auth[-1] == "Bearer preferred-neutral"
            assert "preferred-neutral" not in next((Path(authorized) / "semantic-cache").glob("*.json")).read_text()
    print("semantic checks passed")


if __name__ == "__main__":
    check_semantic()
