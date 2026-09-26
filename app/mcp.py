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
    params: StdioServerParameters
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
                params=StdioServerParameters(
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
                owners[tool.name] = owners.get(tool.name, 0) + 1
        for server in self.servers:
            for tool in server.tools:
                name = f"{server.name}__{tool.name}" if owners[tool.name] > 1 else tool.name
                self.tools[name] = ToolRef(server=server, tool=tool.name)
                server.view.append(
                    {
                        "name": name,
                        "description": tool.description or "",
                        "schema": tool.inputSchema,
                    }
                )

    async def call(self, name: str, args: dict):
        """Вызов инструмента по имени из реестра, с таймаутом его сервера."""
        ref = self.tools.get(name)
        if ref is None:
            raise KeyError(f"инструмента {name!r} нет ни на одном живом сервере")
        assert ref.server.session is not None  # в реестре только живые серверы
        return await asyncio.wait_for(
            ref.server.session.call_tool(ref.tool, args), ref.server.timeout_s
        )

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
        for server in self.servers:
            if server.stack is not None:
                with contextlib.suppress(Exception):
                    await server.stack.aclose()
            if server.process is not None:
                with contextlib.suppress(Exception):
                    await server.process.aclose()
        self.servers = []
        self.tools = {}

    def view(self) -> dict:
        """Список серверов для ручки и вкладки: имя, статус, инструменты."""
        return {
            "servers": [
                {"name": s.name, "status": s.status, "tools": s.view}
                for s in self.servers
            ]
        }


MANAGER = McpManager()
"""Один на приложение: стартует и гаснет вместе с ним (`_lifespan` в main.py)."""
