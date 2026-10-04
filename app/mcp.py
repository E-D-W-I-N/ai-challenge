"""Generic MCP sessions: manual Streamable HTTP services and explicit stdio fixtures."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from urllib.parse import urlsplit
from contextvars import ContextVar
from copy import deepcopy

import httpx
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import anyio.abc
import anyio.streams.text
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.message import SessionMessage

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "mcp.json"
"""Корень репозитория: cwd дочерних процессов и умолчательное место конфига."""

CHILD_ENV_KEYS = ("PATH", "HOME", "LANG")
"""Белый список окружения дочерних процессов. `os.environ` целиком не
наследуется никогда: в нём лежит OPENROUTER_API_KEY."""

CONNECT_TIMEOUT_S = 5.0
"""Сколько ждём рукопожатие и список инструментов: не ответивший вовремя
сервер — down, а приложение стартует дальше."""

KILL_AFTER_S = 2.0
"""Пауза между terminate и kill при остановке."""


def _child_env() -> dict[str, str]:
    return {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}


async def _pump_stdout(process, send) -> None:
    """Строки stdout сервера → сообщения сессии. По образцу транспорта SDK:
    неразобранная строка едет в поток исключением, а не пропадает молча."""
    assert process.stdout is not None
    async with send:
        buffer = ""
        async for chunk in anyio.streams.text.TextReceiveStream(process.stdout):
            lines = (buffer + chunk).split("\n")
            buffer = lines.pop()
            for line in lines:
                try:
                    message = types.JSONRPCMessage.model_validate_json(line)
                except Exception as exc:  # noqa: BLE001 — сервер болтает в stdout
                    await send.send(exc)
                    continue
                await send.send(SessionMessage(message))


async def _pump_stdin(process, recv) -> None:
    """Сообщения сессии → строки stdin сервера."""
    assert process.stdin is not None
    async for message in recv:
        raw = message.message.model_dump_json(by_alias=True, exclude_none=True)
        await process.stdin.send((raw + "\n").encode())


async def _kill(process) -> None:
    if process.returncode is None:
        process.kill()
    with contextlib.suppress(Exception):
        await process.wait()


@dataclass
class McpServer:
    """A remote session (or an explicitly configured legacy stdio child)."""

    name: str
    timeout_s: float = 10.0
    params: StdioServerParameters | None = None
    url: str = ""
    status: str = "disconnected"
    error: str = ""
    tools: list = field(default_factory=list)
    view: list = field(default_factory=list)
    process: anyio.abc.Process | None = None
    session: ClientSession | None = None
    task: asyncio.Task | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class ToolRef:
    server: McpServer
    tool: str


class McpConfigConflict(ValueError):
    pass


def validate_servers(rows) -> list[dict]:
    """Only ordinary HTTP endpoints; credentials belong outside this UI."""
    if not isinstance(rows, list) or len(rows) > 12:
        raise ValueError("servers: список, максимум 12 серверов")
    result, names, urls = [], set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {"name", "url", "enabled"}:
            raise ValueError("сервер: только name, url, enabled")
        name, url, enabled = row.get("name"), row.get("url"), row.get("enabled", False)
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or "__" in name:
            raise ValueError("name: 1–64 латинских буквы, цифры, _ или -; без __")
        if not isinstance(url, str) or not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
            raise ValueError("url: непустой HTTP(S) URL без пробелов")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ValueError("url: некорректный адрес или порт") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or port == 0:
            raise ValueError("url: HTTP(S) endpoint без логина, пароля, query или fragment")
        if not isinstance(enabled, bool):
            raise ValueError("enabled: true или false")
        if name in names or url in urls:
            raise ValueError("имена и URL серверов должны быть уникальны")
        names.add(name)
        urls.add(url)
        result.append({"name": name, "url": url, "enabled": enabled})
    return result


class McpManager:
    """Generic MCP transport; never starts a service for a URL.

    The connection owner enters/exits SDK task groups in one task. A lease
    serializes tool exchanges against config changes, reconnect and shutdown.
    """

    def __init__(self, *, disabled: bool = False) -> None:
        self.servers: list[McpServer] = []
        self.tools: dict[str, ToolRef] = {}
        self.config = {"revision": 0, "servers": []}
        self.store = None
        self.disabled = disabled
        self._lock = asyncio.Lock()
        self._owner = ContextVar("mcp_lease", default=None)
        self.context_namespace = ""
        self.before_change = None
        self.after_change = None

    async def _changing(self, names):
        if self.before_change is not None:
            await self.before_change(names)

    def _changed(self, names):
        if self.after_change is not None:
            self.after_change(names)

    @staticmethod
    def schedules(server: McpServer) -> bool:
        names = {tool.name for tool in server.tools if (tool.meta or {}).get("host_only")}
        return {"_reminder_claim", "_reminder_finish"} <= names and "reminders" in {tool.name for tool in server.tools}

    def scheduling_tool(self, name: str) -> bool:
        ref = self.tools.get(name)
        return ref is not None and ref.tool == "remind" and self.schedules(ref.server)

    def context_for(self, chat_id: str) -> str:
        return self.context_namespace + "/" + chat_id

    @contextlib.asynccontextmanager
    async def lease(self):
        task = asyncio.current_task()
        if self._owner.get() is task:
            yield self
            return
        async with self._lock:
            token = self._owner.set(task)
            try:
                yield self
            finally:
                self._owner.reset(token)

    async def start(self, store=None) -> None:
        self.store = store
        self.config = store.load_mcp_config() if store else {"revision": 0, "servers": []}
        if self.disabled:
            return
        # Persisted UI config supersedes the explicit legacy file. The tracked
        # default file is empty: no subprocess/demo is launched by default.
        if self.config["revision"]:
            await self._replace(self.config["servers"])
            return
        path = DEFAULT_CONFIG_PATH
        if not path.is_file():
            return
        config = json.loads(path.read_text(encoding="utf-8"))
        for name, spec in config.get("servers", {}).items():
            server = McpServer(name=name, timeout_s=float(spec.get("timeout_s", 10)))
            if "url" in spec:
                row = validate_servers([{"name": name, "url": spec["url"], "enabled": spec.get("enabled", False)}])[0]
                self.config["servers"].append(row)
                server.url = row["url"]
                self.servers.append(server)
                if row["enabled"]:
                    await self._connect(server)
            else:
                server.params = StdioServerParameters(command=sys.executable, args=["-m", spec["module"]], env=_child_env(), cwd=ROOT)
                self.servers.append(server)
                await self._connect(server)
        self._build_registry()

    async def _replace(self, rows) -> None:
        for server in self.servers:
            await self._disconnect(server)
        self.servers = []
        for row in rows:
            server = McpServer(name=row["name"], url=row["url"])
            self.servers.append(server)
            if row["enabled"] and not self.disabled:
                await self._connect(server)
        self._build_registry()

    async def _connect(self, server) -> None:
        server.ready = asyncio.Event()
        server.closed = asyncio.Event()
        server.status, server.error = "connecting", ""
        server.task = asyncio.create_task(self._connection(server))
        await server.ready.wait()

    async def _connection(self, server) -> None:
        try:
            async with contextlib.AsyncExitStack() as stack:
                if server.url:
                    client = await stack.enter_async_context(httpx.AsyncClient(timeout=httpx.Timeout(30, read=300), trust_env=False))
                    read, write, _ = await stack.enter_async_context(streamable_http_client(server.url, http_client=client))
                else:
                    params = server.params
                    server.process = await anyio.open_process([params.command, *params.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, env=params.env, cwd=params.cwd)
                    stack.push_async_callback(server.process.aclose)
                    read_send, read = anyio.create_memory_object_stream(0)
                    write, write_recv = anyio.create_memory_object_stream(0)
                    group = await stack.enter_async_context(anyio.create_task_group())
                    stack.callback(group.cancel_scope.cancel)
                    group.start_soon(_pump_stdout, server.process, read_send)
                    group.start_soon(_pump_stdin, server.process, write_recv)
                server.session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(server.session.initialize(), CONNECT_TIMEOUT_S)
                cursor = None
                while True:
                    result = await asyncio.wait_for(server.session.list_tools(cursor=cursor), CONNECT_TIMEOUT_S)
                    server.tools.extend(result.tools)
                    cursor = result.nextCursor
                    if not cursor:
                        break
                server.status = "ok"
                server.ready.set()
                await server.closed.wait()
                if not server.url:
                    group.cancel_scope.cancel()
        except Exception as exc:
            # Exception groups/transport errors can contain request headers or
            # remote content; show a useful class without copying that content.
            server.status = "down"
            leaf = exc
            while isinstance(leaf, BaseExceptionGroup) and leaf.exceptions:
                leaf = leaf.exceptions[0]
            if server.url:
                detail = ("HTTP " + str(leaf.response.status_code)) if isinstance(leaf, httpx.HTTPStatusError) else type(leaf).__name__
                server.error = "Не удалось подключиться к MCP: " + detail
            else:
                from .store import redact
                server.error = redact(str(leaf))
        finally:
            if server.process is not None and server.process.returncode is None:
                await _kill(server.process)
            server.session = None
            if server.status == "ok":
                server.status = "disconnected"
            server.ready.set()

    async def _disconnect(self, server) -> None:
        process = server.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), KILL_AFTER_S)
            except asyncio.TimeoutError:
                await _kill(process)
        server.closed.set()
        if server.task is not None:
            # Bound cleanup even if the remote session DELETE never answers.
            try:
                await asyncio.wait_for(server.task, CONNECT_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass
        server.tools, server.view = [], []
        server.status, server.error = "disconnected", ""

    def _build_registry(self) -> None:
        self.tools = {}
        owners = {}
        for server in self.servers:
            server.view = []
            if server.status == "ok":
                for tool in server.tools:
                    if not (tool.meta or {}).get("host_only"):
                        owners[tool.name] = owners.get(tool.name, 0) + 1
        qualified = {name for name, count in owners.items() if count > 1}
        # A bare name can collide with another server's qualified name too.
        for server in self.servers:
            for tool in server.tools:
                if tool.name in qualified:
                    candidate = f"{server.name}__{tool.name}"
                    if candidate in owners:
                        qualified.add(candidate)
        for server in self.servers:
            if server.status != "ok":
                continue
            for tool in server.tools:
                if (tool.meta or {}).get("host_only"):
                    continue
                name = f"{server.name}__{tool.name}" if tool.name in qualified else tool.name
                self.tools[name] = ToolRef(server, tool.name)
                schema = deepcopy(tool.inputSchema)
                if self.schedules(server) and tool.name in {"remind", "cancel", "clear"}:
                    schema.get("properties", {}).pop("context_id", None)
                    if "context_id" in schema.get("required", []):
                        schema["required"].remove("context_id")
                server.view.append({"name": name, "description": tool.description or "", "schema": schema})

    def _revision(self, revision):
        current = self.store.load_mcp_config() if self.store else self.config
        if isinstance(revision, bool) or not isinstance(revision, int) or revision != current["revision"] or revision != self.config["revision"]:
            raise McpConfigConflict("Настройки MCP изменились: обновите список перед повтором")

    def _save(self, rows, revision):
        self._revision(revision)
        self.config = self.store.save_mcp_config(rows, revision) if self.store else {"revision": revision + 1, "servers": deepcopy(rows)}

    async def status(self):
        saved = self.store.load_mcp_config() if self.store is not None else self.config
        names = {s.name for s in self.servers} if saved["revision"] != self.config["revision"] else set()
        if names:
            await self._changing(names)
        try:
            async with self.lease():
                if self.store is not None:
                    saved = self.store.load_mcp_config()
                    if saved["revision"] != self.config["revision"]:
                        self.config = saved
                        await self._replace(saved["servers"])
                return self.view()
        finally:
            if names:
                self._changed(names)

    async def configure(self, rows, revision):
        rows = validate_servers(rows)
        self._revision(revision)
        names = {s.name for s in self.servers}
        await self._changing(names)
        try:
            async with self.lease():
                self._save(rows, revision)
                await self._replace(rows)
                return self.view()
        finally:
            self._changed(names)

    async def connection(self, name, revision, enabled):
        self._revision(revision)
        if not any(r["name"] == name for r in self.config["servers"]):
            raise ValueError("Сначала сохраните URL сервера")
        names = {name}
        await self._changing(names)
        try:
            async with self.lease():
                self._revision(revision)
                row = next((r for r in self.config["servers"] if r["name"] == name), None)
                if row is None:
                    raise ValueError("Сначала сохраните URL сервера")
                if self.disabled and enabled:
                    raise ValueError("MCP подключение отключено")
                rows = deepcopy(self.config["servers"])
                next(r for r in rows if r["name"] == name)["enabled"] = enabled
                self._save(rows, revision)
                server = next(s for s in self.servers if s.name == name)
                await self._disconnect(server)
                if enabled:
                    await self._connect(server)
                self._build_registry()
                return self.view()
        finally:
            self._changed(names)

    async def call(self, name: str, args: dict, *, chat_id: str | None = None):
        async with self.lease():
            ref = self.tools.get(name)
            if ref is None or ref.server.status != "ok" or ref.server.session is None:
                raise KeyError(f"инструмента {name!r} нет ни на одном живом сервере")
            if chat_id is not None and self.schedules(ref.server) and ref.tool in {"remind", "cancel", "clear"}:
                args = {**args, "context_id": self.context_for(chat_id)}
            try:
                return await asyncio.wait_for(ref.server.session.call_tool(ref.tool, args), ref.server.timeout_s)
            except Exception as exc:
                ref.server.status = "down"
                ref.server.error = "Ошибка вызова MCP: " + type(exc).__name__
                self._build_registry()
                raise

    async def reminder_protocol(self, server: McpServer, tool: str, args: dict, *, concurrent: bool = False):
        async def invoke():
            if (not any(s is server for s in self.servers) or server.status != "ok" or server.session is None
                    or not self.schedules(server)):
                raise RuntimeError("сервер напоминаний отключён")
            allowed = {"reminders", "_reminder_claim", "_reminder_finish", "cancel"}
            if tool not in allowed:
                raise ValueError("неизвестная команда напоминаний")
            result = await asyncio.wait_for(server.session.call_tool(tool, args), server.timeout_s)
            if result.isError:
                raise RuntimeError("ошибка сервера напоминаний")
            content = result.content[0].text if result.content else ""
            return content if tool == "cancel" else json.loads(content)

        if concurrent:
            # Cancellation must reach a pinned live session while Agent.ask
            # owns the exchange lease; reconfiguration waits on that lease.
            return await invoke()
        async with self.lease():
            return await invoke()

    async def stop(self) -> None:
        names = {s.name for s in self.servers}
        await self._changing(names)
        try:
            async with self.lease():
                for server in self.servers:
                    await self._disconnect(server)
                self.servers, self.tools = [], {}
        finally:
            self._changed(names)

    def view(self) -> dict:
        result = {"servers": [{"name": s.name, "url": s.url, "status": s.status, "error": s.error, "tools": s.view} for s in self.servers]}
        if self.store is not None or self.config["servers"]:
            result["config"] = deepcopy(self.config)
            result["disabled"] = self.disabled
        return result


MANAGER = McpManager()
