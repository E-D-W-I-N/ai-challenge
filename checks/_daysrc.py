"""Читает `day.py` веток дней 1–5 и отдаёт их колонки как объекты.

Ростер Дня 6 собран из этих колонок руками, и сверять его глазами нельзя:
двадцать одна колонка, у каждой полтора десятка полей и промпты по полторы
тысячи символов. Поэтому проверка берёт исходник из ветки и сравнивает
поле за полем.

`Scenario` и `Session` объявлены здесь заново, а не импортируются из
`app.schema`: сценариев в стенде больше нет, а `repeats` и `depends_on` ушли
из конфига. Старому `day.py` они нужны, и держать их в приложении только ради
проверки — это тот самый мёртвый код, которого в демо-репозитории быть
не должно.
"""

from __future__ import annotations

import subprocess
import sys
import types
from dataclasses import dataclass, field


@dataclass
class Session:
    """Колонка в том виде, в каком её знали дни 1–5."""

    label: str
    model: str
    messages: list
    temperature: float | None = None
    max_tokens: int | None = None
    stop: list | None = None
    response_format: dict | None = None
    repeats: int = 1
    depends_on: str | None = None
    note: str = ""
    extra_body: dict = field(default_factory=dict)


@dataclass
class Scenario:
    """Сценарий в том виде, в каком его знали дни 1–5."""

    title: str
    description: str
    sessions: list
    layout: str = "split"
    judge_questions: list = field(default_factory=list)
    judge_model: str | None = None


def source(branch: str) -> str:
    """Текст day.py указанной ветки. Ветка обязана быть на месте."""
    result = subprocess.run(
        ["git", "show", f"{branch}:day.py"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"не читается {branch}:day.py — {result.stderr.strip()}. "
            "Проверьте, что ветки дней есть локально: git fetch origin"
        )
    return result.stdout


def columns(branch: str) -> list[Session]:
    """Все колонки ветки подряд, в порядке сценариев и колонок внутри них."""
    shim = types.ModuleType("app.schema")
    shim.Session = Session
    shim.Scenario = Scenario
    package = types.ModuleType("app")
    package.schema = shim

    saved = {name: sys.modules.get(name) for name in ("app", "app.schema")}
    sys.modules["app"] = package
    sys.modules["app.schema"] = shim
    try:
        namespace: dict = {"__name__": f"day_source_{branch.replace('-', '_')}"}
        exec(compile(source(branch), f"{branch}/day.py", "exec"), namespace)
        scenarios = namespace["SCENARIOS"]
    finally:
        # Настоящий app вернуть обязательно: иначе следующая проверка получит
        # заглушку вместо приложения и упадёт в самом неочевидном месте.
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return [session for scenario in scenarios for session in scenario.sessions]


def split_messages(messages: list[dict]) -> tuple[str, str]:
    """Делит messages колонки на системную часть и вопрос — как при переносе.

    Системные сообщения склеиваются в системный промпт агента, последняя
    реплика пользователя становится черновиком в поле ввода.
    """
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    users = [m["content"] for m in messages if m.get("role") == "user"]
    return system, (users[-1] if users else "")
