"""Offline day23 rewrite/filter contracts, with neutral synthetic hits."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from fastapi.testclient import TestClient
from checks import _stub


def check_rag_refinement():
    from app import agent as agents, main, rag
    from app.schema import AgentSpec
    from app.store import Store

    async def drain(stream):
        return [event async for event in stream]

    def candidates(index, query, top_k):
        queries.append((query, top_k))
        return {"query": query, "top_k": top_k, "index": {"index_id": "pinned-neutral"},
                "hits": [{"chunk_id": str(i), "text": f"neutral chunk {i}", "score": score}
                         for i, score in enumerate((0.9, 0.6, 0.3, 0.299, -0.2))][:top_k]}

    queries = []
    spec = AgentSpec(label="refined", model="stub/chat", rag_enabled=True, rag_final_k=2)
    with patch.object(rag.Index, "retrieve", candidates):
        filtered = rag.retrieve("search", original_query="question", spec=spec)
        assert queries == [("search", 20)]
        assert [c["decision"] for c in filtered["candidates"]] == ["kept", "kept", "final_cap", "threshold", "threshold"]
        assert [h["score"] for h in filtered["hits"]] == [0.9, 0.6]
        spec.rag_final_k = 5
        inclusive = rag.retrieve("search", spec=spec)
        assert [h["score"] for h in inclusive["hits"]] == [0.9, 0.6, 0.3]
        spec.rag_filter_enabled = False; spec.rag_rewrite_enabled = False
        plain = rag.retrieve("plain", spec=spec)
        assert queries[-1] == ("plain", 5) and len(plain["hits"]) == 5
        assert all(c["decision"] == "kept" for c in plain["candidates"])

    async def scenarios(root):
        store = Store(str(root / "chat.sqlite")).init()
        chat = agents.Agent(AgentSpec(label="new", model="openai/gpt-6-luna", rag_enabled=True,
                                     system="PRIVATE CHAT INSTRUCTION", extra_body={"seed": 78}), store=store)
        for i in range(5):
            chat.remember("user", f"old question {i}"); chat.remember("assistant", f"old answer {i}")
        # Exclude incomplete and failed pairs from rewrite history.
        chat.remember("user", "failed question"); chat.remember("assistant", "failed answer", error="failed")
        before = len(chat.history)
        def reply(messages, index):
            return '{"query":"standalone neutral 42"}' if index == 0 else "final neutral answer"
        _stub.reset(); queries.clear()
        with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", _stub.make(reply)):
            events = await drain(chat.ask("current question 42"))
        assert events[-1]["committed"] and len(_stub.CALLS) == 2
        assert [e["stage"] for e in events if e["type"] == "retrieval"] == ["rewrite", "search", "filter"]
        assert queries == [("standalone neutral 42", 20)]
        bodies = chat.history[-1].request_bodies; snapshot = chat.history[-1].rag
        assert bodies == [c["payload"] for c in _stub.CALLS]
        first = bodies[0]; last = bodies[-1]
        assert first["model"] == last["model"] == "openai/gpt-6-luna"
        assert first["reasoning"] == {"effort": "none"} and first["max_tokens"] == 9216
        assert "seed" not in first and "tools" not in first and first["messages"][0]["content"] != chat.spec.system
        used = json.loads(first["messages"][1]["content"])
        assert used["question"] == "current question 42"
        assert used["history"] == [{"user": f"old question {i}", "assistant": f"old answer {i}"} for i in (2,3,4)]
        assert snapshot["original_query"] == "current question 42" and snapshot["history_used"] == used["history"]
        assert last["messages"][-1]["content"] == "current question 42"
        assert snapshot["context"] == last["messages"][-2]["content"]
        assert events[-1]["metrics"]["total_tokens"] == 200 and events[-1]["metrics"]["cost_usd"] == 0.000246
        assert len(chat.history) == before + 2
        saved = copy.deepcopy(snapshot)
        loaded = agents.Agent(chat.spec, agent_id=chat.id, store=store)
        assert loaded.history[-1].rag == saved and loaded.history[-1].request_bodies == bodies
        branch = chat.carry_off(len(chat.history)); branch["history"][-1].rag["candidates"][0]["text"] = "mutation"
        assert chat.history[-1].rag == saved

        # Unknown rewrite cost stays unknown in its actual-call record.
        _stub.reset()
        with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", _stub.make(reply, usage={"cost_usd": None})):
            await drain(chat.ask("unknown cost"))
        assert chat.history[-1].rag["rewrite"]["usage"]["cost_usd"] is None

        # No hits skip compression, tool lease and final generation; persist rewrite request.
        chat.spec.rag_similarity_threshold = 1.0
        _stub.reset()
        with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", _stub.make('{"query":"empty lookup"}')), \
             patch.object(chat, "compress", side_effect=AssertionError("nohit compression")):
            empty = await drain(chat.ask("absent", scheduled={"id": 7, "server": "fixture"}))
        assert len(_stub.CALLS) == 1 and empty[-1]["text"] == rag.NO_HITS and empty[-1]["committed"]
        assert next(e for e in empty if e["type"] == "start")["generation"] is False
        assert chat.history[-1].metrics["reminder_execution"]["id"] == 7
        assert chat.history[-1].rag["hits"] == [] and len(chat.history[-1].request_bodies) == 1

        # All non-success completions are terminal, with known paid diagnostics, no retry/retrieval.
        for finish, output in ((None, '{"query":"x"}'), ("length", '{"query":"x"}'), ("stop", "bad json")):
            _stub.reset(); queries.clear(); depth = len(chat.history)
            with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", _stub.make(output, usage={"finish_reason": finish})):
                failed = await drain(chat.ask("retry input"))
            assert len(_stub.CALLS) == 1 and not queries and len(chat.history) == depth
            assert failed[-1]["committed"] is False and failed[-1]["question"] == "retry input"
            assert failed[-1]["metrics"]["cost_usd"] == 0.000123 and failed[-1]["request_bodies"] == [_stub.CALLS[0]["payload"]]
        taken = chat.take_last_exchange(); previous = copy.deepcopy(taken.turns[-1].rag)
        with patch.object(agents, "stream_completion", _stub.make("bad json")):
            failed = await drain(chat.ask(taken.question))
        assert not failed[-1]["committed"] and chat.restore(taken) and chat.history[-1].rag == previous

        # Successful rewrite followed by retrieval failure keeps actual usage and JSON.
        _stub.reset()
        with patch.object(agents, "rag_lookup", side_effect=ValueError("neutral lookup failure")), patch.object(agents, "stream_completion", _stub.make('{"query":"x"}')):
            failed = await drain(chat.ask("lookup error"))
        assert failed[-1]["metrics"]["total_tokens"] == 100 and len(failed[-1]["request_bodies"]) == 1

        # Scheduler persists paid failed rewrite usage in its existing error assistant.
        from app.reminders import ReminderScheduler
        server = SimpleNamespace(name="fixture", status="ok", timeout_s=1)
        manager = SimpleNamespace(servers=[server], reminder_protocol=AsyncMock(return_value=True))
        scheduler = ReminderScheduler(manager, SimpleNamespace(store=store))
        item = {"id": 9, "text": "neutral", "context_id": "neutral"}
        scheduler.claims[(server.name, item["id"])] = {}
        _stub.reset()
        with patch.object(scheduler, "receipt", return_value=True), patch.object(agents, "stream_completion", _stub.make("invalid JSON")):
            await scheduler._execute(server, item, "neutral token", chat)
        assert chat.history[-1].error and chat.history[-1].metrics["cost_usd"] == 0.000123
        assert chat.history[-1].metrics["reminder_execution"]["id"] == 9
        assert chat.history[-1].request_bodies == [_stub.CALLS[0]["payload"]]

        # Disconnect during final generation commits partial answer and known paid rewrite once.
        _stub.reset()
        async def partial(session, **kwargs):
            if session.label == "RAG query rewrite":
                async for event in _stub.make('{"query":"x"}')(session, **kwargs): yield event
            else:
                yield {"type":"delta", "text":"partial", "metrics":{"total_tokens":7,"cost_usd":0.1}}
                raise asyncio.CancelledError()
        chat.spec.rag_similarity_threshold = 0.3
        with patch.object(rag.Index, "retrieve", candidates), patch.object(agents, "stream_completion", partial):
            try: await drain(chat.ask("disconnect"))
            except asyncio.CancelledError: pass
            else: raise AssertionError("disconnect must propagate")
        assert chat.history[-1].content == "partial" and chat.history[-1].metrics["total_tokens"] == 107
        assert chat.history[-1].metrics["cost_usd"] == 0.100123

        # Timeout closes the stream and never runs retrieval; cancel is terminal too.
        for cancel_now in (False, True):
            _stub.reset(); queries.clear()
            with patch.object(rag, "REWRITE_TIMEOUT", 0.01 if not cancel_now else 60), \
                 patch.object(rag.Index, "retrieve", candidates), \
                 patch.object(agents, "stream_completion", _stub.make('{"query":"x"}', delay=0.1)):
                stream = chat.ask("interrupt")
                assert (await anext(stream))["stage"] == "rewrite"
                task = asyncio.create_task(drain(stream))
                await asyncio.sleep(0.005)
                if cancel_now: chat.cancel()
                result = await task
            assert not queries and len(_stub.CALLS) == 1 and _stub.ACTIVE["now"] == 0
            assert not result[-1]["committed"]
            if cancel_now: assert result[-1]["cancelled"]
        # Cancellation at the SSE boundary prevents even the rewrite call.
        _stub.reset()
        with patch.object(agents, "stream_completion", _stub.make()):
            stream = chat.ask("cancel before paid call")
            await anext(stream); chat.cancel(); result = await drain(stream)
        assert not _stub.CALLS and result[-1]["cancelled"]
        chat.spec.rag_enabled = False
        with patch.object(agents, "rag_lookup", side_effect=AssertionError("OFF retrieval")), patch.object(agents, "rag_rewrite", side_effect=AssertionError("OFF rewrite")), patch.object(agents, "stream_completion", _stub.make("plain answer")):
            off = await drain(chat.ask("off"))
        assert off[0]["type"] == "start" and chat.history[-1].rag is None
        store.close()

    with tempfile.TemporaryDirectory(prefix="rag-refinement-") as temp:
        asyncio.run(scenarios(Path(temp)))
    legacy = agents.spec_from_config({"label":"legacy", "model":"stub/model", "rag_enabled": True}, fallback=spec)
    assert legacy.rag_enabled and not legacy.rag_rewrite_enabled and not legacy.rag_filter_enabled
    half = agents.spec_from_config({"model":"stub/model", "rag_rewrite_enabled": True}, fallback=spec)
    assert half.rag_rewrite_enabled and not half.rag_filter_enabled
    with TestClient(main.app) as client:
        result = client.post("/api/agents", json={"agent":{"label":"new defaults", "model":"stub/model"}}).json()["agents"][0]
        assert not result["rag_enabled"] and result["rag_rewrite_enabled"] and result["rag_filter_enabled"]
        assert (result["rag_candidates_k"],result["rag_final_k"],result["rag_similarity_threshold"]) == (20,5,0.3)
        route = f'/api/agents/{result["id"]}'
        for invalid in ({"rag_rewrite_enabled": None}, {"rag_filter_enabled": 1}, {"rag_candidates_k": True}, {"rag_final_k": 101}, {"rag_similarity_threshold": "0.3"}, {"rag_candidates_k": 4}, {"rag_rewrite_enabled": False, "system": 123}):
            before = copy.deepcopy(main.REGISTRY.require(result["id"]).spec)
            assert client.patch(route,json=invalid).status_code == 400
            assert main.REGISTRY.require(result["id"]).spec == before
        assert client.patch(route, json={"rag_candidates_k": 3, "rag_final_k": 2, "rag_similarity_threshold": -1}).status_code == 200
        assert client.patch(route, json={"rag_final_k": 4}).status_code == 400
        client.delete(route)
    return "threshold inclusive/cap reasons; rewrite 3pairs/model/strictstop/cancel/timeout; nohits/reminders; exactJSON/usage/restart/deepcopy/legacy/API"
