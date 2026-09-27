"""Git-сервер: журнал, статус и сводка diff репозитория вызовами git.

Без shell и с таймаутом на каждый вызов. Пустой вывод — честный пустой
ответ, а ругань самого git на несуществующий ref — честный ответ об
ошибке: прятать её значило бы выдумать результат.
"""

import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent.parent
"""Корень репозитория: все три инструмента работают в нём."""

TIMEOUT_S = 15

server = FastMCP("git")


def _git(*args: str) -> str:
    """Один вызов git в корне репо, списком аргументов и без shell."""
    try:
        run = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"git {args[0]} не ответил за {TIMEOUT_S} с"
    if run.returncode != 0:
        # git сам назвал, что не так, — его текст и есть ответ об ошибке.
        return run.stderr.strip() or f"git {args[0]} завершился с кодом {run.returncode}"
    return run.stdout.strip()


@server.tool(description="Последние n коммитов: короткий хэш, дата, первая строка")
def git_log(n: int = 10) -> str:
    return _git("log", f"-{n}", "--format=%h %ad %s", "--date=short")


@server.tool(description="Короткий статус рабочего дерева (porcelain), как есть")
def git_status() -> str:
    return _git("status", "--porcelain")


@server.tool(description="Сводка изменений по ref: git diff --stat <ref>")
def git_diff_stat(ref: str = "HEAD~1") -> str:
    return _git("diff", "--stat", ref)


if __name__ == "__main__":
    server.run()  # stdio: менеджер говорит с процессом по stdin/stdout
