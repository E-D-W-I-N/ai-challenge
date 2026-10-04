"""Offline day24 provenance, exact quotations and terminal lifecycle contracts."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from checks import _stub


def check_rag_citations():
    from app import agent as agents, main, rag
    from app.schema import AgentSpec
    from app.store import Store

    hits = [{"chunk_id": "neutral-first", "source": "fixture:first", "title": "First",
             "section": "Facts", "text": "The neutral item is blue.", "score": 0.3},
            {"chunk_id": "neutral-second", "source": "fixture:second", "title": "Second",
             "section": "Notes", "text": "The second item is green.", "score": 0.2}]
    def retrieved(index, query, top_k):
        return {"query": query, "index": {"index_id": "neutral-pinned"}, "hits": copy.deepcopy(hits)}
    valid = {"answer": "The second item is green [2].",
             "citations": [{"source_id": 2, "quote": "second item is green"}]}
    snapshot = {"hits": copy.deepcopy(hits)}
    text, proof = rag.validate_answer(json.dumps(valid), snapshot, {"finish_reason": "stop"})
    assert text == valid["answer"] and proof["citations"][0] == {
        "source_id": 2, "chunk_id": "neutral-second", "source": "fixture:second",
        "title": "Second", "section": "Notes", "quote": "second item is green"}
    invalids = [
        {**valid, "citations": []},
        {**valid, "citations": [{"source_id": True, "quote": "second item is green"}]},
        {**valid, "citations": [{"source_id": 3, "quote": "second item is green"}]},
        {**valid, "citations": [{"source_id": 2, "quote": "SECOND ITEM IS GREEN"}]},
        {**valid, "citations": [{"source_id": 2, "quote": " "}]},
        {**valid, "citations": valid["citations"] * 2},
        {**valid, "citations": [{**valid["citations"][0], "source": "invented"}]},
        {**valid, "answer": "The item is green [1]."},
        {**valid, "answer": "The item is green [" + "1" * 5000 + "]."},
        {**valid, "answer": "The item is green [02]."},
    ]
    for invalid in invalids:
        try:
            rag.validate_answer(json.dumps(invalid), snapshot, {"finish_reason": "stop"})
        except rag.CitationError:
            pass
        else:
            raise AssertionError("invalid provenance contract accepted")
    for finish in (None, "length", "tool_calls"):
        try:
            rag.validate_answer(json.dumps(valid), snapshot, {"finish_reason": finish})
        except rag.CitationError:
            pass
        else:
            raise AssertionError("incomplete final frame accepted")

    async def drain(stream):
        return [event async for event in stream]

    async def scenarios(root):
        store = Store(str(root / "neutral.sqlite")).init()
        spec = AgentSpec(label="citations", model="stub/model", rag_enabled=True,
                         rag_rewrite_enabled=False, rag_filter_enabled=False,
                         response_format={"type": "text"}, extra_body={"response_format": {"type": "text"}})
        chat = agents.Agent(spec, store=store)
        unchanged = copy.deepcopy(chat.spec)
        _stub.reset()
        with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make(json.dumps(valid), reasoning="neutral reasoning")):
            result = await drain(chat.ask("neutral request"))
        assert result[-1]["committed"] and result[-1]["text"] == valid["answer"]
        assert [e["text"] for e in result if e["type"] == "delta"] == [valid["answer"]]
        assert any(e["type"] == "reasoning" for e in result)
        assert chat.spec == unchanged
        body = chat.history[-1].request_bodies[0]
        assert body["response_format"] == {"type": "json_object"}
        assert chat.history[-1].rag["answer"] == proof
        assert len(chat.history[-1].rag["hits"]) == 2, "filterOFF must retain weak hits when gate passes"
        assert chat.history[-1].rag["answer_policy"] == {"weak_context_enabled": True, "similarity_threshold": 0.3}
        context = chat.history[-1].rag["context"]
        assert [h["source_id"] for h in json.loads(context.split("\n", 1)[1])] == [1, 2]
        before = copy.deepcopy(chat.history[-1])
        loaded = agents.Agent(chat.spec, agent_id=chat.id, store=store)
        assert loaded.history[-1].rag == before.rag
        branch = chat.carry_off(len(chat.history))
        branch["history"][-1].rag["answer"]["citations"][0]["quote"] = "mutated"
        assert chat.history[-1].rag == before.rag

        # A malformed result is not shown, committed or retried; actual paid diagnostics remain.
        for output in ("raw untrusted output", json.dumps(invalids[2]),
                       '{"answer":"x [1]","citations":[{"source_id":' + '1' * 5000 + ',"quote":"x"}]}',
                       '{"status":"unknown","status":"insufficient"}',
                       '[' * 1100 + '0' + ']' * 1100):
            _stub.reset()
            with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make(output)):
                failed = await drain(chat.ask("neutral invalid request"))
            assert len(_stub.CALLS) == 1 and len(chat.history) == 2
            assert not failed[-1]["committed"] and failed[-1]["question"] == "neutral invalid request"
            assert failed[-1]["metrics"]["cost_usd"] == 0.000123
            assert failed[-1]["request_bodies"] == [_stub.CALLS[0]["payload"]]
            assert not any(e["type"] == "delta" for e in failed)
        taken = chat.take_last_exchange()
        with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make("invalid")):
            restored = await drain(main._regenerate_events(chat, taken))
        assert restored[-1]["restored"] and chat.history[-1] == before

        # Only the exact explicit semantic refusal branch permits missing citations after model usage.
        _stub.reset()
        with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make('{"status":"insufficient"}')):
            refused = await drain(chat.ask("neutral semantic request"))
        assert refused[-1]["committed"] and refused[-1]["text"] == rag.NO_HITS
        assert refused[-1]["rag"]["answer"] == {"status": "insufficient", "reason": "model", "citations": []}
        assert len(_stub.CALLS) == 1 and len(chat.history[-1].request_bodies) == 1
        assert chat.history[-1].metrics["cost_usd"] == 0.000123

        # Inclusive gate always applies, independently of filtering, before compression/model lease.
        for filtering in (False, True):
            chat.spec.rag_filter_enabled = filtering
            chat.spec.rag_similarity_threshold = 0.31
            _stub.reset()
            with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", side_effect=AssertionError("weak context model")), \
                 patch.object(chat, "compress", side_effect=AssertionError("weak context compression")):
                refused = await drain(chat.ask("neutral weak request"))
            assert refused[-1]["committed"] and refused[-1]["text"] == rag.NO_HITS and not _stub.CALLS
            assert refused[-1]["rag"]["answer"] == {"status": "insufficient", "reason": "low_similarity", "citations": []}
            assert len(refused[-1]["rag"]["hits"]) == (0 if filtering else 2)
        chat.spec.rag_filter_enabled = False
        chat.spec.rag_similarity_threshold = 0.3

        # Cancellation at the newly buffered publication boundary cannot commit the answer.
        depth = len(chat.history)
        with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make(json.dumps(valid))):
            stream = chat.ask("neutral cancel request")
            async for event in stream:
                if event["type"] == "delta":
                    chat.cancel()
                if event["type"] == "done":
                    assert event["cancelled"] and not event["committed"]
        assert len(chat.history) == depth

        # A due reminder's malformed citations become an error assistant with paid diagnostics.
        from app.reminders import ReminderScheduler
        server = SimpleNamespace(name="fixture", status="ok", timeout_s=1)
        manager = SimpleNamespace(servers=[server], reminder_protocol=AsyncMock(return_value=True))
        scheduler = ReminderScheduler(manager, SimpleNamespace(store=store))
        item = {"id": 4, "text": "neutral", "context_id": "neutral"}
        scheduler.claims[(server.name, item["id"])] = {}
        _stub.reset()
        with patch.object(scheduler, "receipt", return_value=True), patch.object(rag.Index, "retrieve", retrieved), \
             patch.object(agents, "stream_completion", _stub.make(json.dumps(invalids[2]))):
            await scheduler._execute(server, item, "neutral token", chat)
        error = chat.history[-1]
        assert error.error and "проверки RAG-цитат" in error.error
        assert error.metrics["cost_usd"] == 0.000123 and error.metrics["reminder_execution"]["id"] == 4
        assert error.request_bodies == [_stub.CALLS[0]["payload"]]
        assert "answer" not in error.rag and error.rag["index"]["index_id"] == "neutral-pinned"
        store.close()

    with tempfile.TemporaryDirectory(prefix="rag-citations-") as temp:
        asyncio.run(scenarios(Path(temp)))
    return "strict source/quote IDs; buffered publication; fixed actual format; weakgate/filterOFF; failed usage/JSON; restore/restart/fork/cancel"
