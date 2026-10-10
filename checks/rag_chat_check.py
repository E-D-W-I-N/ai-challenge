"""Day22 retrieval/prompt/persistence lifecycle with neutral temporary inputs."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from shared_models import Connection, bind_connection
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from checks import _stub
from rag.chunks import chunk_documents
from rag.documents import ingest
from rag.embeddings import EmbeddingConfig
from rag.index import Index, build_index


def check_rag_chat():
    from app import agent as agents, main, rag as retrieval
    from app.schema import AgentSpec
    from app.store import Store

    async def drain(stream):
        return [event async for event in stream]

    def grounded(messages, index):
        context = next((m["content"] for m in messages if m.get("content", "").startswith("RAG:")), None)
        if context is None:
            return "Neutral final answer"
        hit = json.loads(context[context.index("\n") + 1:])[0]
        return json.dumps({"answer": "Neutral final answer [1]",
                           "citations": [{"source_id": 1, "quote": hit["text"]}]})

    calls = []
    def transport(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"model": body["model"], "data": [
            {"index": i, "embedding": [1, (len(text) % 7) + 1, 2]}
            for i, text in enumerate(body["input"])]})

    with tempfile.TemporaryDirectory(prefix="rag-chat-") as temp:
        root = Path(temp)
        fake_key = "offline-rag-context-key"
        source = root / "neutral.html"
        source.write_text('<article><h1>Neutral source</h1><p>' + ('Neutral material 42. ' * 80)
                          + fake_key + '</p></article>')
        ingest([{"path": str(source)}], root)
        config = EmbeddingConfig("http://127.0.0.1:8005/v1", "offline-embedding", dimensions=3)
        with httpx.Client(transport=httpx.MockTransport(transport)) as embedding:
            build_index(root, config, "fixed", client=embedding, size=128, overlap=16)
            metadata = Index(root).metadata()
            expected_hits = Index(root).search("neutral question", client=embedding)
            assert len(expected_hits) == 5
            # Strategies are metadata, not a restriction on query retrieval.
            for strategy in ("structural", "semantic"):
                def segmentation(docs, *args, **kwargs):
                    return [dict(c, strategy="semantic") for c in chunk_documents(docs, "fixed", 128, 16)], {"calls": 0}
                with patch("rag.index.semantic_chunks", segmentation):
                    built = build_index(root, config, strategy, client=embedding, size=128, overlap=16)
                found = Index(root).retrieve("neutral question", client=embedding)
                assert found["index"]["strategy"] == strategy and found["index"]["index_id"] == built["index_id"]
                assert len(found["hits"]) == 5 and all(hit["text"] for hit in found["hits"])
            build_index(root, config, "fixed", client=embedding, size=128, overlap=16)
            pinned = Index(root).metadata()
            replacement = {}
            def replace_during_embedding(request):
                replacement.update(build_index(root, config, "structural", client=embedding, size=128, overlap=16))
                return transport(request)
            with httpx.Client(transport=httpx.MockTransport(replace_during_embedding)) as racing:
                found = Index(root).retrieve("race query", top_k=20, client=racing)
            assert found["index"]["index_id"] == pinned["index_id"] != replacement["index_id"]
            assert found["index"]["strategy"] == "fixed" and all(hit["strategy"] == "fixed" for hit in found["hits"])
            assert len(found["hits"]) > 5

            real_retrieve = Index.retrieve
            def offline_retrieve(index, query, *args, **kwargs):
                return real_retrieve(index, query, *args, client=embedding, **kwargs)
            with patch("rag.index.storage_root", lambda: root), bind_connection(Connection(api_key=fake_key)), \
                 patch.object(Index, "retrieve", offline_retrieve), \
                 patch.object(agents, "stream_completion", _stub.make(grounded)):
                store = Store(str(root / "chat.db")).init()
                store.save_model_settings({"api_key": fake_key})
                agent = agents.Agent(AgentSpec(label="RAG", model="stub/model", rag_enabled=True, rag_rewrite_enabled=False), store=store)
                _stub.reset(); count = len(calls)
                events = asyncio.run(drain(agent.ask("neutral question")))
                start = next(e for e in events if e["type"] == "start")
                answer = agent.history[-1]; snapshot = copy.deepcopy(answer.rag)
                assert events[0]["type"] == "retrieval" and events[-1]["committed"]
                assert len(calls) == count + 1 and calls[-1]["input"] == ["neutral question"]
                assert start["rag_at"] == len(start["resolved_messages"]) - 2
                assert {k: v for k, v in snapshot.items() if k != "answer"} == start["rag"]
                assert snapshot == events[-1]["rag"]
                assert snapshot["context"] == _stub.CALLS[0]["payload"]["messages"][start["rag_at"]]["content"]
                assert answer.request_bodies[0] == _stub.CALLS[0]["payload"]
                assert fake_key not in json.dumps(snapshot) and len(snapshot["hits"]) == 3
                assert snapshot["config"]["candidates_k"] == 10 and snapshot["config"]["final_k"] == 3
                assert store.load_messages(agent.id)[-1]["rag"] == snapshot
                loaded = agents.Agent(agent.spec, agent_id=agent.id, store=store)
                assert loaded.spec.rag_enabled and loaded.history[-1].rag == snapshot
                original_bodies = copy.deepcopy(answer.request_bodies)
                carried = agent.carry_off(2)
                carried["history"][-1].rag["hits"][0]["text"] = "child mutation"
                carried["history"][-1].request_bodies[0]["messages"][0]["content"] = "child mutation"
                assert agent.history[-1].rag == snapshot and agent.history[-1].request_bodies == original_bodies
                assert store.load_messages(agent.id)[-1]["rag"] == snapshot
                # OFF never touches an index or external embedding boundary.
                agent.spec.rag_enabled = False
                with patch.object(agents, "rag_lookup", side_effect=AssertionError("OFF lookup")):
                    off = asyncio.run(drain(agent.ask("plain question")))
                assert off[0]["type"] == "start" and off[0]["rag_at"] is None and agent.history[-1].rag is None
                assert len(calls) == count + 1
                # Published deletion cannot alter historical copies.
                (root / "index.sqlite").unlink()
                assert loaded.history[-1].rag == snapshot
                old_exchange = loaded.take_last_exchange()
                rollback = asyncio.run(drain(main._regenerate_events(loaded, old_exchange)))
                assert rollback[-1]["restored"] and loaded.history[-1].rag == snapshot
                assert loaded.history[-1].request_bodies == original_bodies
                agent.spec.rag_enabled = True; _stub.reset()
                with patch.object(agent, "service_plan", return_value=["summary"]) as plan:
                    failed = asyncio.run(drain(agent.ask("missing index")))
                assert not plan.called, "retrieval failure must precede compression planning"
                assert [e["type"] for e in failed] == ["retrieval", "error", "done"]
                assert not failed[-1]["committed"] and failed[-1]["question"] == "missing index" and not _stub.CALLS
                assert len(agent.history) == 4
                taken = agent.take_last_exchange()
                restored = asyncio.run(drain(main._regenerate_events(agent, taken)))
                assert restored[-1]["restored"] and len(agent.history) == 4
                assert agent.history[-1].content == taken.turns[-1].content
                agent.forget(); assert not store.load_messages(agent.id)
                store.close()

            # Failure checks before any generation, including no compression calls.
            build_index(root, config, "fixed", client=embedding, size=128, overlap=16)
            for mode in ("stale", "model", "dimension", "http"):
                def bad(request):
                    if mode == "http": return httpx.Response(503, text="private upstream body")
                    body = json.loads(request.content)
                    return httpx.Response(200, json={"model": "other" if mode == "model" else body["model"],
                        "data": [{"index": 0, "embedding": [1,2] if mode == "dimension" else [1,2,3]}]})
                corpus = json.loads((root / "corpus.json").read_text())
                if mode == "stale":
                    (root / "corpus.json").write_text(json.dumps(dict(corpus, fingerprint="changed")))
                try:
                    with httpx.Client(transport=httpx.MockTransport(bad)) as broken:
                        try: Index(root).retrieve("bad query", client=broken)
                        except ValueError: pass
                        else: raise AssertionError(mode)
                finally:
                    (root / "corpus.json").write_text(json.dumps(corpus))
            try: Index(root).retrieve("query", config=replace(config, revision="other"), client=embedding)
            except ValueError: pass
            else: raise AssertionError("configuration mismatch accepted")
            db = sqlite3.connect(root / "index.sqlite")
            db.execute("DELETE FROM chunks"); db.commit(); db.close()
            count = len(calls)
            try: Index(root).retrieve("corrupt query", client=embedding)
            except ValueError as error: assert "row counts" in str(error)
            else: raise AssertionError("incomplete index accepted")
            assert len(calls) == count, "corrupt index must fail before query embedding"

        async def cancellation():
            ready, release = asyncio.Event(), asyncio.Event()
            async def lookup(query, **options):
                ready.set(); await release.wait(); return snapshot
            agent = agents.Agent(AgentSpec(label="cancel", model="stub/model", rag_enabled=True, rag_rewrite_enabled=False))
            with patch.object(agents, "rag_lookup", lookup), patch.object(agents, "stream_completion", _stub.make(grounded)):
                _stub.reset(); task = asyncio.create_task(drain(agent.ask("cancel query")))
                await ready.wait(); agent.cancel(); agent.spec.rag_enabled = False; release.set()
                result = await task
                assert result[-1]["cancelled"] and not result[-1]["committed"] and not _stub.CALLS and not agent.history
                # Toggle edits during lookup apply only to the next exchange.
                release.clear(); ready.clear(); agent.spec.rag_enabled = True
                task = asyncio.create_task(drain(agent.ask("inflight query")))
                await ready.wait(); agent.spec.rag_enabled = False; release.set(); result = await task
                assert result[-1]["committed"] and agent.history[-1].rag == snapshot
                assert not agent.spec.rag_enabled
                # Stop at compressing yield must prevent the paid call.
                agent.spec.rag_enabled = False
                with patch.object(agent, "service_plan", return_value=["summary"]), \
                     patch.object(agent, "compress", side_effect=AssertionError("paid after stop")):
                    stream = agent.ask("stop before compression")
                    assert (await anext(stream))["type"] == "compressing"
                    agent.cancel(); remainder = await drain(stream)
                    assert remainder[-1]["cancelled"] and not remainder[-1]["committed"]
        asyncio.run(cancellation())

        async def worker_cancellation():
            started, finish = threading.Event(), threading.Event()
            def blocked(query, **options):
                started.set()
                if not finish.wait(5):
                    raise TimeoutError("Neutral worker fixture was not released")
                return copy.deepcopy(snapshot)
            chat = agents.Agent(AgentSpec(label="worker", model="stub/model", rag_enabled=True, rag_rewrite_enabled=False))
            with patch.object(retrieval, "retrieve", blocked), patch.object(agents, "stream_completion", _stub.make()):
                _stub.reset(); task = asyncio.create_task(drain(chat.ask("worker query")))
                try:
                    assert await asyncio.to_thread(started.wait, 5)
                    chat.cancel(); finish.set(); result = await task
                    assert result[-1]["cancelled"] and not chat.history and not _stub.CALLS
                    started.clear(); finish.clear(); task = asyncio.create_task(drain(chat.ask("disconnected query")))
                    assert await asyncio.to_thread(started.wait, 5)
                    task.cancel()
                    try: await task
                    except asyncio.CancelledError: pass
                    else: raise AssertionError("task cancellation swallowed")
                    finish.set()
                    assert not chat.history and not _stub.CALLS
                finally:
                    finish.set()
        asyncio.run(worker_cancellation())

        # Actual LLM JSON/stream parser and independent HTTP MCP keep the same
        # canonical RAG context through all model/tool rounds.
        from app import llm, mcp
        from checks.mcp_url import service
        received = []
        def provider(request):
            body = json.loads(request.content); received.append(body)
            if len(received) == 1:
                delta = {"content": "Unverified intermediate draft", "tool_calls": [{"index": 0, "id": "ping-one", "type": "function",
                    "function": {"name": "ping", "arguments": '{"text":"neutral"}'}}]}
            else:
                delta = {"content": grounded(body["messages"], 0)}
            frames = [{"choices": [{"delta": delta}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]}]
            return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")
        with service("services.echo", temp) as (process, url):
            async def mcp_exchange():
                manager = mcp.McpManager()
                try:
                    await manager.configure([{"name": "neutral", "url": url, "enabled": True}], 0)
                    assert manager.servers[0].status == "ok", manager.servers[0].error
                    lookup_queries = []
                    async def lookup(query, **options):
                        lookup_queries.append(query)
                        return copy.deepcopy(snapshot)
                    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as provider_client:
                        with patch.object(agents, "MANAGER", manager), patch.object(agents, "rag_lookup", lookup), \
                             patch.object(agents, "stream_completion", llm.stream_completion), \
                             patch.object(llm, "shared_client", return_value=provider_client), \
                             patch.object(llm, "model_key", return_value="offline-provider-key"):
                            chat = agents.Agent(AgentSpec(label="tools", model="stub/model", rag_enabled=True, rag_rewrite_enabled=False))
                            result = await drain(chat.ask("tool question"))
                            assert result[-1]["committed"] and len(received) == 2
                            assert [e["text"] for e in result if e["type"] == "delta"] == ["Neutral final answer [1]"]
                            assert chat.history[-1].request_bodies == received and chat.history[-1].rag == snapshot
                            assert any(e["type"] == "tool_call" and e["result"] == "pong neutral" for e in result)
                            assert all(any(m.get("content") == snapshot["context"] for m in body["messages"]) for body in received)
                            assert received[-1]["messages"][-1]["role"] == "tool"
                            # ON must reject a provider override before lookup,
                            # compression or actual model traffic; regenerate
                            # restores the original snapshot instead of lying
                            # about context omitted from the outbound payload.
                            override = [{"role": "user", "content": "Neutral replacement"}]
                            chat.spec.extra_body = {"messages": override}
                            saved_answer = copy.deepcopy(chat.history[-1])
                            with patch.object(chat, "service_plan", side_effect=AssertionError("conflict started compression")):
                                rejected = await drain(chat.ask("conflicting request"))
                                taken = chat.take_last_exchange()
                                restored = await drain(main._regenerate_events(chat, taken))
                            assert [e["type"] for e in rejected] == ["error", "done"]
                            assert rejected[-1]["rag"] is None and not rejected[-1]["committed"]
                            assert rejected[-1]["question"] == "conflicting request" and "extra_body.messages" in rejected[-1]["error"]
                            assert restored[-1]["restored"] and chat.history[-1] == saved_answer
                            assert lookup_queries == ["tool question"] and len(received) == 2
                            off = agents.Agent(AgentSpec(label="legacy override", model="stub/model", extra_body={"messages": override}))
                            result = await drain(off.ask("original OFF question"))
                            assert result[-1]["committed"] and off.history[-1].rag is None
                            assert received[-1]["messages"] == override and off.history[-1].request_bodies == received[-1:]
                            assert lookup_queries == ["tool question"]
                finally:
                    await manager.stop()
            asyncio.run(mcp_exchange())
            assert process.poll() is None

        # Preparation failures expose completed service requests to scheduler
        # callers, and don't discard them on task cancellation.
        async def preparation_failure():
            chat = agents.Agent(AgentSpec(label="service", model="stub/model", rag_enabled=True, rag_rewrite_enabled=False))
            request_sink, rag_sink = [], {}
            async def lookup(query, **options): return copy.deepcopy(snapshot)
            async def compression(*args):
                llm.record_request({"messages": [{"role": "user", "content": "completed service"}]})
                raise ValueError("Neutral service failure")
            with patch.object(agents, "rag_lookup", lookup), patch.object(chat, "service_plan", return_value=["summary"]), \
                 patch.object(chat, "compress", compression):
                result = await drain(chat.ask("scheduled", scheduled={"id": 1, "server": "neutral"},
                                              request_bodies=request_sink, rag_result=rag_sink))
            assert result[-1]["committed"] is False and result[-1]["request_bodies"] == request_sink
            assert request_sink[0]["messages"][0]["content"] == "completed service"
            assert rag_sink == {k: v for k, v in snapshot.items() if k != "answer"}
            assert not chat.history
        asyncio.run(preparation_failure())

        # A still-valid reminder whose Agent.cancel flag was set while retrieval
        # awaited must not turn uncommitted done into a static failure message.
        from app.reminders import ReminderScheduler
        async def cancelled_reminder():
            store = Store(str(root / "cancel-reminder.db")).init()
            chat = agents.Agent(AgentSpec(label="cancel reminder", model="stub/model", rag_enabled=True, rag_rewrite_enabled=False), store=store)
            server = SimpleNamespace(name="neutral", status="ok", timeout_s=1)
            manager = SimpleNamespace(servers=[server], reminder_protocol=AsyncMock(return_value=True))
            scheduler = ReminderScheduler(manager, SimpleNamespace(store=store, allow_tools=True))
            item = {"id": 1, "text": "neutral", "context_id": "neutral"}
            scheduler.claims[(server.name, item["id"])] = {}
            ready, release = asyncio.Event(), asyncio.Event()
            async def lookup(query, **options): ready.set(); await release.wait(); return copy.deepcopy(snapshot)
            with patch.object(scheduler, "receipt", return_value=True), patch.object(agents, "rag_lookup", lookup):
                task = asyncio.create_task(scheduler._execute(server, item, "neutral token", chat))
                await ready.wait(); chat.cancel(); release.set(); await task
            assert not chat.history and not store.load_messages(chat.id)
            store.close()
        asyncio.run(cancelled_reminder())

        with TestClient(main.app) as client:
            response = client.post("/api/agents", json={"agent": {"label": "toggle", "model": "stub/model"}})
            item = response.json()["agents"][0]; aid = item["id"]
            assert item["rag_enabled"] is False
            route = f"/api/agents/{aid}"
            for bad in (None, 1, "true"):
                assert client.patch(route, json={"rag_enabled": bad}).status_code == 400
            assert client.patch(route, json={"rag_enabled": True, "rag_rewrite_enabled": False}).json()["rag_enabled"] is True
            assert main.REGISTRY.store.load_session(aid)["config"]["rag_enabled"] is True
            with patch.object(agents, "rag_lookup", side_effect=ValueError("Unavailable neutral index")):
                response = client.post(route + "/messages", json={"text": "restore input"})
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["event"] == "done" and not events[-1]["committed"] and events[-1]["question"] == "restore input"
            assert not main.REGISTRY.require(aid).busy and not main.REGISTRY.require(aid).history
            client.delete(route)

        # A legacy database gets nullable snapshots without deleting messages.
        legacy = root / "legacy.db"
        db = sqlite3.connect(legacy)
        db.execute('CREATE TABLE messages(session_id TEXT, seq INTEGER, role TEXT, content TEXT, error TEXT, metrics TEXT, request_bodies TEXT, at REAL, PRIMARY KEY(session_id,seq))')
        db.execute("INSERT INTO messages VALUES ('old',0,'assistant','legacy',NULL,NULL,NULL,1)")
        db.commit(); db.close()
        store = Store(str(legacy)).init()
        assert store.load_messages("old")[0]["rag"] is None and store._migrate() == []
        store.close()
    return "pinned generations/all strategies/top5; ON/OFF/payload/redaction/restart/fork; errors/done/restore/cancel/toggle/migration"
