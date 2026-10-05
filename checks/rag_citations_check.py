"""Offline day24 provenance, exact quotations and terminal lifecycle contracts."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile

import httpx
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from checks import _stub


def check_rag_citations():
    from app import agent as agents, main, rag, llm
    from app.schema import AgentSpec
    from app.store import Store

    hits = [{"chunk_id": "neutral-first", "source": "fixture:first", "title": "First",
             "section": "Facts", "text": "The neutral item is blue.", "score": 0.3},
            {"chunk_id": "neutral-second", "source": "fixture:second", "title": "Second",
             "section": "Notes", "text": "The second item is green.", "score": 0.2}]
    def retrieved(index, query, top_k, **options):
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
                         rag_rewrite_enabled=False, reasoning_enabled=True,
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
        assert len(chat.history[-1].rag["hits"]) == 2, "selection must retain weak hits when gate passes"
        assert chat.history[-1].rag["answer_policy"] == {"weak_context_enabled": True, "similarity_threshold": 0.3, "context_scope": "selected", "gate_stage": "selected"}
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

        # All weak candidates skip paid rerank and mark the cosine selection provisional.
        chat.spec.rag_rerank_enabled = True
        chat.spec.rag_similarity_threshold = 0.31
        _stub.reset()
        with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", side_effect=AssertionError("weak context model")), \
             patch.object(chat, "compress", side_effect=AssertionError("weak context compression")):
            refused = await drain(chat.ask("neutral weak request"))
        assert refused[-1]["committed"] and refused[-1]["text"] == rag.NO_HITS and not _stub.CALLS
        weak = refused[-1]["rag"]
        assert weak["answer"] == {"status": "insufficient", "reason": "low_similarity", "citations": []}
        assert len(weak["hits"]) == 2 and weak["answer_policy"]["gate_stage"] == "candidates"
        assert weak["selection"]["ordering"] == "cosine" and "rerank" not in weak
        chat.spec.rag_similarity_threshold = 0.3
        chat.spec.rag_rerank_enabled = False

        # Reordering assigns citation IDs from final context order for both providers.
        for provider in ("openrouter", "compatible"):
            ranked = agents.Agent(AgentSpec(label="ranked citations", model="neutral-final", provider=provider,
                rag_enabled=True, rag_rewrite_enabled=False,
                reasoning_enabled=True, rag_rerank_enabled=True, rag_rerank_provider=provider, rag_rerank_model="neutral-ranker"), store=store)
            # A discarded strong candidate cannot authorize the selected weak context.
            ranked.spec.rag_final_k = 1
            _stub.reset()
            def weak_selection_reply(messages, index):
                assert index == 0, "weak selected context must not call final model"
                return '{"source_ids":[2,1]}'
            with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make(weak_selection_reply)):
                refused = await drain(ranked.ask("neutral discarded strong request"))
            assert refused[-1]["committed"] and refused[-1]["text"] == rag.NO_HITS
            assert len(_stub.CALLS) == 1 and refused[-1]["metrics"]["total_tokens"] == 100
            assert refused[-1]["metrics"]["cost_usd"] == .000123
            assert refused[-1]["rag"]["answer_policy"]["gate_stage"] == "selected"
            assert [h["chunk_id"] for h in refused[-1]["rag"]["hits"]] == ["neutral-second"]
            assert refused[-1]["rag"]["selection"]["selected_source_ids"] == [2]
            assert ranked.history[-1].request_bodies == [_stub.CALLS[0]["payload"]]
            assert len(json.loads(_stub.CALLS[0]["messages"][-1]["content"])["sources"]) == 2
            # Retaining a strong source permits a promoted low-cosine citation in final order.
            ranked.spec.rag_final_k = 2
            ordered_answer = {"answer": "The second item is green [1].",
                              "citations": [{"source_id": 1, "quote": "second item is green"}]}
            _stub.reset()
            def ranked_reply(messages, index):
                return '{"source_ids":[2,1]}' if index == 0 else json.dumps(ordered_answer)
            with patch.object(rag.Index, "retrieve", retrieved), patch.object(agents, "stream_completion", _stub.make(ranked_reply)):
                completed = await drain(ranked.ask("neutral rank request"))
            assert completed[-1]["committed"] and completed[-1]["metrics"]["total_tokens"] == 200
            saved = ranked.history[-1]
            sources = json.loads(saved.rag["context"].split("\n", 1)[1])
            assert [(h["source_id"], h["chunk_id"]) for h in sources] == [(1, "neutral-second"), (2, "neutral-first")]
            assert saved.rag["answer"]["citations"][0]["chunk_id"] == "neutral-second"
            assert saved.rag["answer"]["citations"][0]["source_id"] == 1
            assert saved.rag["context"] == _stub.CALLS[-1]["messages"][-2]["content"]
            assert saved.request_bodies == [call["payload"] for call in _stub.CALLS]
            assert len(saved.rag["hits"]) == 2 and saved.rag["rerank"]["source_ids"] == [2, 1]
            assert ("provider" in saved.request_bodies[0]) == (provider == "openrouter")

        # Real transport + Agent: ON accepts grounded text; OFF violations never publish RAG.
        for provider in ("openrouter", "compatible"):
            actual = agents.Agent(AgentSpec(label="actual reasoning citations", model="neutral/model", provider=provider,
                reasoning_enabled=True, rag_enabled=True, rag_rewrite_enabled=False), store=store)
            mode = {"value": "on"}; dispatched = []
            def respond(request):
                dispatched.append(json.loads(request.content))
                if mode["value"] == "tools":
                    frames = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "neutral-call", "function": {"name": "neutral_tool", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]}]
                else:
                    frames = [{"choices": [{"delta": {"content": json.dumps(valid)}, "finish_reason": "stop"}]}]
                    if mode["value"] in ("on", "early"):
                        frames.insert(0, {"choices": [{"delta": {"reasoning_content": "neutral reasoning"}}]})
                frames.append({"choices": [], "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100,
                    "cost": .000123, "completion_tokens_details": {"reasoning_tokens": 5}}})
                return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")
            declaration = [{"type": "function", "function": {"name": "neutral_tool", "description": "neutral fixture", "parameters": {"type": "object", "properties": {}}}}]
            never_run = AsyncMock(side_effect=AssertionError("OFF violation must not execute MCP"))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with patch.object(llm, "shared_client", return_value=client), patch.object(llm, "api_key", return_value="neutral-fixture-key"), \
                     patch.object(agents, "stream_completion", llm.stream_completion), patch.object(rag.Index, "retrieve", retrieved), \
                     patch.object(agents, "declared_tools", return_value=declaration), patch.object(agents, "run_tool", never_run):
                    completed = await drain(actual.ask("neutral actual grounded request"))
                    assert completed[-1]["committed"] and completed[-1]["text"] == valid["answer"]
                    assert [event["text"] for event in completed if event["type"] == "delta"] == [valid["answer"]]
                    assert any(event["type"] == "reasoning" for event in completed)
                    before_actual = copy.deepcopy(actual.history[-1])
                    actual.spec.reasoning_enabled = False
                    for violation in ("early", "late", "tools"):
                        mode["value"] = violation; dispatched.clear()
                        failed = await drain(actual.ask("neutral actual violation"))
                        assert not failed[-1]["committed"] and len(actual.history) == 2
                        assert not any(event["type"] in ("delta", "tool_call") for event in failed)
                        assert failed[-1]["metrics"]["total_tokens"] == 100 and failed[-1]["metrics"]["cost_usd"] == .000123
                        assert failed[-1]["request_bodies"] == dispatched and len(dispatched) == 1
                        assert dispatched[0]["tools"] == declaration and "answer" not in failed[-1]["rag"]
                        assert (dispatched[0].get("reasoning", {}).get("effort") or dispatched[0].get("reasoning_effort")) == "none"
                    mode["value"] = "late"
                    taken = actual.take_last_exchange()
                    restored = await drain(main._regenerate_events(actual, taken))
                    assert restored[-1]["restored"] and actual.history[-1] == before_actual
                    never_run.assert_not_awaited()

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
        # Scheduler timeout after paid rerank retains its actual usage and requests.
        from app import reminders
        chat.spec.rag_rerank_enabled = True
        chat.spec.rag_rerank_model = "neutral-ranker"
        _stub.reset()
        async def ranking_then_timeout(session, **options):
            if session.model == "neutral-ranker":
                async for event in _stub.make('{"source_ids":[2,1]}')(session, **options):
                    yield event
            else:
                # A cumulative usage update is observed twice but charged once.
                stub = _stub.make(json.dumps(valid))
                async for event in stub(session, **options):
                    if event["type"] == "metrics":
                        yield event
                        yield copy.deepcopy(event)
                        await asyncio.sleep(10)
                    else:
                        yield event
        with patch.object(scheduler, "receipt", return_value=True), patch.object(rag.Index, "retrieve", retrieved), \
             patch.object(agents, "stream_completion", ranking_then_timeout), patch.object(reminders, "RUN_SECONDS", .05):
            await scheduler._execute(server, item, "neutral timeout token", chat)
        timeout_error = chat.history[-1]
        assert timeout_error.error and "TimeoutError" in timeout_error.error
        assert timeout_error.metrics["total_tokens"] == 200 and timeout_error.metrics["cost_usd"] == .000246
        assert timeout_error.rag["rerank"]["source_ids"] == [2, 1] and "answer" not in timeout_error.rag
        assert timeout_error.request_bodies == [call["payload"] for call in _stub.CALLS] and len(_stub.CALLS) == 2
        store.close()

    with tempfile.TemporaryDirectory(prefix="rag-citations-") as temp:
        asyncio.run(scenarios(Path(temp)))
    return "strict source/quote IDs; buffered publication; fixed actual format; weakgate/filterOFF; failed usage/JSON; restore/restart/fork/cancel"
