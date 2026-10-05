"""Offline independent reasoning preferences, actual provider payloads and reported violations."""
import asyncio
import json
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from checks import _stub


def check_reasoning():
    from app import agent, llm, main, rag, rag_api
    from app.schema import AgentSpec
    from app.store import Store
    from rag.embeddings import EmbeddingConfig, Embeddings
    from rag.semantic import SemanticConfig, _payload as semantic_payload, _call
    from rag.preparation import PreparationConfig, _payload as preparation_payload
    from shared_models import bind_compatible_url, generation_payload

    overrides = {"reasoning": {"enabled": True, "effort": "high", "exclude": True},
                 "reasoning_effort": "high", "include_reasoning": True, "thinking": {"type": "enabled"},
                 "enable_thinking": True, "thinking_budget": 1000,
                 "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": "high", "neutral": 42},
                 "output_config": {"effort": "high", "neutral": 7}, "seed": 8,
                 "provider": {"require_parameters": False, "order": ["neutral"]}}
    for provider in ("openrouter", "compatible"):
        for enabled in (False, True):
            spec = AgentSpec(label="neutral", model="neutral/model", provider=provider, reasoning_enabled=enabled, extra_body=overrides)
            body = llm.build_payload(spec, [])
            assert body["seed"] == 8 and body["chat_template_kwargs"] == {"neutral": 42} and body["output_config"] == {"neutral": 7}
            assert not {"enable_thinking", "thinking_budget", "thinking"} & body.keys()
            if provider == "openrouter":
                assert body["reasoning"] == ({"enabled": True, "exclude": False} if enabled else {"effort": "none", "enabled": False, "exclude": False})
                assert body["provider"]["require_parameters"] is True and "reasoning_effort" not in body
            else:
                assert body["reasoning_effort"] == ("medium" if enabled else "none") and "reasoning" not in body
            assert generation_payload(body, provider, reasoning_enabled=enabled) == body
            semantic = SemanticConfig(model="neutral/model", provider=provider, reasoning_enabled=enabled)
            preparation = PreparationConfig(model="neutral/model", provider=provider, reasoning_enabled=enabled)
            for service in (semantic_payload("neutral", [(0, 7)], semantic, 64), preparation_payload("<p>neutral</p>", preparation)):
                assert service.get("reasoning") == body.get("reasoning") and service.get("reasoning_effort") == body.get("reasoning_effort")
        assert SemanticConfig(provider=provider).fingerprint() != SemanticConfig(provider=provider, reasoning_enabled=True).fingerprint()

    for field, value in (("configuration_update", {"reasoning": {"enabled": True}}), ("output_config", {"effort": "high"})):
        try: generation_payload({"messages": [{"role": "user", "content": "neutral", field: value}]}, "openrouter")
        except ValueError: pass
        else: raise AssertionError("per-message override bypassed OFF")
    for cls in (SemanticConfig, PreparationConfig, EmbeddingConfig):
        for invalid in (None, 1, "true"):
            try: cls(reasoning_enabled=invalid)
            except ValueError: pass
            else: raise AssertionError("nonboolean reasoning accepted")
    for field in ("reasoning_enabled", "preparation_reasoning_enabled", "semantic_reasoning_enabled"):
        try: rag_api.StageRequest(**{field: 1})
        except ValueError: pass
        else: raise AssertionError("coerced stage boolean")

    with tempfile.TemporaryDirectory(prefix="neutral-reasoning-") as directory:
        store = Store(Path(directory) / "chat.sqlite").init()
        defaults = AgentSpec(label="neutral", model="neutral/model")
        legacy = agent.spec_from_config({"model": "neutral/model"}, fallback=defaults)
        assert not legacy.reasoning_enabled and not legacy.rag_rerank_reasoning_enabled
        assert (legacy.rag_candidates_k, legacy.rag_final_k) == (10, 3)
        chat = agent.Agent(defaults, store=store)
        chat.spec.reasoning_enabled = True
        chat.save_config()
        loaded = agent.Agent(defaults, agent_id=chat.id, store=store)
        assert loaded.spec.reasoning_enabled
        from app.registry import AgentRegistry
        fork = AgentRegistry(store=store).fork(loaded, 0, label="neutral branch")
        assert fork.spec.reasoning_enabled and not fork.spec.rag_rerank_reasoning_enabled
        assert store.load_session(fork.id)["config"]["reasoning_enabled"] is True
        store.close()

    with TestClient(main.app) as client:
        created = client.post("/api/agents", json={"agent": {"model": "neutral/model"}}).json()["agents"][0]
        route = '/api/agents/' + created["id"]
        assert not created["reasoning_enabled"] and not created["rag_rerank_reasoning_enabled"]
        assert (created["rag_candidates_k"], created["rag_final_k"]) == (10, 3)
        for field in ("reasoning_enabled", "rag_rerank_reasoning_enabled"):
            for invalid in (None, 1, "true"):
                assert client.patch(route, json={field: invalid, "label": "must not save"}).status_code == 400
                assert client.get(route).json()["label"] == created["label"]
            assert client.patch(route, json={field: True}).json()[field] is True
        assert client.patch(route, json={"reasoning_enabled": False}).json()["rag_rerank_reasoning_enabled"] is True
        client.delete(route)

    embedding = EmbeddingConfig(model="neutral/embed", provider="compatible")
    assert embedding.fingerprint() == replace(embedding, reasoning_enabled=True).fingerprint()
    archived = asdict(embedding); archived.pop("reasoning_enabled")
    assert EmbeddingConfig(**archived).fingerprint() == embedding.fingerprint()
    seen = []
    def embed_response(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
    with httpx.Client(transport=httpx.MockTransport(embed_response)) as client:
        for enabled in (False, True):
            Embeddings(replace(embedding, reasoning_enabled=enabled), client).embed(["neutral"])
    assert seen[0] == seen[1] == {"model": "neutral/embed", "input": ["neutral"], "encoding_format": "float"}

    traced = []; dispatched = []
    def semantic_response(request):
        dispatched.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"end_unit_ids":[1]}'}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 2}}})
    with httpx.Client(transport=httpx.MockTransport(semantic_response)) as client:
        config = SemanticConfig(model="neutral/model", provider="compatible")
        try: _call(client, config, semantic_payload("neutral", [(0, 7)], config, 64), traced.append)
        except ValueError as error: assert "reasoning while disabled" in str(error)
        else: raise AssertionError("synchronous reasoning bypassed OFF")
        assert len(traced) == len(dispatched) == 1 and traced[0]["usage"]["total_tokens"] == 5
        config = replace(config, reasoning_enabled=True)
        _call(client, config, semantic_payload("neutral", [(0, 7)], config, 64), traced.append)
        assert dispatched[-1]["reasoning_effort"] == "medium"

    async def contracts():
        for provider in ("openrouter", "compatible"):
            for evidence in ({"reasoning_content": "neutral thought"}, {"reasoning_details": [{"type": "reasoning.encrypted", "data": "neutral"}]}, None):
                captured = []
                def respond(request):
                    captured.append(json.loads(request.content))
                    frames = [{"choices": [{"delta": evidence or {"content": "neutral", "tool_calls": [{"index": 0, "id": "neutral", "function": {"name": "neutral", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
                              {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "cost": .001,
                               "completion_tokens_details": {"reasoning_tokens": 1 if evidence is None else 0}}}]
                    return httpx.Response(200, text="".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n")
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    with patch.object(llm, "shared_client", return_value=client), patch.object(llm, "api_key", return_value="neutral-test-key"), bind_compatible_url("http://neutral.test/v1"), llm.capture_requests() as requests:
                        events = [event async for event in llm.stream_completion(AgentSpec(label="neutral", model="neutral/model", provider=provider), prompt_override=[])]
                assert not any(event["type"] == "tool_calls" for event in events)
                assert any(event["type"] == "error" for event in events) and events[-1]["metrics"]["error"]
                assert events[-1]["text"] == "" and events[-1]["metrics"]["total_tokens"] == 5 and events[-1]["metrics"]["cost_usd"] == .001
                assert requests == captured and len(captured) == 1
            # Chat rewrite inherits ON; independent rerank remains OFF.
            _stub.reset()
            await rag.rewrite("neutral", [], "neutral/model", _stub.make('{"query":"neutral"}'), asyncio.Event(), provider=provider, reasoning_enabled=True)
            snapshot = {"hits": [{"text": "neutral"}], "candidates": [{"text": "neutral"}]}
            spec = AgentSpec(label="neutral", model="neutral/model", provider=provider, reasoning_enabled=True, rag_rerank_provider=provider)
            await rag.rerank("neutral", snapshot, spec, _stub.make('{"source_ids":[1]}'), asyncio.Event())
            first, second = [call["payload"] for call in _stub.CALLS]
            assert first.get("reasoning", {}).get("enabled", first.get("reasoning_effort") == "medium")
            assert second.get("reasoning", {}).get("enabled", second.get("reasoning_effort") != "none") is False
            for enabled in (False, True):
                with tempfile.TemporaryDirectory(prefix="neutral-compression-") as temporary:
                    store = Store(Path(temporary) / "chat.sqlite").init()
                    spec = AgentSpec(label="neutral", model="neutral/model", provider=provider, reasoning_enabled=enabled,
                                     strategy="summary", keep_last=0, compress_every=2)
                    chat = agent.Agent(spec, store=store)
                    chat.history = [agent.Turn(role="user", content="neutral"), agent.Turn(role="assistant", content="neutral answer")]
                    _stub.reset()
                    with patch.object(agent, "stream_completion", _stub.make("neutral summary")):
                        await chat.compress(spec)
                    assert len(_stub.CALLS) == 1
                    body = _stub.CALLS[0]["payload"]
                    assert body.get("reasoning", {}).get("enabled", body.get("reasoning_effort") == "medium") is enabled
                    store.close()
    asyncio.run(contracts())
    return "independent flags/API/migration/fork; 10/3; actual ON/OFF payload and override precedence; reported violation usage; embedding identity/HTTP unchanged"
