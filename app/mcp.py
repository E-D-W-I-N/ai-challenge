"""Подключение MCP: соединение по stdio и список инструментов.

По конфигу (`mcp.json` в корне или путь из `MCP_CONFIG_PATH`) менеджер
поднимает по процессу на сервер, здоровается по протоколу и забирает
`tools/list`. Сервер, упавший на рукопожатии, помечается down — приложение
стартует дальше. Конфига нет или `MCP_DISABLED=1` — менеджер пуст,
и приложение работает в точности как без него.

Процессы запускаем сами (`anyio.open_process`), а не `stdio_client` из SDK:
`stop()` обязан гасить их сам — terminate, через две секунды kill, — и без
ручки на процесс ни это, ни проверка «exitcode не None» не выразить.
Протокол при этом весь на SDK: `ClientSession` ведёт initialize, tools/list
и tools/call.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import anyio
import anyio.abc
import anyio.streams.text
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.message import SessionMessage

ROOT = Path(__file__).resolve().parent.parent
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
    """Один сервер из конфига: процесс, сессия и то, что про него известно."""

    name: str
    timeout_s: float
    params: StdioServerParameters | None = None
    url: str = ""
    status: str = "down"
    error: str = ""
    tools: list = field(default_factory=list)  # types.Tool, как ответил сервер
    view: list = field(default_factory=list)  # [{name, description, schema}]
    process: anyio.abc.Process | None = None
    session: ClientSession | None = None
    stack: contextlib.AsyncExitStack | None = None


@dataclass
class ToolRef:
    """Инструмент в реестре: его имя на сервере (без префикса) и чей он."""

    server: McpServer
    tool: str


class McpManager:
    """Серверы MCP из конфига и реестр их инструментов.

    Пустой менеджер — законное состояние: конфига нет, `MCP_DISABLED=1` или
    все серверы лежат. Пустой менеджер неотличим от приложения без MCP.
    """

    def __init__(self) -> None:
        self.servers: list[McpServer] = []
        self.tools: dict[str, ToolRef] = {}
        self.context_namespace = ""

    @staticmethod
    def schedules(server: McpServer) -> bool:
        return {"_reminder_claim", "_reminder_finish"} <= {t.name for t in server.tools}

    def scheduling_tool(self, name: str) -> bool:
        ref = self.tools.get(name)
        return ref is not None and ref.tool == "remind" and self.schedules(ref.server)

    def context_for(self, chat_id: str) -> str:
        return self.context_namespace + "/" + chat_id

    async def start(self) -> None:
        if os.environ.get("MCP_DISABLED") == "1":
            return
        raw = os.environ.get("MCP_CONFIG_PATH")
        path = Path(raw) if raw else ROOT / "mcp.json"
        if not path.is_file():
            return
        config = json.loads(path.read_text(encoding="utf-8"))
        for name, spec in config.get("servers", {}).items():
            server = McpServer(
                name=name,
                timeout_s=float(spec.get("timeout_s", 10)),
                url=spec.get("url", ""),
                params=None if spec.get("url") else StdioServerParameters(
                    # Команда — всегда тот интерпретатор, что крутит приложение:
                    # системный python3.9 сервер не поднял бы.
                    command=sys.executable,
                    args=["-m", spec["module"]],
                    env=_child_env(),
                    cwd=ROOT,
                ),
            )
            await self._connect(server)
            self.servers.append(server)
        self._build_registry()

    async def _connect(self, server: McpServer) -> None:
        """Процесс, рукопожатие и список инструментов одного сервера.

        Упал — status down и управление возвращается: старт приложения
        не держится на чужом процессе.
        """
        if server.url:
            stack = contextlib.AsyncExitStack()
            try:
                read, write, _ = await stack.enter_async_context(
                    streamablehttp_client(server.url, timeout=server.timeout_s)
                )
                server.session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(server.session.initialize(), CONNECT_TIMEOUT_S)
                result = await asyncio.wait_for(server.session.list_tools(), CONNECT_TIMEOUT_S)
                server.tools = list(result.tools)
                server.status, server.stack = "ok", stack
            except Exception as exc:
                server.error = f"{type(exc).__name__}: {exc}"
                with contextlib.suppress(Exception):
                    await stack.aclose()
            return
        assert server.params is not None
        process = await anyio.open_process(
            [server.params.command, *server.params.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=server.params.env,
            cwd=server.params.cwd,
        )
        server.process = process
        stack = contextlib.AsyncExitStack()
        try:
            read_send, read_recv = anyio.create_memory_object_stream(0)
            write_send, write_recv = anyio.create_memory_object_stream(0)
            group = await stack.enter_async_context(anyio.create_task_group())
            group.start_soon(_pump_stdout, process, read_send)
            group.start_soon(_pump_stdin, process, write_recv)
            server.session = await stack.enter_async_context(
                ClientSession(read_recv, write_send)
            )
            await asyncio.wait_for(server.session.initialize(), CONNECT_TIMEOUT_S)
            result = await asyncio.wait_for(server.session.list_tools(), CONNECT_TIMEOUT_S)
            server.tools = list(result.tools)
            server.status = "ok"
            server.stack = stack
        except Exception as exc:  # noqa: BLE001 — down, а не падение приложения
            server.error = f"{type(exc).__name__}: {exc}"
            await _kill(process)
            with contextlib.suppress(Exception):
                await stack.aclose()
            with contextlib.suppress(Exception):
                await process.aclose()

    def _build_registry(self) -> None:
        """Реестр `имя → инструмент`. Уникальное имя — как есть; коллизия —
        оба с префиксом сервера, голого имени не остаётся."""
        owners: dict[str, int] = {}
        for server in self.servers:
            for tool in server.tools:
                if self.schedules(server) and tool.name.startswith("_reminder_"):
                    continue
                owners[tool.name] = owners.get(tool.name, 0) + 1
        for server in self.servers:
            for tool in server.tools:
                if self.schedules(server) and tool.name.startswith("_reminder_"):
                    continue
                name = f"{server.name}__{tool.name}" if owners[tool.name] > 1 else tool.name
                self.tools[name] = ToolRef(server=server, tool=tool.name)
                schema = copy.deepcopy(tool.inputSchema)
                if self.schedules(server) and tool.name in {"remind", "cancel"}:
                    schema.get("properties", {}).pop("context_id", None)
                server.view.append(
                    {
                        "name": name,
                        "description": tool.description or "",
                        "schema": schema,
                    }
                )

    async def call(self, name: str, args: dict, *, chat_id: str | None = None):
        """Вызов инструмента по имени из реестра, с таймаутом его сервера."""
        ref = self.tools.get(name)
        if ref is None:
            raise KeyError(f"инструмента {name!r} нет ни на одном живом сервере")
        assert ref.server.session is not None  # в реестре только живые серверы
        if chat_id is not None and self.schedules(ref.server) and ref.tool in {"remind", "cancel"}:
            args = {**args, "context_id": self.context_for(chat_id)}
        return await asyncio.wait_for(
            ref.server.session.call_tool(ref.tool, args), ref.server.timeout_s
        )

    async def reminder_protocol(self, server: McpServer, tool: str, args: dict):
        result = await asyncio.wait_for(server.session.call_tool(tool, args), server.timeout_s)
        if result.isError:
            raise RuntimeError(result.content)
        return json.loads(result.content[0].text)

    async def stop(self) -> None:
        """Гасит все процессы: terminate всем, через две секунды kill тем,
        кто не послушался."""
        alive = [
            s
            for s in self.servers
            if s.process is not None and s.process.returncode is None
        ]
        for server in alive:
            server.process.terminate()
        for server in alive:
            try:
                await asyncio.wait_for(server.process.wait(), KILL_AFTER_S)
            except asyncio.TimeoutError:
                await _kill(server.process)
        # Стеки — строго в обратном порядке: скоупы anyio выходят только
        # LIFO, а два сервера входили в них по очереди, в одну задачу.
        # Прямой порядок ломал стек скоупов **молча**, под suppress — и
        # падал уже портал на своём выходе, далеко от причины.
        for server in reversed(self.servers):
            if server.stack is not None:
                with contextlib.suppress(Exception):
                    await server.stack.aclose()
            if server.process is not None:
                with contextlib.suppress(Exception):
                    await server.process.aclose()
        self.servers = []
        self.tools = {}

    async def view(self) -> dict:
        """Список серверов для ручки и вкладки: имя, статус, инструменты.

        У живого сервера с инструментом `reminders` — и его свежий результат
        полем `reminders`: напоминания показывают рядом с инструментами, и
        второй ручки для этого не заводится. У лежачего сервера поля нет —
        дёргать его нечем, а честнее молчащего поля — его отсутствие.
        """
        servers = []
        for s in self.servers:
            row = {"name": s.name, "status": s.status, "tools": s.view}
            name = next(
                (n for n, ref in self.tools.items() if ref.server is s and ref.tool == "reminders"),
                None,
            )
            if s.status == "ok" and name is not None:
                with contextlib.suppress(Exception):
                    result = await self.call(name, {})
                    row["reminders"] = json.loads(result.content[0].text)
            servers.append(row)
        return {"servers": servers}


MANAGER = McpManager()
"""Один на приложение: стартует и гаснет вместе с ним (`_lifespan` в main.py)."""
