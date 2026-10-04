"""Day19: standalone HTTP pipeline, real provider JSON and persisted client info."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import agent, llm, main, mcp
from app.schema import AgentSpec
from app.store import Store
from checks.mcp_url import service


def check_pipeline_http():
    with tempfile.TemporaryDirectory(prefix="pipeline-http-") as tmp:
        repo = Path(tmp) / "selected repository"
        output = Path(tmp) / "selected output"
        repo.mkdir()
        output.mkdir()
        outside = Path(tmp) / "outside sentinel.txt"
        sentinel = b"outside sentinel must remain unchanged\n"
        outside.write_bytes(sentinel)
        linked = output / "linked.txt"
        linked.symlink_to(outside)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / ".gitignore").write_text("/ignored.txt\n")
        (repo / "a.txt").write_text("\nneedle alpha2\n" + "\n" * 7 + "Needle alpha10\n")
        (repo / "b.txt").write_text("needle beta1\n")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore", "a.txt", "b.txt"], check=True, capture_output=True)
        (repo / "z.txt").write_text("NEEDLE untracked\n")
        (repo / "ignored.txt").write_text("needle ignored\n")
        expected_search = "a.txt:2:needle alpha2\na.txt:10:Needle alpha10\nb.txt:1:needle beta1\nz.txt:1:NEEDLE untracked"
        expected_summary = "a.txt — 2 строки:\n  a.txt:2:needle alpha2\n  a.txt:10:Needle alpha10\n\nb.txt — 1 строка:\n  b.txt:1:needle beta1\n\nz.txt — 1 строка:\n  z.txt:1:NEEDLE untracked"
        name = "цепочка.txt"
        saved_result = f"записано: {output.resolve() / name} ({len(expected_summary.encode('utf-8'))} байт)"
        received = []
        fake_key = "fixture-provider-secret-only"
        plan = ["search", "summarize", "save_file"]

        def provider(request):
            body = json.loads(request.content)
            received.append(body)
            assert request.headers["authorization"] == "Bearer " + fake_key
            index = len(received) - 1
            assert index < 4, "pipeline repeated the model exchange unexpectedly"
            if index < 3:
                if index == 0:
                    args = {"query": "needle", "limit": 5}
                else:
                    previous = body["messages"][-1]
                    assert previous["role"] == "tool"
                    args = {"text": previous["content"], "max_items": 3} if index == 1 else {"name": name, "content": previous["content"]}
                arguments = json.dumps(args, ensure_ascii=False)
                frames = [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"call_{index + 1}", "type": "function", "function": {"name": plan[index], "arguments": arguments[:2]}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[2:]}}]}}]},
                ]
                finish = "tool_calls"
            else:
                assert body["messages"][-1]["content"] == saved_result, body["messages"][-1]
                frames = [{"choices": [{"delta": {"content": "saved: " + saved_result}}]}]
                finish = "stop"
            prompt, completion = [(11, 3), (17, 5), (23, 7), (29, 11)][index]
            frames.append({"choices": [{"delta": {}, "finish_reason": finish}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion, "cost": .001 * (index + 1)}})
            return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")

        path = str(Path(tmp) / "app.sqlite")
        store = Store(path).init()
        manager = mcp.McpManager()
        with service("services.pipeline", tmp, "--repo", str(repo), "--files-dir", str(output)) as (external, url):
            with patch.object(main.REGISTRY, "store", store), patch.object(mcp, "MANAGER", manager), patch.object(agent, "MANAGER", manager), patch.object(mcp, "DEFAULT_CONFIG_PATH", Path(tmp) / "missing.json"):
                with TestClient(main.app) as api:
                    # The exact save/connect routes used by the Tools URL form.
                    answer = api.put("/api/mcp/config", json={"revision": 0, "servers": [{"name": "chosen-pipeline", "url": url, "enabled": False}]})
                    assert answer.status_code == 200 and answer.json()["servers"][0]["status"] == "disconnected", answer.text
                    connected = api.post("/api/mcp/connect", json={"revision": 1, "name": "chosen-pipeline"})
                    assert connected.status_code == 200, connected.text
                    public = connected.json()["servers"][0]
                    assert public["status"] == "ok" and {item["name"] for item in public["tools"]} == set(plan), public
                    server = manager.servers[0]
                    assert server.process is None
                    schemas = {item["name"]: item["schema"] for item in public["tools"]}
                    assert all(item["description"] and item["schema"]["properties"] for item in public["tools"])
                    created = api.post("/api/agents", json={"agent": {"label": "pipeline capture", "model": "fixture/any-model", "temperature": .37, "max_tokens": 31, "extra_body": {"seed": 42}}})
                    assert created.status_code == 200, created.text
                    identity = created.json()["agents"][0]["id"]
                    chat = main.REGISTRY.load(identity)

                    async def exchange():
                        # Actual HTTP calls must replace this output entry,
                        # keeping its outside target intact, then overwrite normally.
                        for content in ["replacement — only inside output\n", "ordinary overwrite\n"]:
                            saved = await manager.call("save_file", {"name": linked.name, "content": content})
                            assert not saved.isError, saved
                            assert saved.content[0].text == f"записано: {output.resolve() / linked.name} ({len(content.encode('utf-8'))} байт)", saved
                            assert outside.read_bytes() == sentinel
                            assert not linked.is_symlink() and linked.read_bytes() == content.encode("utf-8")
                        session = server.session
                        events = []
                        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                            with patch.object(llm, "shared_client", return_value=client), patch.object(llm, "api_key", return_value=fake_key), patch.object(llm, "attribution_headers", return_value={}), patch.object(agent, "stream_completion", llm.stream_completion):
                                stream = chat.ask("Find and save <script>question</script>")
                                async for event in stream:
                                    events.append(event)
                                    if event["type"] == "tool_call":
                                        break
                                assert events[-1]["name"] == "search" and events[-1]["result"] == expected_search
                                disconnect = asyncio.create_task(manager.connection("chosen-pipeline", 2, False))
                                await asyncio.sleep(.03)
                                assert not disconnect.done() and server.session is session
                                async for event in stream:
                                    events.append(event)
                                    if event["type"] == "tool_call":
                                        assert not disconnect.done() and server.session is session
                                await disconnect
                        assert events[-1]["committed"] and events[-1]["error"] is None, events[-1]
                        assert [event["name"] for event in events if event["type"] == "tool_call"] == plan
                        assert [event["result"] for event in events if event["type"] == "tool_call"] == [expected_search, expected_summary, saved_result]
                        assert not manager.tools and external.poll() is None
                    api.portal.call(exchange)
                    assert len(received) == 4
                    bodies = api.get(f"/api/agents/{identity}").json()["transcript"][-1]["request_bodies"]
                    assert bodies == received
                    assert [sum(message["role"] == "tool" for message in body["messages"]) for body in received] == [0, 1, 2, 3]
                    for body in received:
                        assert body["temperature"] == .37 and body["max_tokens"] == 31 and body["seed"] == 42
                        assert {item["function"]["name"]: item["function"]["parameters"] for item in body["tools"]} == schemas
                        assert body["stream"] is True and body["usage"] == {"include": True}
                        assert body["provider"]["require_parameters"] is True
                    assert received[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": expected_search}
                    assert json.loads(received[2]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {"text": expected_search, "max_items": 3}
                    assert json.loads(received[3]["messages"][-2]["tool_calls"][0]["function"]["arguments"]) == {"name": name, "content": expected_summary}
                    assert (output / name).read_bytes() == expected_summary.encode("utf-8")
                    assert {item.name for item in output.iterdir()} == {name, linked.name}
                    assert fake_key not in json.dumps(bodies) and "authorization" not in json.dumps(bodies).lower()
                    metrics = chat.usage_summary()
                    assert (metrics["prompt_tokens"], metrics["completion_tokens"], metrics["total_tokens"], metrics["cost_usd"]) == (80, 26, 106, .01), metrics
                    assert [(run["name"], run["server"], run["ok"]) for run in chat.history[-1].metrics["tool_calls"]] == [(tool, "chosen-pipeline", True) for tool in plan]
                    snapshot = deepcopy(received)
                    changed = api.patch(f"/api/agents/{identity}", json={"temperature": .99})
                    assert changed.status_code == 200, changed.text
                    assert api.get(f"/api/agents/{identity}").json()["transcript"][-1]["request_bodies"] == snapshot
            assert external.poll() is None, "application shutdown killed the independent pipeline service"
        store.close()
        reopened = Store(path).init()
        restored = agent.Agent(AgentSpec(label="fallback", model="ignored"), store=reopened, agent_id=identity)
        assert restored.transcript()[-1]["request_bodies"] == received
        assert [turn["role"] for turn in restored.transcript()] == ["user", "assistant"]
        rendered = subprocess.run(["node", str(mcp.ROOT / "checks" / "request_info.js")], input=json.dumps({"transcript": restored.transcript(), "received": received}), capture_output=True, text=True, cwd=mcp.ROOT, timeout=15)
        assert rendered.returncode == 0, rendered.stdout + rendered.stderr
        reopened.close()
    return "manual pipeline HTTP URL save/connect/list; symlink replaced and ordinary overwrite keeps outside sentinel intact; real 4-round provider bodies equal SQLite/restart/Node info; result bytes and usage; whole-exchange disconnect keeps external service alive"
