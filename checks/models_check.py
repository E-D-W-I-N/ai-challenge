"""Offline shared providers, immutable embedding identity and full-permutation rerank."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from checks import _stub


def check_models():
    from app import agent as agents, main, llm, rag
    from app.schema import AgentSpec
    from app.store import Store
    from shared_models import bind_compatible_url, endpoint, OPENROUTER_BASE_URL
    from rag.documents import ingest
    from rag.embeddings import EmbeddingConfig
    from rag.index import Index, build_index

    generic = AgentSpec(label="neutral", model="same-id", provider="compatible")
    payload = llm.build_payload(generic, [{"role": "user", "content": "neutral"}])
    assert not ({"provider", "plugins", "usage", "reasoning"} & set(payload))
    assert payload["stream_options"] == {"include_usage": True}
    router = llm.build_payload(AgentSpec(label="neutral", model="same-id"), [])
    assert router["provider"] == {"require_parameters": True} and router["usage"] == {"include": True}
    legacy = agents.spec_from_config({"model": "saved-exact-id", "rag_final_k": 7, "rag_candidates_k": 20}, fallback=generic)
    assert legacy.model == "saved-exact-id" and legacy.rag_top_k == 7 and not legacy.rag_rerank_enabled

    # Status exposes editable canonical selectors while archived identities stay exact.
    from app import rag_api
    legacy_stages = {"corpus": {"preparation_config": {"auth_mode": "compatible", "model": "saved-prep"}},
                     "chunks": {"semantic_config": {"auth_mode": "openrouter", "model": "saved-chunks"}},
                     "embeddings": {"embedding_config": {"model": "saved-vector", "base_url": "http://old.test/v1"}}}
    archived = {"index": {"embedding_config": copy.deepcopy(legacy_stages["embeddings"]["embedding_config"]), "embedding_fingerprint": "pinned"}}
    original = copy.deepcopy(legacy_stages)
    class StatusIndex:
        root = Path("/neutral-nonexistent-status")
        def status(self): return copy.deepcopy(archived)
    with patch.object(rag_api, "Index", StatusIndex), patch.object(rag_api.workflow, "stages", return_value=legacy_stages):
        restored = rag_api.status()
    assert restored["stages"]["corpus"]["preparation_config"] == {"provider": "compatible", "model": "saved-prep"}
    assert restored["stages"]["chunks"]["semantic_config"] == {"provider": "openrouter", "model": "saved-chunks"}
    assert restored["embedding_defaults"]["provider"] == "compatible"
    assert restored["index"] == archived["index"] and legacy_stages == original
    restored["embedding_defaults"]["model"] = "editable"
    assert restored["stages"]["embeddings"]["embedding_config"]["model"] == "saved-vector"
    invalid = copy.deepcopy(original); invalid["corpus"]["preparation_config"]["auth_mode"] = "unknown"
    with patch.object(rag_api, "Index", StatusIndex), patch.object(rag_api.workflow, "stages", return_value=invalid):
        assert "stage_error" in rag_api.status()

    async def collect(response):
        return [event async for event in response]

    async def transport_contract():
        seen = []
        def respond(request):
            body = json.loads(request.content)
            seen.append((str(request.url), request.headers.get("Authorization"), body))
            frames = [{"choices": [{"delta": {"content": "neutral"}}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                      {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}]
            return httpx.Response(200, text="".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(llm, "shared_client", return_value=client), patch.dict(os.environ, {"OPENROUTER_API_KEY": "", "RAG_EMBEDDING_API_KEY": ""}), bind_compatible_url("http://neutral.test/v1"):
                result = await collect(llm.stream_completion(generic, prompt_override=[]))
        assert result[-1]["text"] == "neutral" and result[-1]["metrics"]["total_tokens"] == 5
        assert result[-1]["metrics"]["cost_usd"] is None
        assert seen[0][0] == "http://neutral.test/v1/chat/completions" and seen[0][1] is None
        assert seen[0][2]["stream_options"] == {"include_usage": True}
    asyncio.run(transport_contract())

    with tempfile.TemporaryDirectory(prefix="models-neutral-") as temp:
        root = Path(temp)
        store = Store(root / "chat.sqlite").init()
        store.save_model_settings({"compatible_base_url": "http://new-neutral.test/v1"})
        reopened = Store(root / "chat.sqlite").init()
        assert reopened.load_model_settings() == store.load_model_settings()
        reopened.close()
        html = root / "neutral.html"; html.write_text("<html><title>Neutral</title><p>The neutral object is blue.</p></html>")
        ingest([{"path": str(html)}], root)
        config = EmbeddingConfig("http://old-neutral.test/v1", "saved-vector-id", 2, "saved-revision")
        calls = []; mode = {"query": False, "wrong_dimension": False}
        def embeddings(request):
            body = json.loads(request.content); calls.append((str(request.url), body))
            if mode["query"]:
                assert request.url.host == "new-neutral.test", "Query must never send new credentials to old host"
                assert body["model"] == "saved-vector-id" and body["dimensions"] == 2
            vector = [1, 0, 0] if mode["wrong_dimension"] else [1, 0]
            return httpx.Response(200, json={"model": body["model"], "data": [{"index": i, "embedding": vector} for i, _ in enumerate(body["input"])]})
        with httpx.Client(transport=httpx.MockTransport(embeddings)) as client:
            metadata = build_index(root, config, client=client)
            # Reproduce exact legacy four-field metadata without rewriting its fingerprint.
            old_config = {k: v for k, v in asdict(config).items() if k != "provider"}
            with sqlite3.connect(root / "index.sqlite") as db:
                db.execute("UPDATE metadata SET value=? WHERE key='embedding_config'", (json.dumps(old_config),))
            mode["query"] = True
            result = Index(root).retrieve("neutral", client=client, expected_base_url="http://new-neutral.test/v1")
            assert result["index"]["embedding_config"] == old_config
            assert result["index"]["embedding_fingerprint"] == metadata["embedding_fingerprint"] == config.fingerprint()
            mode["wrong_dimension"] = True
            try: Index(root).retrieve("neutral", client=client, expected_base_url="http://new-neutral.test/v1")
            except ValueError as error: assert "dimension mismatch" in str(error)
            else: raise AssertionError("Wrong dimension accepted")

        async def pipeline():
            spec = AgentSpec(label="rerank", model="saved-chat", provider="compatible", rag_enabled=True,
                             rag_top_k=3, rag_filter_enabled=True, rag_rerank_enabled=True,
                             rag_rerank_provider="compatible", rag_rerank_model="saved-ranking-model")
            chat = agents.Agent(spec, store=store)
            lookup_urls = []; round_urls = []
            def candidates(index, query, top_k, **options):
                lookup_urls.append(options["expected_base_url"])
                return {"query": query, "index": {"index_id": "neutral-pinned"}, "hits": [
                    {"chunk_id": str(i), "source": "fixture:" + str(i), "text": "neutral text " + str(i), "score": score}
                    for i, score in enumerate((.9, .3, .1))][:top_k]}
            def reply(messages, index):
                round_urls.append(endpoint("compatible"))
                if index == 0:
                    # User changes settings during an in-flight ask; this ask remains on its frozen URL.
                    store.save_model_settings({"compatible_base_url": "http://later-neutral.test/v1"})
                    return '{"query":"neutral rewritten"}'
                return '{"source_ids":[2,1]}' if index == 1 else "neutral final"
            _stub.reset()
            with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", _stub.make(reply)):
                events = await collect(chat.ask("neutral question"))
            assert events[-1]["committed"] and len(_stub.CALLS) == 3
            assert round_urls == ["http://new-neutral.test/v1"] * 3 and lookup_urls == ["http://new-neutral.test/v1"]
            assert [h["chunk_id"] for h in chat.history[-1].rag["hits"]] == ["1", "0"]
            assert [c["decision"] for c in chat.history[-1].rag["candidates"]] == ["kept", "kept", "threshold"]
            proof = chat.history[-1].rag["rerank"]
            assert proof["source_ids"] == [2, 1] and "scores" not in proof
            bodies = chat.history[-1].request_bodies
            assert bodies == [c["payload"] for c in _stub.CALLS] and bodies[1]["model"] == "saved-ranking-model"
            assert all("provider" not in body and "plugins" not in body for body in bodies)
            assert chat.history[-1].metrics["total_tokens"] == 300 and chat.history[-1].metrics["cost_usd"] == .000369
            assert chat.spec.model == "saved-chat" and chat.spec.rag_top_k == 3
            for invalid in ('{"source_ids":[1]}', '{"source_ids":[1,1]}', '{"source_ids":[true,2]}', '{"source_ids":[1,2],"scores":[1,0]}', 'invalid'):
                depth = len(chat.history); _stub.reset()
                def invalid_reply(messages, index):
                    return '{"query":"neutral rewritten"}' if index == 0 else invalid
                with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", _stub.make(invalid_reply)):
                    failed = await collect(chat.ask("neutral invalid"))
                assert not failed[-1]["committed"] and len(chat.history) == depth and len(_stub.CALLS) == 2
                assert failed[-1]["metrics"]["total_tokens"] == 200 and failed[-1]["request_bodies"] == [c["payload"] for c in _stub.CALLS]
            # Cancelled rerank exposes paid metrics without fabricated identity permutation.
            cancel = asyncio.Event(); entered = asyncio.Event()
            async def cancelling(session, **options):
                yield {"type": "metrics", "metrics": {"total_tokens": 17, "cost_usd": .01}}
                entered.set(); await asyncio.sleep(10)
            task = asyncio.create_task(rag.rerank("neutral", {"hits": [{"text": "a"}]}, spec, cancelling, cancel))
            await entered.wait(); cancel.set(); cancelled = await task
            assert cancelled["cancelled"] and "source_ids" not in cancelled and cancelled["usage"]["total_tokens"] == 17
        asyncio.run(pipeline())
        store.close()
    with TestClient(main.app) as client, patch.object(main, "has_key", return_value=False), patch.object(agents, "stream_completion", _stub.make()):
        created = client.post("/api/agents", json={"agent": {"label": "compatible", "model": "neutral", "provider": "compatible"}}).json()["agents"][0]
        route = f'/api/agents/{created["id"]}'
        assert created["provider"] == "compatible" and main.REGISTRY.require(created["id"]).context_length is None
        assert client.post(route + "/messages", json={"text": "neutral"}).status_code == 200
        assert client.post(route + "/regenerate").status_code == 200
        assert client.patch("/api/model-settings", json={"compatible_base_url": "http://user:secret@neutral.test"}).status_code == 422
        assert client.patch("/api/model-settings", json={"compatible_base_url": "http://neutral.test/v1"}, headers={"Origin": "https://cross.test"}).status_code == 403
        client.delete(route)
    return "provider payload/auth; optional compatible key; frozen shared URL; legacy embedding identity/current dispatch; full permutation/strict failure/paid usage"
