"""Разговор с агентом из консоли — без браузера и без запущенного сервера.

Это самое короткое доказательство главных требований дня: агент самостоятелен,
веб-слой ему не нужен, и его память переживает перезапуск. Тот же класс `Agent`,
тот же `stream_completion`, та же история — просто вывод идёт в терминал.

    .venv/bin/python -m app.cli
    .venv/bin/python -m app.cli --model openai/gpt-4o-mini --history-limit 0
    echo "привет" | .venv/bin/python -m app.cli --once

Демонстрация памяти между запусками — два запуска подряд, разные процессы:

    .venv/bin/python -m app.cli                       # представьтесь, запомните id
    .venv/bin/python -m app.cli --session ag_00001    # спросите, как вас зовут

Команды внутри диалога: /выход, /история, /забыть, /агенты, /сессии.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import llm, store
from .agent import Agent, AgentBusyError
from .config import has_key
from .registry import REGISTRY
from .schema import AgentSpec
from .store import StoreBusyError

DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_SYSTEM = "Ты — агент стенда AI-челленджа. Отвечай по-русски, коротко и по делу."


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Разговор с агентом из консоли: тот же класс, что и в стенде.",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"id модели (по умолчанию {DEFAULT_MODEL})")
    parser.add_argument("--system", default=DEFAULT_SYSTEM, help="системный промпт агента")
    parser.add_argument("--label", default="CLI", help="имя агента в реестре")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument(
        "--history-limit",
        type=int,
        default=None,
        help="сколько сообщений истории уходит в модель; 0 — агент без памяти",
    )
    parser.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="продолжить сохранённую сессию по её id вместо создания новой",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="прочитать один вопрос со stdin, ответить и выйти",
    )
    return parser.parse_args(argv)


def build_agent(args: argparse.Namespace) -> Agent:
    """Агент по аргументам командной строки. Он же кладётся в реестр процесса.

    С `--session` агент не создаётся, а поднимается из базы: конфиг и история
    приезжают оттуда, аргументы командной строки к нему уже не применяются —
    иначе продолжение разговора молча сменило бы модель на дефолтную.
    """
    if args.session:
        agent = REGISTRY.load(args.session)
        if agent is None:
            raise SystemExit(
                f"сессии {args.session} нет в базе ({store.db_path()}). "
                "Посмотреть сохранённые: python -m app.cli, затем /сессии"
            )
        return agent

    spec = AgentSpec(
        label=args.label,
        model=args.model,
        messages=[],
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        system=args.system,
        history_limit=args.history_limit,
    )
    return REGISTRY.create(spec)


async def ask(agent: Agent, text: str, out=sys.stdout) -> str:
    """Один обмен: печатает ответ по мере генерации, возвращает его целиком."""
    answer = ""
    error: str | None = None
    async for event in agent.ask(text):
        kind = event["type"]
        if kind == "delta":
            answer += event["text"]
            out.write(event["text"])
            out.flush()
        elif kind in ("repeat_error", "error"):
            error = event["message"]
        elif kind == "done":
            answer = event["text"] or answer
    out.write("\n")
    if error:
        out.write(f"[ошибка] {error}\n")
    out.flush()
    return answer


def _print_history(agent: Agent, out=sys.stdout) -> None:
    if not agent.history:
        out.write("[история пуста]\n")
        return
    for turn in agent.history:
        mark = " (оборван)" if turn.error else ""
        out.write(f"{turn.role}{mark}: {turn.content}\n")


def _print_sessions(out=sys.stdout) -> None:
    """Сохранённые сессии — те, что переживут перезапуск."""
    sessions = REGISTRY.sessions(limit=30)
    if not sessions:
        out.write("[сохранённых сессий нет]\n")
        return
    out.write(f"сохранённых сессий: {len(sessions)} · база {store.db_path()}\n")
    for row in sessions:
        mark = "живая" if row["live"] else "в базе"
        out.write(
            f"  {row['id']}  {row['label']}  {row['config'].get('model', '')}  "
            f"реплик {row['history_len']}  ({mark})\n"
        )
    out.write("Продолжить: python -m app.cli --session <id>\n")


def _print_agents(out=sys.stdout) -> None:
    out.write(f"живых агентов: {len(REGISTRY)} (потолок {REGISTRY.max_agents})\n")
    for agent in REGISTRY.list():
        out.write(f"  {agent.id}  {agent.spec.label}  {agent.spec.model}  реплик {len(agent.history)}\n")


async def repl(agent: Agent, *, once: bool = False, out=sys.stdout) -> int:
    """Цикл «вопрос — ответ». Возвращает код возврата процесса."""
    out.write(f"агент {agent.id} · {agent.spec.model} · окно памяти {agent.history_limit}\n")
    if agent.history:
        # Ради этой строки день и делался: процесс новый, разговор старый.
        out.write(
            f"[продолжаем] в базе {len(agent.history)} реплик — "
            "агент помнит этот разговор с прошлого запуска\n"
        )
    else:
        out.write(f"[новая сессия] продолжить её потом: --session {agent.id}\n")
    if not has_key():
        out.write("[нет ключа] OPENROUTER_API_KEY не найден — вызова не будет.\n")
    if not once:
        out.write("Команды: /выход, /история, /забыть, /агенты, /сессии\n")

    while True:
        if not once:
            out.write("\n> ")
            out.flush()
        line = sys.stdin.readline()
        if not line:
            return 0
        text = line.strip()
        if not text:
            if once:
                return 0
            continue

        if text in ("/выход", "/exit", "/quit"):
            return 0
        if text in ("/история", "/history"):
            _print_history(agent, out)
            continue
        if text in ("/забыть", "/forget"):
            agent.forget()
            out.write("[история очищена]\n")
            continue
        if text in ("/агенты", "/agents"):
            _print_agents(out)
            continue
        if text in ("/сессии", "/sessions"):
            _print_sessions(out)
            continue

        try:
            await ask(agent, text, out)
        except AgentBusyError as exc:
            out.write(f"[занят] {exc}\n")
        except StoreBusyError as exc:
            # Текст уже объясняет, что делать: имя класса ничего не добавит.
            out.write(f"[база занята] {exc}\n")
        except Exception as exc:  # noqa: BLE001 — консоль не должна падать стеком
            out.write(f"[ошибка] {type(exc).__name__}: {exc}\n")

        if once:
            return 0


async def _run(args: argparse.Namespace) -> int:
    agent = build_agent(args)
    try:
        return await repl(agent, once=args.once)
    finally:
        # Общий httpx-клиент открыт на процесс — закрываем его сами.
        await llm.aclose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
