"""Day17: real Agent/LLM tool rounds against an independent HTTP Git service."""
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

from app import agent, llm, mcp
from app.schema import AgentSpec
from app.store import Store
from checks.mcp_url import service


def check_git_exchange():
    with tempfile.TemporaryDirectory(prefix="git-http-") as tmp:
        repository = Path(tmp) / "independent repository"
        repository.mkdir()
        command = ["git", "-C", str(repository), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]
        subprocess.run([*command, "init", "-q"], check=True, capture_output=True)
        tracked = repository / "tracked.txt"
        for text, subject in [("first\n", "fixture first"), ("second\n", "fixture second")]:
            tracked.write_text(text)
            subprocess.run([*command, "add", "tracked.txt"], check=True, capture_output=True)
            subprocess.run([*command, "commit", "-qm", subject], check=True, capture_output=True)
        tracked.write_text("working change\n")
        expected_log = subprocess.run([*command, "log", "-1", "--format=%h %ad %s", "--date=short"], check=True, capture_output=True, text=True).stdout.strip()
        expected_status = subprocess.run([*command, "status", "--porcelain"], check=True, capture_output=True, text=True).stdout.strip()
        path = str(Path(tmp) / "app.sqlite")
        store = Store(path).init()
        spec = AgentSpec(label="git rounds", model="fixture/any-model", temperature=.37, max_tokens=17, extra_body={"seed": 42, "provider": {"order": ["fixture"]}})
        chat = agent.Agent(spec, store=store)
        received = []
        fake_key = "fixture-provider-secret-only"

        def provider(request):
            received.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer " + fake_key
            round_index = len(received) - 1
            assert round_index < 3, "agent repeated the tool exchange unexpectedly"
            if round_index < 2:
                name, arguments = [("git_log", '{"n":1}'), ("git_status", "{}")][round_index]
                # Exercise the actual streaming parser, including split arguments.
                frames = [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"call_{round_index}", "type": "function", "function": {"name": name, "arguments": arguments[:1]}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[1:]}}]}}]},
                ]
                finish = "tool_calls"
            else:
                frames = [{"choices": [{"delta": {"content": "final <script>text</script>"}}]}]
                finish = "stop"
            usage = [
                {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22, "cost": .001},
                {"prompt_tokens": 23, "completion_tokens": 7, "total_tokens": 30, "cost": .002},
                {"prompt_tokens": 31, "completion_tokens": 11, "total_tokens": 42, "cost": .003},
            ][round_index]
            frames.append({"choices": [{"delta": {}, "finish_reason": finish}], "usage": usage})
            return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")

        with service("services.git", tmp, "--repo", str(repository)) as (external, url):
            async def scenario():
                manager = mcp.McpManager()
                try:
                    await manager.configure([{"name": "chosen-name", "url": url, "enabled": True}], 0)
                    server = manager.servers[0]
                    assert server.status == "ok", server.error
                    assert server.process is None and set(manager.tools) == {"git_log", "git_status", "git_diff_stat"}
                    schemas = {tool["name"]: tool["schema"] for tool in server.view}
                    assert schemas["git_log"]["properties"]["n"]["type"] == "integer"
                    assert schemas["git_diff_stat"]["properties"]["ref"]["type"] == "string"
                    diff = await manager.call("git_diff_stat", {})
                    assert not diff.isError and "tracked.txt" in diff.content[0].text
                    bad_ref = await manager.call("git_diff_stat", {"ref": "fixture-missing-revision"})
                    assert "fixture-missing-revision" in bad_ref.content[0].text
                    option = await manager.call("git_diff_stat", {"ref": "--help"})
                    assert option.isError
                    session = server.session
                    events = []
                    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                        with patch.object(agent, "MANAGER", manager), patch.object(llm, "shared_client", return_value=client), patch.dict(os.environ, {"OPENROUTER_API_KEY": fake_key, "RAG_EMBEDDING_API_KEY": ""}), patch.object(llm, "attribution_headers", return_value={}), patch.object(agent, "stream_completion", llm.stream_completion):
                            exchange = chat.ask("Read this repository: <script>question</script> " + fake_key)
                            async for event in exchange:
                                events.append(event)
                                if event["type"] == "tool_call":
                                    break
                            assert events[-1]["name"] == "git_log" and events[-1]["result"] == expected_log
                            disconnect = asyncio.create_task(manager.connection("chosen-name", 1, False))
                            await asyncio.sleep(.03)
                            assert not disconnect.done() and server.session is session
                            # Resume in the same task: the whole exchange owns the
                            # lease, and nested manager.call must not deadlock.
                            async for event in exchange:
                                events.append(event)
                                if event["type"] == "tool_call":
                                    assert not disconnect.done() and server.session is session
                            await disconnect
                    assert not manager.tools and server.session is None
                    assert external.poll() is None, "disconnect killed the independent Git service"
                    assert events[-1]["committed"] and events[-1]["answer_index"] == 1 and events[-1]["error"] is None, events[-1]
                    assert len(received) == 3 and chat.history[-1].request_bodies == received
                    for body in received:
                        assert body["temperature"] == .37 and body["max_tokens"] == 17 and body["seed"] == 42
                        assert body["provider"] == {"require_parameters": True, "order": ["fixture"]}
                        assert {tool["function"]["name"] for tool in body["tools"]} == set(schemas)
                        assert body["usage"] == {"include": True} and body["stream"] is True
                        assert body["plugins"] == [{"id": "context-compression", "enabled": False}]
                    assert not any(message["role"] == "tool" for message in received[0]["messages"])
                    assert received[1]["messages"][-2]["tool_calls"][0]["function"] == {"name": "git_log", "arguments": '{"n":1}'}
                    assert received[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call_0", "content": expected_log}
                    assert received[2]["messages"][:-2] == received[1]["messages"]
                    assert received[2]["messages"][-2]["tool_calls"][0]["function"]["name"] == "git_status"
                    assert received[2]["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": expected_status}
                    runs = chat.history[-1].metrics["tool_calls"]
                    assert [(run["name"], run["server"], run["ok"]) for run in runs] == [("git_log", "chosen-name", True), ("git_status", "chosen-name", True)]
                    metrics = chat.usage_summary()
                    assert (metrics["prompt_tokens"], metrics["completion_tokens"], metrics["total_tokens"]) == (71, 23, 94)
                    assert abs(metrics["cost_usd"] - .006) < 1e-9
                    assert fake_key not in json.dumps(received) and "authorization" not in json.dumps(received).lower()
                    snapshot = deepcopy(received)
                    spec.temperature = .99
                    spec.extra_body.clear()
                    assert chat.transcript()[-1]["request_bodies"] == snapshot
                finally:
                    await manager.stop()
                assert external.poll() is None, "shutdown killed the independent Git service"
            asyncio.run(scenario())
        identity = chat.id
        store.close()
        reopened = Store(path).init()
        restored = agent.Agent(AgentSpec(label="fallback", model="ignored"), store=reopened, agent_id=identity)
        assert restored.transcript()[-1]["request_bodies"] == received
        assert [turn["role"] for turn in restored.transcript()] == ["user", "assistant"]
        assert fake_key not in json.dumps(restored.transcript())
        rendered = subprocess.run(
            ["node", str(mcp.ROOT / "checks" / "request_info.js")],
            input=json.dumps({"transcript": restored.transcript(), "received": received}),
            capture_output=True, text=True, cwd=mcp.ROOT,
        )
        assert rendered.returncode == 0, rendered.stdout + rendered.stderr
        reopened.close()
    return "independent Git initialize/list/call with its own repository; real 3-round Agent/LLM bodies equal SQLite and actual client info; summed usage; disconnect waits for full exchange and leaves service alive"
