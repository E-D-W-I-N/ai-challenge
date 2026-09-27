"""Critical offline acceptance against an independently launched HTTP MCP service."""
from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from checks import _stub

_stub.install_offline()
from app import agent as agent_module, llm, mcp, store as store_module
from app.main import app
from app.registry import AgentRegistry
from app.reminders import ReminderScheduler
from app.schema import AgentSpec
from app.store import Store
from fastapi.testclient import TestClient
from services.reminders import server as service

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)


def call(name, args, cid="tool_1"):
    return {"id": cid, "name": name, "arguments": json.dumps(args)}


async def until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < end, "timed out waiting for actual scheduled result"
        await asyncio.sleep(.02)


def service_claims(path):
    # Migration retains legacy rows; elapsed time is never a completed execution.
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE reminders(id INTEGER PRIMARY KEY AUTOINCREMENT,text TEXT NOT NULL,due_at REAL NOT NULL,every REAL)")
        conn.execute("INSERT INTO reminders(text,due_at) VALUES ('legacy',1)")
    assert service.list_reminders(path=path, now=1000)["items"][0]["fired"] == 0
    rid = service.add_reminder("bound", 10, path=path, now=100, context_id="opaque/chat")
    assert not service.claim_reminder(rid, "early", "opaque/chat", path=path, now=109)
    assert service.claim_reminder(rid, "first", "opaque/chat", path=path, now=110)
    assert not service.claim_reminder(rid, "second", "opaque/chat", path=path, now=111)
    live = service.list_reminders(path=path, now=111)["items"][-1]
    assert live["status"] == "running" and live["fired"] == 0
    interrupted = service.list_reminders(path=path, now=411)["items"][-1]
    assert interrupted["status"] == "failed" and interrupted["fired"] == 0
    assert not service.claim_reminder(rid, "replay", "opaque/chat", path=path, now=500)
    assert not service.finish_reminder(rid, "first", path=path, now=500)
    concurrent = service.add_reminder("concurrent", 0, path=path, now=600, context_id="opaque/chat")
    with ThreadPoolExecutor(max_workers=2) as workers:
        winners = list(workers.map(lambda token: service.claim_reminder(
            concurrent, token, "opaque/chat", path=path, now=600), ("one", "two")))
    assert sorted(winners) == [False, True], winners
    sibling = service.add_reminder("same chat", 0, path=path, now=600, context_id="opaque/chat")
    assert not service.claim_reminder(sibling, "sibling", "opaque/chat", path=path, now=600)


