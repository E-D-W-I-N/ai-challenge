"""Execute due MCP jobs in their originating chat; no provider or tool hardcoding."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid

from .agent import AgentBusyError

LOG = logging.getLogger(__name__)
POLL_SECONDS = 0.25
RUN_SECONDS = 240  # Below the service's 300-second claim expiry; no automatic replay.


class ReminderScheduler:
    def __init__(self, manager, registry):
        self.manager, self.registry = manager, registry
        self.loop = None
        self.running = {}
        self.claims = {}
        self.invalidating = {}

    async def invalidate(self, names):
        for name in names:
            self.invalidating[name] = self.invalidating.get(name, 0) + 1
        tasks = [task for (name, _), task in self.running.items() if name in names]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def resume(self, names):
        for name in names:
            remaining = self.invalidating.get(name, 0) - 1
            if remaining > 0:
                self.invalidating[name] = remaining
            else:
                self.invalidating.pop(name, None)

    def start(self):
        # Opaque host namespace prevents unrelated application databases sharing ids.
        with self.registry.store.tx() as conn:
            conn.execute("INSERT INTO meta (key,value) VALUES ('reminder_namespace',?) "
                         "ON CONFLICT(key) DO NOTHING", (uuid.uuid4().hex,))
            self.manager.context_namespace = conn.execute(
                "SELECT value FROM meta WHERE key='reminder_namespace'"
            ).fetchone()[0]
        self.loop = asyncio.create_task(self._poll())

    async def stop(self):
        claims = list(self.claims.values())
        tasks = [self.loop, *self.running.values()]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)
        # A task cancelled before its first instruction never enters its finally.
        for claim in claims:
            if not claim["started"]:
                if claim["agent"] is not None:
                    claim["agent"].release()
                with contextlib.suppress(Exception):
                    await self.manager.reminder_protocol(claim["server"], "_reminder_finish",
                        {"id": claim["id"], "token": claim["token"], "error": "приложение остановлено до исполнения"})

    async def _poll(self):
        while True:
            try:
                await self.tick()
            except Exception:
                LOG.exception("Reminder scheduler poll failed")
            await asyncio.sleep(POLL_SECONDS)

    @staticmethod
    def receipt(agent, server, rid):
        return any((t.metrics or {}).get("reminder_scheduled") ==
                   {"id": rid, "server": server.name} for t in agent.history)

    async def tick(self):
        live = {s.name for s in self.manager.servers if s.status == "ok" and self.manager.schedules(s)
                and s.name not in self.invalidating}
        for (owner, _), task in list(self.running.items()):
            if owner not in live:
                task.cancel()
        for server in list(self.manager.servers):
            if server.status != "ok" or not self.manager.schedules(server) or server.name in self.invalidating:
                continue
            try:
                data = await self.manager.reminder_protocol(server, "reminders", {})
            except Exception:
                # Connection loss prevents further tool/model work for this service.
                for (owner, _), task in list(self.running.items()):
                    if owner == server.name:
                        task.cancel()
                LOG.exception("Reminder service unavailable: %s", server.name)
                continue
            items = {i["id"]: i for i in data["items"]}
            for (owner, rid), task in list(self.running.items()):
                if owner == server.name and (rid not in items or items[rid]["status"] != "running"):
                    task.cancel()
            prefix = self.manager.context_namespace + "/"
            for item in items.values():
                if (item["status"] != "pending" or item["due_at"] > time.time()
                        or not item["context_id"].startswith(prefix)):
                    continue
                key = (server.name, item["id"])
                if key in self.running:
                    continue
                chat_id = item["context_id"][len(prefix):]
                agent = self.registry.load(chat_id)
                if agent is not None:
                    try:
                        agent.reserve()
                    except AgentBusyError:
                        continue  # The occurrence is not consumed while the chat is busy.
                token = uuid.uuid4().hex
                try:
                    claimed = await self.manager.reminder_protocol(server, "_reminder_claim",
                        {"id": item["id"], "token": token, "context_id": item["context_id"]})
                except BaseException:
                    if agent is not None:
                        agent.release()
                    raise
                if not claimed:
                    if agent is not None:
                        agent.release()
                    continue
                self.claims[key] = {"started": False, "agent": agent, "server": server,
                                    "id": item["id"], "token": token}
                task = asyncio.create_task(self._execute(server, item, token, agent))
                self.running[key] = task
                task.add_done_callback(lambda _, k=key: (self.running.pop(k, None), self.claims.pop(k, None)))

    async def cancel(self, chat_id: str, server_name: str, rid: int) -> bool:
        agent = self.registry.load(chat_id)
        server = next((s for s in self.manager.servers if s.name == server_name and s.status == "ok"), None)
        if agent is None or server is None or not self.manager.schedules(server):
            return False
        if not self.receipt(agent, server, rid):
            return False
        # The service enforces the exact origin context. This direct call to
        # the pinned session must not await manager.lease: the delayed exchange
        # owns that lease until its model stream exits.
        result = await self.manager.reminder_protocol(server, "cancel", {
            "id": rid, "context_id": self.manager.context_for(chat_id),
        }, concurrent=True)
        if "снято" not in result:
            return False
        running = self.running.get((server_name, rid))
        if running is not None:
            running.cancel()
        return True

    async def _execute(self, server, item, token, agent):
        self.claims[(server.name, item["id"])]["started"] = True
        error = ""
        question = f"[Напоминание №{item['id']}] Срок наступил. Выполни сейчас, без нового планирования: {item['text']}"

        async def valid():
            return (agent is not None and agent.store is self.registry.store
                    and server.name not in self.invalidating
                    and any(s is server and s.status == "ok" for s in self.manager.servers)
                    and self.receipt(agent, server, item["id"])
                    and await self.manager.reminder_protocol(server, "_reminder_claim",
                        {"id": item["id"], "token": token, "context_id": item["context_id"], "check": True}))

        try:
            if not await valid():
                error = "исходный чат удалён или очищен; задача не выполнена"
                return
            async with asyncio.timeout(RUN_SECONDS):
                done = None
                events = agent.ask(question, scheduled={"id": item["id"], "server": server.name},
                                   can_run=valid)
                async with contextlib.aclosing(events):
                    async for event in events:
                        if event["type"] == "done":
                            done = event
                error = (done or {}).get("error") or ""
                if not done or not done.get("committed"):
                    error = error or "модель не вернула результат"
                if any(not c["ok"] for c in ((done or {}).get("metrics") or {}).get("tool_calls", [])):
                    error = error or "ошибка вызова инструмента"
                if ((done or {}).get("metrics") or {}).get("tool_iterations"):
                    error = error or "исчерпан лимит цикла инструментов"
                if error and await valid() and not (done or {}).get("committed"):
                    agent._commit(question, "Ошибка напоминания: " + error, error,
                                  metrics={"reminder_execution": {"id": item["id"], "server": server.name}},
                                  request_bodies=(done or {}).get("request_bodies"))
        except asyncio.CancelledError:
            error = "исполнение остановлено; автоматического повтора нет"
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            LOG.exception("Reminder execution failed: %s/%s", server.name, item["id"])
            with contextlib.suppress(Exception):
                if await valid():
                    agent._commit(question, "Ошибка напоминания: " + error, error,
                                  metrics={"reminder_execution": {"id": item["id"], "server": server.name}})
        finally:
            try:
                await self.manager.reminder_protocol(server, "_reminder_finish",
                    {"id": item["id"], "token": token, "error": error})
            except Exception:
                LOG.exception("Reminder outcome could not be persisted; claim will expire")
            if agent is not None:
                agent.release()
