"""Compact offline checks using separately launched Streamable HTTP services."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app import agent, llm, main, mcp
from app.schema import AgentSpec
from app.store import Store


@contextlib.contextmanager
def service(module, folder, *extra_args):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with open(Path(folder) / (module + ".log"), "w") as log:
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        proc = subprocess.Popen([sys.executable, "-m", module, "--port", str(port), *extra_args], cwd=mcp.ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                if proc.poll() is not None:
                    raise AssertionError(f"{module} exited: {proc.returncode}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    if time.monotonic() > deadline:
                        raise AssertionError("HTTP service startup timeout")
                    time.sleep(.03)
            yield proc, f"http://127.0.0.1:{port}/mcp"
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def check_url_api():
    with tempfile.TemporaryDirectory(prefix="mcp-url-") as tmp, service("services.echo", tmp) as (external, url):
        db = str(Path(tmp) / "app.sqlite")
        store = Store(db).init()
        manager = mcp.McpManager()
        with patch.object(main.REGISTRY, "store", store), patch.object(mcp, "MANAGER", manager), patch.object(mcp, "DEFAULT_CONFIG_PATH", Path(tmp) / "missing.json"):
            with TestClient(main.app) as client:
                initial = client.get("/api/mcp").json()
                assert initial["servers"] == [] and initial["config"] == {"revision": 0, "servers": []}
                for invalid in ["stdio://echo", "http://u:p@localhost/mcp", "http://localhost/mcp?key=secret", "http://localhost/mcp#fragment", "http://localhost:bad/mcp", "http://localhost:0/mcp", "http://localhost/ a"]:
                    answer = client.put("/api/mcp/config", json={"revision": 0, "servers": [{"name": "custom", "url": invalid}]})
                    assert answer.status_code == 400, (invalid, answer.text)
                duplicate = [{"name": "one", "url": url}, {"name": "two", "url": url}]
                assert client.put("/api/mcp/config", json={"revision": 0, "servers": duplicate}).status_code == 400
                assert client.put("/api/mcp/config", json={"revision": False, "servers": []}).status_code == 409
                rows = [{"name": "my-service", "url": url, "enabled": False}]
                saved = client.put("/api/mcp/config", json={"revision": 0, "servers": rows}).json()
                assert saved["config"] == {"revision": 1, "servers": rows} and saved["servers"][0]["status"] == "disconnected"
                assert client.put("/api/mcp/config", json={"revision": 0, "servers": []}).status_code == 409
                connected = client.post("/api/mcp/connect", json={"revision": 1, "name": "my-service"}).json()
                assert connected["servers"][0]["status"] == "ok", connected
                assert connected["servers"][0]["tools"][0]["name"] == "ping"
                assert "text" in connected["servers"][0]["tools"][0]["schema"]["properties"]
                assert manager.servers[0].process is None
                result = client.portal.call(manager.call, "ping", {"text": "real HTTP"})
                assert result.content[0].text == "pong real HTTP"
            assert external.poll() is None, "application shutdown killed the independent service"
        store.close()
        reopened = Store(db).init()
        manager = mcp.McpManager()
        with patch.object(main.REGISTRY, "store", reopened), patch.object(mcp, "MANAGER", manager), patch.object(mcp, "DEFAULT_CONFIG_PATH", Path(tmp) / "missing.json"):
            with TestClient(main.app) as client:
                restored = client.get("/api/mcp").json()
                assert restored["config"]["revision"] == 2 and restored["servers"][0]["status"] == "ok", restored
                disconnected = client.post("/api/mcp/disconnect", json={"revision": 2, "name": "my-service"}).json()
                assert disconnected["servers"][0]["status"] == "disconnected" and not manager.tools
                assert external.poll() is None
                # Endpoint is HTTP but is not MCP: useful failure, saved URL and
                # revision stay available, then explicit correction/reconnect.
                wrong = url.replace("/mcp", "/missing")
                failed = client.put("/api/mcp/config", json={"revision": 3, "servers": [{"name": "other", "url": wrong, "enabled": True}]}).json()
                assert failed["servers"][0]["status"] == "down" and failed["servers"][0]["error"], failed
                fixed = client.put("/api/mcp/config", json={"revision": 4, "servers": [{"name": "other", "url": url, "enabled": True}]}).json()
                assert fixed["servers"][0]["status"] == "ok" and list(manager.tools) == ["ping"], fixed
                assert client.post("/api/mcp/disconnect", json={"revision": 2, "name": "my-service"}).status_code == 409
                writer = Store(db).init()
                cross_process = writer.save_mcp_config([{ "name": "shared", "url": url, "enabled": True }], 5)
                writer.close()
                synced = client.get("/api/mcp").json()
                assert synced["config"] == cross_process and synced["servers"][0]["name"] == "shared" and synced["servers"][0]["status"] == "ok"
                assert client.put("/api/mcp/config", json={"revision": 5, "servers": []}).status_code == 409
                reopened.clear()
                assert reopened.load_mcp_config()["revision"] == 6, "chat reset erased app-wide URLs"
            assert external.poll() is None
        reopened.close()
    return "separate echo initialize/list/call; validation/save/reopen/down/reconnect; external process survives disconnect and shutdown"


def check_url_race():
    with tempfile.TemporaryDirectory(prefix="mcp-race-") as tmp, service("checks._mcp_http", tmp) as (external, url):
        async def scenario():
            manager = mcp.McpManager()
            with patch.object(mcp, "DEFAULT_CONFIG_PATH", Path(tmp) / "none"):
                await manager.start()
                await manager.configure([{"name": "custom", "url": url, "enabled": True}], 0)
                server = manager.servers[0]
                assert server.status == "ok", server.error
                assert "host_probe" not in manager.tools and any(t.name == "host_probe" for t in server.tools)
                old_session = server.session
                entered, release = asyncio.Event(), asyncio.Event()
                original = old_session.call_tool

                async def foreground(*args):
                    entered.set()
                    await release.wait()
                    return await original(*args)

                with patch.object(old_session, "call_tool", foreground):
                    call = asyncio.create_task(manager.call("ping", {"text": "original session", "delay": .1}))
                    await entered.wait()
                    change = asyncio.create_task(manager.configure([{"name": "replacement", "url": url, "enabled": True}], 1))
                    await asyncio.sleep(.03)
                    assert not change.done() and server.session is old_session
                    release.set()
                    result = await call
                    assert result.content[0].text == "pong original session"
                    await change
                    assert manager.servers[0].name == "replacement" and manager.servers[0].session is not old_session
                # Lease protects the gap between model rounds as well as the
                # individual MCP call, without deadlocking nested call().
                async with manager.lease():
                    assert (await manager.call("ping", {"text": "round 1"})).content[0].text == "pong round 1"
                    close = asyncio.create_task(manager.connection("replacement", 2, False))
                    await asyncio.sleep(.03)
                    assert not close.done()
                    assert (await manager.call("ping", {"text": "round 2"})).content[0].text == "pong round 2"
                await close
                assert not manager.tools and external.poll() is None
                await manager.stop()
        asyncio.run(scenario())
    return "foreground call and multi-round lease block config/disconnect until completion; host-only metadata stays accessible"


def check_request_capture():
    with tempfile.TemporaryDirectory(prefix="model-body-") as tmp:
        path = str(Path(tmp) / "app.sqlite")
        store = Store(path).init()
        spec = AgentSpec(label="capture", model="fixture/model", temperature=.37, max_tokens=17, extra_body={"tools": [{"type": "function", "function": {"name": "ping", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}]})
        chat = agent.Agent(spec, store=store)
        received = []
        fake_key = "fixture-provider-secret-only"

        def provider(request):
            received.append(json.loads(request.content))
            assert request.headers["authorization"] == "Bearer " + fake_key
            reply = "final <script>text</script>"
            frames = [{"choices": [{"delta": {"content": reply}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13, "cost": .001}}]
            return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n")

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
                with patch.object(llm, "shared_client", return_value=client), patch.dict(os.environ, {"OPENROUTER_API_KEY": fake_key, "RAG_EMBEDDING_API_KEY": ""}), patch.object(llm, "attribution_headers", return_value={}), patch.object(agent, "stream_completion", llm.stream_completion):
                    events = [event async for event in chat.ask("question " + fake_key)]
                assert events[-1]["committed"] and events[-1]["answer_index"] == 1
                assert len(received) == 1
                assert chat.history[-1].request_bodies == received
                assert received[0]["temperature"] == .37 and received[0]["max_tokens"] == 17
                assert received[0]["tools"] == spec.extra_body["tools"]
                assert fake_key not in json.dumps(received) and "Authorization" not in json.dumps(chat.transcript())
                snapshot = deepcopy(received)
                spec.temperature = .99
                spec.extra_body.clear()
                assert chat.transcript()[-1]["request_bodies"] == snapshot
                assert chat.usage_summary()["total_tokens"] == 13
        asyncio.run(scenario())
        identity = chat.id
        store.close()
        reopened = Store(path).init()
        restored = agent.Agent(AgentSpec(label="fallback", model="ignored"), store=reopened, agent_id=identity)
        assert restored.transcript()[-1]["request_bodies"] == received
        restored.remember("assistant", "legacy")
        assert restored.history[-1].request_bodies is None
        # Idempotent migration of a populated pre-capture messages table.
        with reopened.tx() as tx:
            tx.execute("ALTER TABLE messages DROP COLUMN request_bodies")
        reopened.close()
        migrated = Store(path).init()
        assert migrated.load_messages(identity)[1]["content"] == "final <script>text</script>"
        assert migrated.load_messages(identity)[1]["request_bodies"] is None
        migrated.init()
        migrated.close()
    return "actual outbound JSON equals committed/restored snapshot with final tools override; config edits cannot change it; no auth/key capture; populated legacy migration"
