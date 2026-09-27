"""Read-only Git tools over MCP; repository selected by the service operator."""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

TIMEOUT_S = 15


def create_server(repository: Path, *, host="127.0.0.1", port=8017):
    server = FastMCP("git", host=host, port=port)

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=repository, capture_output=True,
                text=True, timeout=TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"git {args[0]} не ответил за {TIMEOUT_S} с"
        if result.returncode:
            return result.stderr.strip() or f"git {args[0]} завершился с кодом {result.returncode}"
        return result.stdout.strip()

    @server.tool(description="Последние n коммитов: короткий хэш, дата, первая строка")
    def git_log(n: int = 10) -> str:
        if isinstance(n, bool) or not 1 <= n <= 100:
            raise ValueError("n: от 1 до 100")
        return git("log", f"-{n}", "--format=%h %ad %s", "--date=short")

    @server.tool(description="Короткий статус рабочего дерева (porcelain), как есть")
    def git_status() -> str:
        return git("status", "--porcelain")

    @server.tool(description="Сводка изменений по ref: git diff --stat <ref>")
    def git_diff_stat(ref: str = "HEAD~1") -> str:
        if not ref or ref.startswith("-") or any(ord(c) < 32 for c in ref):
            raise ValueError("ref: непустая ревизия Git, без опций команды")
        return git("diff", "--stat", ref, "--")

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent Git MCP service")
    parser.add_argument("--repo", required=True, help="Repository path, independent of the chat app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8017)
    parser.add_argument("--stdio", action="store_true", help="Explicit legacy fixture mode")
    args = parser.parse_args(argv)
    repository = Path(args.repo).expanduser().resolve()
    if not repository.is_dir():
        parser.error("--repo: directory does not exist")
    server = create_server(repository, host=args.host, port=args.port)
    server.run(transport="stdio" if args.stdio else "streamable-http")


if __name__ == "__main__":
    main()
