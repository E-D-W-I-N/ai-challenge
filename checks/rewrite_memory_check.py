"""Neutral Agent/HTTP boundary for per-exchange working memory in Rewrite."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
from checks import _stub


def check_rewrite_memory():
    from app import agent as agents, rag
    from app.schema import AgentSpec
    from app.store import Store
    from rag.documents import ingest
    from rag.embeddings import EmbeddingConfig
    from rag.index import Index, build_index

    async def run(root):
        source = root / "neutral.html"
        source.write_text("<article><h1>Neutral fixture</h1><p>Neutral source material.</p></article>")
        ingest([{"path": str(source)}], root)
        embedding_bodies = []
        def http(request):
            body = json.loads(request.content); embedding_bodies.append(body)
            return httpx.Response(200, json={"model": body["model"], "data": [
                {"index": i, "embedding": [1, 0]} for i, _ in enumerate(body["input"])]})
        store = Store(str(root / "chat.sqlite")).init()
        store.add_memory("fact", "GLOBAL_ONLY")
        store.save_profile({"style": "PROFILE_ONLY"})
        store.add_invariant("rule", "INVARIANT_ONLY")
        chat = agents.Agent(AgentSpec(label="neutral", model="stub/chat", rag_enabled=True), store=store)
        goal = chat.add_working_record("goal", "Neutral goal before")
        limit = chat.add_working_record("limit", "Neutral limit before")
        expected = [{"kind": "goal", "content": goal["content"]}, {"kind": "limit", "content": limit["content"]}]
        original_retrieve = Index.retrieve
        with httpx.Client(transport=httpx.MockTransport(http)) as client:
            build_index(root, EmbeddingConfig("http://neutral.test/v1", "neutral-embedding", dimensions=2, provider="compatible"), "fixed", client=client)
            def retrieve(index, query, **options):
                return original_retrieve(index, query, client=client, **options)
            entered = asyncio.Event(); release = asyncio.Event()
            async def paused(spec, **options):
                if spec.label == "RAG query rewrite":
                    entered.set(); await release.wait()
                async for event in _stub.make(reply)(spec, **options):
                    yield event
            def reply(messages, index):
                if index == 0:
                    return '{"query":"neutral expanded goal"}'
                return json.dumps({"answer": "Neutral source answer [1]", "citations": [{"source_id": 1, "quote": "Neutral source material."}]})
            async def drain():
                return [event async for event in chat.ask("What about that goal?")]
            _stub.reset(); embedding_bodies.clear()
            with patch.object(rag, "Index", return_value=Index(root)), patch.object(Index, "retrieve", retrieve), patch.object(agents, "stream_completion", paused):
                task = asyncio.create_task(drain()); await entered.wait()
                chat.edit_working_record(goal["seq"], content="Neutral goal after")
                chat.drop_working_record(limit["seq"])
                chat.add_working_record("decision", "Neutral decision after")
                release.set(); events = await task
            assert events[-1]["committed"] and len(_stub.CALLS) == 2
            bodies = chat.history[-1].request_bodies
            used = json.loads(bodies[0]["messages"][1]["content"])
            assert used == {"history": [], "question": "What about that goal?", "working_memory": expected}
            assert not any(marker in json.dumps(bodies[0]) for marker in ("GLOBAL_ONLY", "PROFILE_ONLY", "INVARIANT_ONLY"))
            assert embedding_bodies[-1]["input"] == ["neutral expanded goal"]
            final = bodies[-1]["messages"]
            assert final[-1]["content"] == "What about that goal?"
            start = next(e for e in events if e["type"] == "start")
            memory = final[start["working_at"]]["content"]
            assert "Neutral goal before" in memory and "Neutral limit before" in memory
            assert "Neutral goal after" not in memory and "Neutral decision after" not in memory
            assert start["resolved_messages"] == final
            saved = copy.deepcopy(bodies)
            loaded = agents.Agent(chat.spec, agent_id=chat.id, store=store)
            assert loaded.history[-1].request_bodies == saved
            assert loaded.working[0]["content"] == "Neutral goal after"
            carried = loaded.carry_off(len(loaded.history))
            carried["history"][-1].request_bodies[0]["messages"][1]["content"] = "mutation"
            assert loaded.history[-1].request_bodies == saved
            # The next exchange sees fresh records; an empty frozen snapshot remains empty.
            for empty in (False, True):
                if empty:
                    for item in list(chat.working): chat.drop_working_record(item["seq"])
                _stub.reset()
                def next_reply(messages, index):
                    if empty and index == 0:
                        chat.add_working_record("goal", "Added after empty snapshot")
                    return reply(messages, index)
                with patch.object(rag, "Index", return_value=Index(root)), patch.object(Index, "retrieve", retrieve), patch.object(agents, "stream_completion", _stub.make(next_reply)):
                    events = await drain()
                used = json.loads(_stub.CALLS[0]["messages"][1]["content"])
                assert used["working_memory"] == ([] if empty else [{"kind": "goal", "content": "Neutral goal after"}, {"kind": "decision", "content": "Neutral decision after"}])
                if empty: assert next(e for e in events if e["type"] == "start")["working_at"] is None
            # Rewrite OFF keeps ordinary RAG generation; RAG OFF has no service call.
            chat.spec.rag_rewrite_enabled = False
            _stub.reset()
            with patch.object(rag, "Index", return_value=Index(root)), patch.object(Index, "retrieve", retrieve), patch.object(agents, "stream_completion", _stub.make(lambda messages, index: reply(messages, 1))):
                await drain()
            assert len(_stub.CALLS) == 1 and embedding_bodies[-1]["input"] == ["What about that goal?"]
            chat.spec.rag_enabled = False
            _stub.reset()
            with patch.object(agents, "rag_rewrite", side_effect=AssertionError("RAG OFF Rewrite")), patch.object(agents, "rag_lookup", side_effect=AssertionError("RAG OFF lookup")), patch.object(agents, "stream_completion", _stub.make("Neutral ordinary answer")):
                await drain()
            assert len(_stub.CALLS) == 1
        store.close()

    with tempfile.TemporaryDirectory(prefix="rewrite-memory-") as temp:
        asyncio.run(run(Path(temp)))
    return "frozen working-only Rewrite/final slots; actual embedding query/original question; empty/OFF; persisted/forked actual JSON"
