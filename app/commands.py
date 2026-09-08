"""Команды чата. Сегодня она одна — `/прогон`.

Команда — это способ попросить агента сделать что-то помимо разговора:
`/прогон` спавнит субагентов по колонкам сценария и раздаёт им работу.
Разбор вынесен отдельно от HTTP: его проверяют без сети и без сервера.
"""

from __future__ import annotations

from dataclasses import dataclass

RUN_COMMANDS = ("прогон", "run")
"""Имена команды прогона. Русское — основное, английское — чтобы не гадать."""


@dataclass
class Command:
    name: str
    arg: str
    raw: str


def parse(text: str) -> tuple[Command | None, str]:
    """Разбирает ввод пользователя.

    Возвращает пару (команда или None, текст для модели).

    * Команда — только если строка **начинается** со слэша. «Скажи /прогон»
      посреди фразы командой не считается: иначе любой вопрос про команды
      запускал бы прогон.
    * Двойной слэш экранирует: `//прогон` уходит в модель как `/прогон`.
    """
    if not text.startswith("/"):
        return None, text
    if text.startswith("//"):
        return None, text[1:]

    head, _, arg = text[1:].partition(" ")
    return Command(name=head.strip().lower(), arg=arg.strip(), raw=text), text


def is_run(command: Command) -> bool:
    return command.name in RUN_COMMANDS


def resolve_scenario(arg: str, scenarios: list) -> int:
    """Номер сценария из аргумента команды.

    Пусто — единственный сценарий дня (а если их несколько, надо выбрать).
    Число — номер с единицы, как в списке на экране. Иначе — поиск по названию.
    Ошибка — ValueError с текстом, который можно показать пользователю.
    """
    titles = "; ".join(f"{i}. {s.title}" for i, s in enumerate(scenarios, 1))

    if not arg:
        if len(scenarios) == 1:
            return 0
        raise ValueError(f"укажите сценарий: /прогон <номер>. Сценарии дня: {titles}")

    if arg.isdigit():
        index = int(arg) - 1
        if 0 <= index < len(scenarios):
            return index
        raise ValueError(f"сценария {arg} нет. Сценарии дня: {titles}")

    needle = arg.casefold()
    matches = [i for i, s in enumerate(scenarios) if needle in s.title.casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"сценарий «{arg}» не найден. Сценарии дня: {titles}")
    raise ValueError(f"под «{arg}» подходит несколько сценариев. Сценарии дня: {titles}")


def help_text(scenarios: list) -> str:
    lines = ["Команды: /прогон <номер сценария> — запустить сценарий субагентами."]
    for i, scenario in enumerate(scenarios, 1):
        lines.append(f"  {i}. {scenario.title}")
    lines.append("Строка, начинающаяся с // , уходит в модель как обычный текст.")
    return "\n".join(lines)
