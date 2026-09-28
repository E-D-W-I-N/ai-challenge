"""Пайплайн из трёх инструментов: search → summarize → save_file.

Цепочку ведёт модель: никакой автоматики, каждый инструмент видит только
то, что модель положила в аргументы, и цикл вызовов доводит цепочку до
конца без участия человека.

summarize — механический конденсат выдачи search (дедупликация строк,
группировка по файлам, обрезка до `max_items` групп), без LLM
принципиально: сервис не использует ключ модели или импорты приложения,
и проверки обязаны быть офлайн и детерминированными.

save_file пишет только в выделенный оператором каталог результата.
Имя санитизируется: непустое, без «/», «\\» и «..»; отказ — текстом,
и файла не появляется.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path.cwd()  # Explicit CLI --repo overrides this legacy stdio default.
OUTPUT = None

TIMEOUT_S = 15

HIT = re.compile(r"^(?P<file>.*?):(?P<line>\d+):(?P<text>.*)$")
"""Строка выдачи `git grep -n`: «файл:номер: текст»."""

NO_FILE = "(без файла)"
"""Группа строк, не разобранных как «файл:номер: текст»: хранятся как есть."""

server = FastMCP("pipeline")


def files_dir() -> Path:
    """CLI output directory, or the explicit legacy fixture's environment."""
    if OUTPUT is not None:
        return OUTPUT
    raw = os.environ.get("PIPELINE_FILES_DIR")
    return Path(raw) if raw else ROOT / "files"


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русская форма: 1 строка, 3 строки, 5 строк."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _grep(query: str) -> list[str]:
    """Сырой вывод git grep. Код 1 — «ничего не нашлось», а не ошибка.

    `--untracked`: новые, ещё не закоммиченные файлы — тоже репозиторий.
    Игнорируемые Git новые файлы не читаются: правила берутся из выбранного
    репозитория, а не из приложения."""
    try:
        run = subprocess.run(
            ["git", "grep", "-n", "-i", "-F", "--untracked", "-e", query, "--", "."],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [f"ошибка: git grep не ответил за {TIMEOUT_S} с"]
    if run.returncode not in (0, 1):
        return [f"ошибка: {run.stderr.strip() or f'git grep завершился с кодом {run.returncode}'}"]
    return run.stdout.splitlines()


def _sorted_top(lines: list[str], limit: int) -> list[str]:
    """Детерминированная выдача: сортировка по (файл, номер строки), потом
    top-N. git свой порядок не обещает — без сортировки выдача зависела бы
    от потоков git."""

    def key(raw: str):
        hit = HIT.match(raw)
        return (hit["file"], int(hit["line"])) if hit else ("", 0)

    return sorted(lines, key=key)[:limit]


@server.tool(description="Поиск подстроки по репозиторию: git grep -n -i, top-N, детерминированно")
def search(query: str, limit: int = 20) -> str:
    if not query.strip():
        return "пустой запрос: искать нечего"
    if limit < 1:
        return "limit обязан быть больше нуля"
    lines = _grep(query)
    if lines and lines[0].startswith("ошибка:"):
        return lines[0]
    top = _sorted_top(lines, limit)
    if not top:
        return f"по запросу «{query}» ничего не найдено"
    return "\n".join(top)


@server.tool(description="Механический конденсат выдачи search: дедуп, группировка по файлам, лимит групп")
def summarize(text: str, max_items: int = 10) -> str:
    if max_items < 1:
        return "max_items обязан быть больше нуля"
    seen: set[str] = set()
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        hit = HIT.match(line)
        file = hit["file"] if hit else NO_FILE
        if file not in groups:
            groups[file] = []
            order.append(file)
        groups[file].append(line)
    if not order:
        return "пустой вход: конденсировать нечего"
    blocks = []
    for file in order[:max_items]:
        lines = groups[file]
        head = f"{file} — {len(lines)} {_plural(len(lines), 'строка', 'строки', 'строк')}:"
        blocks.append("\n".join([head, *[f"  {line}" for line in lines]]))
    rest = len(order) - max_items
    if rest > 0:
        # Обрезка всегда названная: сколько групп не вошло — сказано.
        blocks.append(f"…ещё {rest} {_plural(rest, 'файл', 'файла', 'файлов')} не показано")
    return "\n\n".join(blocks)


@server.tool(description="Сохранить текст в выделенный каталог; name — только имя файла, без путей")
def save_file(name: str, content: str) -> str:
    if not name.strip() or name != name.strip() or "/" in name or "\\" in name or ".." in name:
        return "отказано: имя обязано быть именем файла — непустое, без «/», «\\» и «..»"
    target = files_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"записано: {target} ({len(content.encode('utf-8'))} байт)"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent pipeline MCP service")
    parser.add_argument("--repo", required=True, help="Git repository context selected by the service operator")
    parser.add_argument("--files-dir", required=True, help="Dedicated output directory selected by the service operator")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8019)
    parser.add_argument("--stdio", action="store_true", help="Explicit legacy fixture mode")
    args = parser.parse_args(argv)
    global ROOT, OUTPUT
    ROOT = Path(args.repo).expanduser().resolve()
    OUTPUT = Path(args.files_dir).expanduser().resolve()
    if not ROOT.is_dir():
        parser.error("--repo: directory does not exist")
    if OUTPUT.exists() and not OUTPUT.is_dir():
        parser.error("--files-dir: must be a directory")
    server.settings.host = args.host
    server.settings.port = args.port
    server.run(transport="stdio" if args.stdio else "streamable-http")


if __name__ == "__main__":
    main()
