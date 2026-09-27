"""Пайплайн из трёх инструментов: search → summarize → save_file.

Цепочку ведёт модель: никакой автоматики, каждый инструмент видит только
то, что модель положила в аргументы, и цикл вызовов доводит цепочку до
конца без участия человека.

summarize — механический конденсат выдачи search (дедупликация строк,
группировка по файлам, обрезка до `max_items` групп), без LLM
принципиально: ключа в этом процессе нет (белый список CHILD_ENV_KEYS),
и проверки обязаны быть офлайн и детерминированными.

save_file пишет только в `files/` — единственное файловое место записи.
Имя санитизируется: непустое, без «/», «\\» и «..»; отказ — текстом,
и файла не появляется.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parent.parent.parent

TIMEOUT_S = 15

HIT = re.compile(r"^(?P<file>.*?):(?P<line>\d+):(?P<text>.*)$")
"""Строка выдачи `git grep -n`: «файл:номер: текст»."""

NO_FILE = "(без файла)"
"""Группа строк, не разобранных как «файл:номер: текст»: хранятся как есть."""

server = FastMCP("pipeline")


def files_dir() -> Path:
    """Куда пишет save_file: из PIPELINE_FILES_DIR, по умолчанию — files/."""
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
    Игнорируемые git не читаются ни без него, ни с ним: `.env`, `files/`
    и `.venv` в выдачу не попадают."""
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


@server.tool(description="Сохранить текст в файл внутри files/; name — только имя файла, без путей")
def save_file(name: str, content: str) -> str:
    if not name.strip() or name != name.strip() or "/" in name or "\\" in name or ".." in name:
        return "отказано: имя обязано быть именем файла — непустое, без «/», «\\» и «..»"
    target = files_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"записано: {target} ({len(content.encode('utf-8'))} байт)"


if __name__ == "__main__":
    server.run()  # stdio: менеджер говорит с процессом по stdin/stdout