async def execution(config, git_root, restart_service):
    store = Store(Path(git_root) / "app.db").init()
    registry = AgentRegistry(store=store)
    manager = mcp.McpManager()
    with patch.dict(os.environ, {"MCP_CONFIG_PATH": str(config), "MCP_DISABLED": "0"}), \
            patch.object(mcp, "ROOT", Path(git_root)), patch.object(agent_module, "MANAGER", manager):
        await manager.start()
        assert all(s.status == "ok" for s in manager.servers), [s.error for s in manager.servers]
        remote = manager.servers[0]
        assert remote.process is None  # Host owns a connection, never the HTTP service.
        declared = agent_module.declared_tools(manager)
        assert not any(t["function"]["name"].startswith("_reminder") for t in declared)
        assert "context_id" not in next(t for t in declared if t["function"]["name"] == "remind")["function"]["parameters"]["properties"]
        origin = registry.create(AgentSpec(label="original", model="stub/current-model"))
        neighbour = registry.create(AgentSpec(label="another", model="stub/neighbour"))
        scheduler = ReminderScheduler(manager, registry)
        scheduler.start()
        # Real provider serialization with an offline HTTP stub: the initial
        # request may schedule, but Git and all subsequent request JSON must
        # appear only after the deadline in the same originating chat.
        captured, sent_at = [], []
        def provider(request):
            body = json.loads(request.content)
            captured.append(body); sent_at.append(time.time())
            index = len(captured) - 1
            if index < 3:
                name, args = [
                    ("remind", {"text": "Проверь пять коммитов", "in_seconds": .35}),
                    ("git_log", {"n": 5}),
                    ("git_diff_stat", {"ref": "HEAD~4"}),
                ][index]
                frame = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"cap_{index}",
                    "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
                finish = "tool_calls"
            else:
                frame = {"choices": [{"delta": {"content": "Итог пяти коммитов готов"}}]}
                finish = "stop"
            ending = {"choices": [{"delta": {}, "finish_reason": finish}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            return httpx.Response(200, text="data: " + json.dumps(frame) + "\n\n"
                + "data: " + json.dumps(ending) + "\n\n" + "data: [DONE]\n\n")

        capture_agent = registry.create(AgentSpec(label="exact delayed JSON", model="offline/any-provider"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as offline_client:
            with patch.object(agent_module, "stream_completion", llm.stream_completion), \
                    patch.object(llm, "shared_client", return_value=offline_client), \
                    patch.object(llm, "api_key", return_value="offline-fixture"), \
                    patch.object(store_module, "api_key", return_value="offline-fixture"), \
                    patch.object(llm, "attribution_headers", return_value={}):
                [event async for event in capture_agent.ask("Через срок проверь пять коммитов")]
                listing = await manager.reminder_protocol(remote, "reminders", {})
                capture_job = next(i for i in listing["items"] if i["context_id"] == manager.context_for(capture_agent.id))
                due = capture_job["due_at"]
                assert len(captured) == 1 and capture_agent.history[-1].request_bodies == captured[:1]
                await asyncio.sleep(.08)
                assert time.time() < due and len(captured) == 1
                await until(lambda: len(capture_agent.history) == 4)
                await until(lambda: not scheduler.running)
                assert len(captured) == 4 and min(sent_at[1:]) >= due, (sent_at, due)
                assert capture_agent.history[-1].request_bodies == captured[1:]
                assert [turn.role for turn in capture_agent.history] == ["user", "assistant"] * 2
                assert [run["name"] for run in capture_agent.history[-1].metrics["tool_calls"]] == ["git_log", "git_diff_stat"]
                assert captured[2]["messages"][-1]["role"] == "tool"
                assert captured[3]["messages"][-1]["role"] == "tool"
        timestamps = []

        def actions(messages, index):
            timestamps.append(time.time())
            if index == 0:
                # Git before AND after remind in the same batch must both wait.
                return [call("git_log", {"n": 5}, "before"),
                        call("remind", {"text": "Проверь последние 5 коммитов и опиши изменения", "in_seconds": .6, "context_id": "forged/other"}, "schedule"),
                        call("git_diff_stat", {}, "after")]
            if index == 1:
                return [call("git_log", {"n": 5})]
            if index == 2:
                assert "fixture-4" in messages[-1]["content"], messages[-1]
                return [call("git_diff_stat", {"ref": "HEAD~4"})]

        def reply(messages, index):
            return "" if index < 3 else "Свежий итог: " + messages[-1]["content"]

        _stub.reset(); _stub.install(reply=reply, tool_calls=actions)
        try:
            events = [e async for e in origin.ask("Через 50 секунд проверь последние 5 коммитов")]
            scheduled = json.loads(next(e for e in events if e["type"] == "tool_call")["result"])
            due = scheduled["due_at"]
            assert len(_stub.CALLS) == 1 and len(origin.history) == 2
            assert [e["name"] for e in events if e["type"] == "tool_call"] == ["remind"]
            assert "заведено" in origin.history[-1].content
            await asyncio.sleep(.15)
            assert time.time() < due and len(_stub.CALLS) == 1
            assert neighbour.history == []
            await until(lambda: len(origin.history) == 4)
            await until(lambda: not scheduler.running)
            assert all(t >= due for t in timestamps[1:]), (timestamps, due)
            assert len(_stub.CALLS) == 4 and all(c["model"] == "stub/current-model" for c in _stub.CALLS)
            assert neighbour.history == [] and "file.txt" in origin.history[-1].content
            assert [t.role for t in origin.history] == ["user", "assistant"] * 2
            assert [c["name"] for c in origin.history[-1].metrics["tool_calls"]] == ["git_log", "git_diff_stat"]
            listing = await manager.reminder_protocol(remote, "reminders", {})
            assert listing["items"][-1]["fired"] == 1 and listing["items"][-1]["status"] == "done"
            await asyncio.sleep(.3)
            assert len(_stub.CALLS) == 4  # No duplicate claim/execution.

            # Busy chat postpones a due occurrence; cancellation consumes no run.
            _stub.reset()
            _stub.install(reply="", tool_calls=lambda ms, i: [call("remind", {"text": "cancel pending", "in_seconds": .2})] if i == 0 else None)
            events = [e async for e in origin.ask("schedule pending")]
            pending = json.loads(next(e for e in events if e["type"] == "tool_call")["result"])["id"]
            origin.reserve()
            await asyncio.sleep(.45)
            assert len(_stub.CALLS) == 1
            await manager.call("cancel", {"id": pending}, chat_id=origin.id)
            origin.release()
            await asyncio.sleep(.3)
            assert len(_stub.CALLS) == 1

            # Every occurrence runs fresh tools. Overrun skips slots on fixed cadence.
            _stub.reset()
            _stub.install(reply=lambda ms, i: "" if i == 0 else "Свежий результат " + str(i),
                delay=.045, chunks=2,
                tool_calls=lambda ms, i: [call("remind", {"text": "Повторяй git status", "in_seconds": .2, "every": .3})] if i == 0
                else ([call("git_status", {})] if ms[-1]["role"] == "user" else None))
            events = [e async for e in origin.ask("repeat")]
            repeat = json.loads(next(e for e in events if e["type"] == "tool_call")["result"])
            initial_len = len(origin.history)
            await until(lambda: len(origin.history) >= initial_len + 4)
            await until(lambda: not scheduler.running)
            await manager.call("cancel", {"id": repeat["id"]}, chat_id=origin.id)
            last = len(origin.history)
            outputs = [t.content for t in origin.history[initial_len:] if t.role == "assistant"]
            assert len(outputs) == 2 and outputs[0] != outputs[1], outputs
            assert _stub.ACTIVE["peak"] == 1
            await asyncio.sleep(.6)
            assert len(origin.history) == last

            # A running cancellation and forget cannot publish or reschedule stale text.
            manager.before_change, manager.after_change = scheduler.invalidate, scheduler.resume
            for operation in ("cancel", "forget", "delete", "disabled", "reconfigure"):
                _stub.reset()
                _stub.install(reply=lambda ms, i: "" if i == 0 else "blocked result",
                    delay=.2, chunks=6,
                    tool_calls=lambda ms, i: [call("remind", {"text": operation, "in_seconds": .15, "every": .2})] if i == 0 else None)
                target = registry.create(AgentSpec(label=operation, model="stub/model"))
                events = [e async for e in target.ask(operation)]
                rid = json.loads(next(e for e in events if e["type"] == "tool_call")["result"])["id"]
                await until(lambda: len(_stub.CALLS) == 2)
                if operation == "cancel":
                    await manager.call("cancel", {"id": rid}, chat_id=target.id)
                elif operation == "forget":
                    target.forget()
                elif operation == "delete":
                    registry.kill(target.id)
                elif operation == "reconfigure":
                    await manager.connection("remind", manager.config["revision"], False)
                    await manager.connection("remind", manager.config["revision"], True)
                else:
                    remote.status = "down"
                await until(lambda: not scheduler.running)
                remote.status = "ok"
                assert len(target.history) == (0 if operation == "forget" else 2)
                if operation == "delete":
                    assert store.load_session(target.id) is None
            manager.before_change = manager.after_change = None

            # Empty model error must be visible in both job state and original chat.
            _stub.reset()
            _stub.install(reply="", tool_calls=lambda ms, i: [call("remind", {"text": "will fail", "in_seconds": .2})] if i == 0 else None)
            events = [e async for e in origin.ask("failure")]
            rid = json.loads(next(e for e in events if e["type"] == "tool_call")["result"])["id"]
            _stub.install(fail=True)
            old = len(origin.history)
            await until(lambda: len(origin.history) == old + 2)
            await until(lambda: not scheduler.running)
            failed = next(i for i in (await manager.reminder_protocol(remote, "reminders", {}))["items"] if i["id"] == rid)
            assert failed["status"] == "failed" and failed["fired"] == 0 and failed["error"]
            assert origin.history[-1].error and "Ошибка напоминания" in origin.history[-1].content

            # Pending job survives host disconnect/restart and chat reload.
            _stub.reset()
            _stub.install(reply="", tool_calls=lambda ms, i: [call("remind", {"text": "after restart", "in_seconds": .45})] if i == 0 else None)
            [e async for e in origin.ask("restart")]
            await scheduler.stop()
            await manager.stop()
            await asyncio.to_thread(restart_service)
            await asyncio.sleep(.5)
            assert len(_stub.CALLS) == 1
            await manager.start()
            remote = manager.servers[0]
            registry = AgentRegistry(store=store)
            origin = registry.require(origin.id)
            scheduler = ReminderScheduler(manager, registry)
            _stub.install(reply="Результат после рестарта")
            before = len(origin.history)
            scheduler.start()
            await until(lambda: len(origin.history) == before + 2)
            await until(lambda: not scheduler.running)
            assert len(_stub.CALLS) == 2

            # Shutdown immediately after claim must release even an unstarted task.
            scheduler.loop.cancel()
            await asyncio.gather(scheduler.loop, return_exceptions=True)
            _stub.reset()
            _stub.install(reply="", tool_calls=[call("remind", {"text": "stop before start", "in_seconds": 0})])
            events = [e async for e in origin.ask("shutdown")]
            rid = json.loads(next(e for e in events if e["type"] == "tool_call")["result"])["id"]
            await scheduler.tick()
            await scheduler.stop()
            assert not origin.busy and len(_stub.CALLS) == 1
            item = next(i for i in (await manager.reminder_protocol(remote, "reminders", {}))["items"] if i["id"] == rid)
            assert item["status"] == "failed" and item["fired"] == 0, item
        finally:
            await scheduler.stop()
            await manager.stop()
            store.close()


def lifespan_acceptance(config, directory):
    from app import main
    store = Store(directory / "lifespan.db").init()
    registry = AgentRegistry(store=store)
    manager = mcp.McpManager()
    _stub.reset()
    _stub.install(reply=lambda ms, i: "" if i == 0 else "Автоматический итог в исходном чате",
                  tool_calls=lambda ms, i: [call("remind", {"text": "automatic result", "in_seconds": .35})] if i == 0 else None)
    with patch.dict(os.environ, {"MCP_CONFIG_PATH": str(config), "MCP_DISABLED": "0"}), \
            patch.object(mcp, "ROOT", directory), patch.object(mcp, "MANAGER", manager), \
            patch.object(agent_module, "MANAGER", manager), patch.object(main, "REGISTRY", registry):
        with TestClient(app) as client:
            original = client.post("/api/agents", json={"agent": {"model": "stub/custom", "label": "original"}}).json()["agents"][0]["id"]
            other = client.post("/api/agents", json={"agent": {"model": "stub/other", "label": "selected"}}).json()["agents"][0]["id"]
            response = client.post(f"/api/agents/{original}/messages", json={"text": "schedule"})
            assert response.status_code == 200 and "answer_index" in response.text
            assert len(_stub.CALLS) == 1
            client.get(f"/api/agents/{other}")  # Selection of another chat has no scheduling effect.
            end = time.monotonic() + 5
            while True:
                body = client.get(f"/api/agents/{original}").json()
                if body["history_len"] == 4:
                    break
                assert time.monotonic() < end
                time.sleep(.03)
            assert body["transcript"][-1]["content"] == "Автоматический итог в исходном чате"
            assert client.get(f"/api/agents/{other}").json()["history_len"] == 0
            assert len(_stub.CALLS) == 2
            end = time.monotonic() + 5
            while client.get(f"/api/agents/{original}").json()["busy"]:
                assert time.monotonic() < end
                time.sleep(.02)

            # The real Tools route and cancel action remain responsive while
            # a delayed model stream owns the manager lease and chat reserve.
            _stub.reset()
            _stub.install(reply=lambda ms, i: "" if i == 0 else "cancelled result",
                delay=.15, chunks=8,
                tool_calls=lambda ms, i: [call("remind", {"text": "cancel active occurrence", "in_seconds": .1, "every": .2})] if i == 0 else None)
            sent = client.post(f"/api/agents/{original}/messages", json={"text": "cancel this"})
            assert sent.status_code == 200, sent.text
            end = time.monotonic() + 5
            while len(_stub.CALLS) < 2:
                assert time.monotonic() < end
                time.sleep(.02)
            started = time.monotonic()
            tools = client.get("/api/mcp", headers={"X-Chat-ID": original}).json()
            job = next(item for item in tools["servers"][0]["reminders"]["items"] if item["text"] == "cancel active occurrence")
            assert job["can_cancel"] and job["status"] == "running", job
            cancelled = client.post(f"/api/agents/{original}/reminders/remind/{job['id']}/cancel", json={})
            assert cancelled.status_code == 200 and cancelled.json()["cancelled"], cancelled.text
            assert time.monotonic() - started < 1.0, "cancel waited for the model exchange lease"
            time.sleep(.6)
            assert client.get(f"/api/agents/{original}").json()["history_len"] == 6
            assert len(_stub.CALLS) == 2
    store.close()


def main():
    with tempfile.TemporaryDirectory(prefix="scheduler-check-") as tmp:
        directory = Path(tmp)
        service_claims(directory / "claims.db")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        env.update(REMIND_DB_PATH=str(directory / "remote.db"), REMIND_PORT=str(port))
        process = subprocess.Popen([sys.executable, "-m", "services.reminders.server"], cwd=ROOT,
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def ready_for(target, target_port):
            end = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", target_port), timeout=.1):
                        return
                except OSError:
                    assert target.poll() is None and time.monotonic() < end
                    time.sleep(.05)

        def ready():
            ready_for(process, port)

        def restart_service():
            nonlocal process
            process.terminate(); process.wait(timeout=5)
            process = subprocess.Popen([sys.executable, "-m", "services.reminders.server"], cwd=ROOT,
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ready()
        git_process = None
        try:
            ready()
            for name in ("app", "checks", "services"):
                (directory / name).symlink_to(ROOT / name, target_is_directory=True)
            repo = directory / "repo"; repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for n in range(5):
                (repo / "file.txt").write_text("change\n" * (n + 1))
                subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
                subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                "commit", "-qm", f"fixture-{n}"], cwd=repo, check=True)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0)); git_port = sock.getsockname()[1]
            git_process = subprocess.Popen(
                [sys.executable, "-m", "services.git", "--repo", str(repo), "--port", str(git_port)],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            ready_for(git_process, git_port)
            config = directory / "mcp.json"
            config.write_text(json.dumps({"servers": {"remind": {"url": f"http://127.0.0.1:{port}/mcp", "enabled": True},
                                                      "git": {"url": f"http://127.0.0.1:{git_port}/mcp", "enabled": True}}}))
            asyncio.run(execution(config, directory, restart_service))
            lifespan_acceptance(config, directory)
            assert process.poll() is None  # Application shutdown leaves service alive.
            assert git_process.poll() is None
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            if git_process is not None:
                git_process.terminate()
                try:
                    git_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    git_process.kill(); git_process.wait()
    print("HTTP MCP: deferred git + exact chat, repeat fresh runs, cancel/forget/delete, claims, error and restart OK")


if __name__ == "__main__":
    main()
