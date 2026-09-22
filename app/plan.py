"""Состояние задачи: **план и есть состояние**.

Список шагов со статусами и три флажка; этап, текущий шаг и ожидаемое
действие из них **вычисляются** (`stage_of`), а не хранятся рядом — ярлык
этапа расходился бы с работой молча. Модуль без состояния: чистые функции
над словарём, только правила переходов и вид плана для модели.
"""

from __future__ import annotations

STEP_STATUSES = ("pending", "in_progress", "done")
"""Что бывает с шагом. Они же стоят в `enum` описания инструмента: модель
выбирает из того же списка, из которого проверяет код."""

STAGES = ("planning", "approval", "execution", "validation", "done", "paused")
"""В порядке прохождения; `paused` последним — пауза ложится поверх любого
этапа. Ни один не хранится: этап это ответ `stage_of`."""


def empty() -> dict:
    """Пустое состояние задачи. Флажки хранятся, а не вычисляются: «план
    утверждён» обязано пережить возврат шага в `pending`. Поля «с какого
    этапа приостановлено» рядом нет намеренно: сняли флажок — этап
    восстановился сам, список не менялся."""
    return {"steps": [], "approved": False, "finished": False, "paused": False}


def _steps(plan) -> list[dict]:
    """Пустой список у чего угодно кривого: читают модуль и ручки,
    и инструмент, и подъём из базы, а падать на чужой форме нельзя."""
    steps = (plan or {}).get("steps")
    return [step for step in steps if isinstance(step, dict)] if isinstance(steps, list) else []


def current_step(plan) -> int | None:
    """Номер текущего шага (с нуля) или `None`. Текущий — `in_progress`;
    его нет — первый не-`done`. Одно место на оба правила: второе назвало бы
    в промпте не тот шаг, что интерфейс."""
    steps = _steps(plan)
    for index, step in enumerate(steps):
        if step.get("status") == "in_progress":
            return index
    for index, step in enumerate(steps):
        if step.get("status") != "done":
            return index
    return None


def stage_of(plan) -> tuple[str, int | None]:
    """Этап задачи и номер текущего шага — **единственное** место, где этап
    вычисляется.

    Разбор упорядоченный, а не по таблице: условия этапов пересекаются —
    неутверждённый план со всеми шагами `done` подходит и под `approval`,
    и под `validation`, а пойди разбор по второму, кнопка «Утвердить»
    ответила бы 409. Порядок задан явно, сверху вниз. Пауза первой строкой:
    приостановленная задача остаётся такой, каким бы ни был список.
    """
    steps = _steps(plan)
    current = current_step(plan)
    if (plan or {}).get("paused"):
        return "paused", current
    if (plan or {}).get("finished"):
        return "done", current
    if not steps:
        return "planning", None
    if not (plan or {}).get("approved"):
        return "approval", current
    if all(step.get("status") == "done" for step in steps):
        return "validation", current
    return "execution", current


_MARKS = {"done": "[x]", "in_progress": "[>]"}
"""Чем помечен шаг в списке для модели. Неизвестный статус — пустая рамка:
план читается и из файла, который завела чужая версия."""


def plan_lines(plan) -> str:
    """План так, как его **видит модель**, — одной картой и в блоке промпта,
    и в результате инструмента: `план утверждён: да`, затем
    `1. [x] собрать требования`, `2. [>] схема базы ← в работе`. Две формы
    одного списка разъехались бы молча. Номера с единицы — их называет
    правило этапа."""
    approved = bool((plan or {}).get("approved"))
    lines = ["план утверждён: " + ("да" if approved else "нет")]
    if (plan or {}).get("finished"):
        lines.append("задача завершена: да")
    # Пауза названа и здесь: отказ отдаёт план этой же функцией.
    if (plan or {}).get("paused"):
        lines.append("задача на паузе: да")
    steps = _steps(plan)
    if not steps:
        lines.append("шагов ещё нет")
        return "\n".join(lines)
    for index, step in enumerate(steps, start=1):
        status = step.get("status")
        mark = _MARKS.get(status, "[ ]")
        tail = " ← в работе" if status == "in_progress" else ""
        lines.append(f"{index}. {mark} {step.get('title', '')}{tail}")
    return "\n".join(lines)


UPDATE_PLAN_DESCRIPTION = (
    "Список шагов задачи целиком — твоя рабочая память по задаче. Зови ВСЕГДА, "
    "когда: (1) получил задачу и плана ещё нет — разложи её на 3–7 конкретных "
    "шагов, все со статусом pending; (2) человек попросил поправить план; "
    "(3) начинаешь шаг — поставь ему in_progress, и только одному; (4) закончил "
    "шаг — поставь ему done, а следующему in_progress; (5) проверка нашла "
    "проблему — верни нужному шагу pending. Передавай ВЕСЬ список каждый раз, "
    "а не изменения: чего нет в списке, того в плане не станет. done ставь только "
    "тому, что правда сделано в этом разговоре."
)
"""**Проверенный на живой модели текст**, слово в слово: `openai/gpt-4o-mini`
по нему зовёт инструмент и возвращает готовый план. Менять нельзя ни слова,
не прогнав живой запрос: описание инструмента — это промпт."""

FINISH_TASK_DESCRIPTION = (
    "Зови РОВНО ТОГДА, когда план утверждён и все его шаги отмечены done: "
    "это проверка сделанного. Перед вызовом перечитай результат по каждому "
    "шагу и найди пробелы, которые влияют на правильность результата, — "
    "не стиль, не оформление, не мелочи. Каждая проблема — одна строка: что "
    "не так и в каком шаге. Нашёл проблемы — передай их списком: задача "
    "останется незавершённой, а ты обязан вернуть нужный шаг в pending через "
    "update_plan и исправить. Проблем нет — передай пустой список: задача "
    "завершится. Не зови, пока есть шаги не done."
)
"""Просим **перечень проблем**, а не «годно?»: попросишь одобрить — одобрит.
Судья обязан предъявлять улику, а не вердикт. Разбора текста нет вовсе:
форму массива держит провайдер."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_plan",
            "description": UPDATE_PLAN_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Что сделать, одной строкой",
                                },
                                "status": {"type": "string", "enum": list(STEP_STATUSES)},
                            },
                            "required": ["title", "status"],
                        },
                    }
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish_task",
            "description": FINISH_TASK_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {"problems": {"type": "array", "items": {"type": "string"}}},
                "required": ["problems"],
            },
        },
    },
]
"""Два инструмента в формате OpenRouter. Объявляются только чату с включённым
рабочим процессом (`Agent.tool_specs`); исполняет вызовы `Agent.run_tool`,
переходы решает `apply`."""

FORCE_UPDATE_PLAN = {"type": "function", "function": {"name": "update_plan"}}
"""Чем `tool_choice` принуждает модель на этапе планирования. Именно эта
функция, а не `"required"`: «любой инструмент» позволило бы позвать
`finish_task`, который на планировании отказан. Когда поле уезжает, решает
`Agent.turn_choice`."""


TOOL_NAMES = tuple(tool["function"]["name"] for tool in TOOLS)
"""Что модели **вправе** позвать — выведено из самого объявления: вторая
таблица имён разошлась бы с `TOOLS` молча. Список и есть ворота
(`Agent.run_tool`): пять из семи действий `apply` — кнопки человека."""

STAGE_RULES = {
    "planning": (
        "Этап: планирование. Твой единственный ответ сейчас — вызов "
        "update_plan со списком шагов, все со статусом pending. Разложи "
        "задачу на 3–7 шагов; маленькую задачу — на 2–3, но не меньше двух. "
        "План утверждает человек кнопкой; до этого ничего не выполняй "
        "и не пиши решение."
    ),
    "approval": (
        "Этап: план ждёт утверждения человеком. Не выполняй шаги. Просят "
        "поправить — перепиши список целиком через update_plan. Утверждает "
        "план человек кнопкой, а не ты: не считай его утверждённым по словам "
        "в переписке."
    ),
    "execution": (
        "Этап: выполнение, шаг {k} из {n} — «{title}». Выполняй текущий шаг. "
        "Перед началом отметь его in_progress, по завершении — done, "
        "а следующему in_progress, всё через update_plan. Не переходи "
        "к следующему, пока текущий не сделан. Когда все шаги done — проверь "
        "работу и позови finish_task."
    ),
    "validation": (
        "Этап: проверка. Все шаги отмечены сделанными. Перечитай результат "
        "и позови finish_task: перечисли пробелы, влияющие на правильность, — "
        "не стиль и не оформление; нет таких — передай пустой список. Если "
        "передал проблемы, верни нужный шаг в pending через update_plan "
        "и исправь."
    ),
    "done": (
        "Этап: задача завершена. Отвечай на вопросы по сделанному. План менять "
        "нельзя: переоткрыть задачу может только человек, кнопкой."
    ),
    "paused": (
        "Этап: работа над задачей ПРИОСТАНОВЛЕНА человеком. Не выполняй шаги, "
        "не предлагай следующие, не продолжай работу — даже если тебя просят "
        "продолжить. План не меняй. Спросят про задачу — ответь одной фразой, "
        "что она на паузе, и что снять паузу может только человек, кнопкой."
    ),
}
"""Правило этапа — повелительно, по одному на этап, и **у `done` и `paused`
оно непустое**: модель без распоряжения продолжает править план.

Едет **системным** сообщением — это распоряжение; список шагов едет `user`,
это сведения. Правило паузы названо жёстко («даже если тебя просят
продолжить»), но держится пауза не на нём — оба инструмента отказаны
кодом (`apply`)."""


def stage_rule(plan) -> str:
    """Правило этапа — уже с номерами. Подставляются они здесь, а не
    у вызывающего: вторая подстановка разошлась бы с первой молча."""
    stage, current = stage_of(plan)
    rule = STAGE_RULES[stage]
    if stage != "execution" or current is None:
        return rule
    steps = _steps(plan)
    return rule.format(k=current + 1, n=len(steps), title=steps[current].get("title", ""))


class PlanError(Exception):
    """Переход отказан. Внутри не код ошибки, а текст-директива — целиком
    тот, что уедет модели: получившая «ошибка» объявляет успех и идёт дальше.
    Причина первым аргументом, **выход** — вторым и необязательный: обычно
    он один на все причины действия (`ALLOWED`), но у `finish_task`
    с неразобранным `problems` общий советовал бы уже сделанное."""

    def __init__(self, reason: str, allowed: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.allowed = allowed


def refusal(name: str, plan, reason: str, allowed: str) -> str:
    """Отказ так, как его читает модель, — одной формулой на все причины.
    Четыре части: что не выполнено, почему, что запрещено утверждать и что
    допустимо. Плюс текущий план целиком."""
    return (
        f"{name} НЕ выполнен, состояние не изменилось: {reason}. "
        f"НЕ утверждай, что это сделано. Допустимо: {allowed}. "
        f"Текущий план:\n{plan_lines(plan)}"
    )


BROKEN_ARGS = "аргументы пришли не разбираемой строкой, это не JSON"
"""Причина, до которой `apply` не доходит: строку аргументов разбирает
`Agent.run_tool`. Текст здесь, рядом с остальными причинами: то, что
прочитает модель, живёт в одном месте."""

def broken_args(name: str, plan, reason: str = BROKEN_ARGS) -> str:
    """Отказ на неразобранные аргументы — той же формулой и в том же месте."""
    return refusal(
        name, plan, reason, "позови инструмент заново и передай аргументы одним объектом JSON"
    )


_TAILS = {
    "approval": "план не утверждён, жди кнопки",
    "validation": "все шаги сделаны — проверь и позови finish_task",
}
"""Чем кончается результат `update_plan` — по этапу, в который план попал.
У `execution` хвост свой, с номером шага, и собирается он ниже."""


def _one_step(raw) -> dict:
    """Один шаг из присланного — или `PlanError` с назвавшей себя причиной.
    Причины у заголовка и у статуса разные намеренно: «план не записан» без
    указания, что не так, модель исправляет наугад."""
    title = raw.get("title") if isinstance(raw, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise PlanError("у шага нет заголовка title или он пустой")
    status = raw.get("status")
    if status not in STEP_STATUSES:
        raise PlanError(
            f"у шага «{title.strip()}» статус {status!r}, "
            f"а бывает только {', '.join(STEP_STATUSES)}"
        )
    return {"title": title.strip(), "status": status}


def _new_steps(args) -> list[dict]:
    """Любая кривизна — `PlanError` с причиной внутри; в директиву её
    собирает `apply`, где известно имя инструмента и текущий план."""
    raw = (args or {}).get("steps")
    if not isinstance(raw, list) or not raw:
        raise PlanError("список шагов пустой, а план без шагов — это не план")
    steps = [_one_step(item) for item in raw]
    running = [step for step in steps if step["status"] == "in_progress"]
    if len(running) > 1:
        # «Ровно один in_progress» — правило промпта: свежий план «все
        # pending» законен. Кода здесь столько, чтобы не было двух текущих
        # сразу — иначе `current_step` назвал бы один, а модель вела другой.
        raise PlanError(
            f"in_progress отмечено шагов: {len(running)}, а в работе бывает только один"
        )
    return steps


def _updated(plan: dict, args) -> tuple[dict, str]:
    """`update_plan`: переписывает список шагов целиком. Утверждённый план
    тоже: отметки и возврат шага в `pending` — это ведение работы, и
    `approved` не снимается. Запрещено только у завершённой задачи."""
    steps = _new_steps(args)
    # Список стал короче утверждённого — утверждение снимается: иначе модель,
    # приславшая вместо трёх шагов один со статусом `done`, получила бы
    # `validation` и закрыла задачу, которой никто не делал. Сравнивается
    # **число** шагов, а не состав: сверка по заголовкам читала бы
    # переименование как «удалили один, добавили другой». И не запрет —
    # попросившему убрать шаг остался бы один выход, `reset`.
    revoked = plan["approved"] and len(steps) < len(_steps(plan))
    fresh = {**plan, "steps": steps}
    if revoked:
        fresh["approved"] = False
    stage, current = stage_of(fresh)
    done = sum(1 for step in steps if step["status"] == "done")
    running = next(
        (i for i, step in enumerate(steps) if step["status"] == "in_progress"), None
    )
    head = (
        f"План записан: {len(steps)} шагов, сделано {done}, "
        + ("в работе: шаг %d." % (running + 1) if running is not None else "в работе: нет.")
    )
    tail = _TAILS.get(stage)
    if stage == "execution" and current is not None:
        tail = f"выполняй шаг {current + 1}"
    if revoked:
        # Снятое утверждение обязано быть названо: молча вернуть план
        # к ожиданию кнопки — оставить модель ждать неизвестно чего.
        tail = "Шагов стало меньше, чем утверждал человек: план снова ждёт утверждения."
    # Результат — **всегда весь список**, а не «ок»: он возвращается модели
    # в контекст и работает её рабочей памятью по задаче.
    return fresh, f"{head}\n{plan_lines(fresh)}" + (f"\n{tail}" if tail else "")


def _finished(plan: dict, args) -> tuple[dict, str]:
    """`finish_task`: проверка сделанного. Только на `validation`, и оба
    условия проверяются по отдельности: позвавшая раньше времени модель
    обязана прочитать, чего именно не хватает."""
    if not plan.get("approved"):
        raise PlanError("план ещё не утверждён человеком, проверять нечего")
    steps = _steps(plan)
    unfinished = [step for step in steps if step.get("status") != "done"]
    if not steps or unfinished:
        raise PlanError(
            f"шагов не done: {len(unfinished) or len(steps)} — "
            "сначала сделай их и отметь через update_plan"
        )
    problems = (args or {}).get("problems")
    if not isinstance(problems, list):
        # Выход называется здесь: до этой причины доходят с утверждённым
        # планом и всеми шагами `done`, и общий советовал бы сделанное.
        raise PlanError(
            "problems прислан не списком, а перечень проблем обязателен",
            "позови finish_task заново и передай problems массивом строк; "
            "проблем нет — пустым массивом",
        )
    named = [str(item).strip() for item in problems if str(item).strip()]
    if named:
        # Флаг как был: задача не завершена. Проблемы не хранятся — они
        # уезжают модели в контекст, ей их и исправлять.
        listed = "; ".join(named)
        return plan, (
            f"Записано проблем: {len(named)}. Задача НЕ завершена. "
            f"Верни нужный шаг в pending через update_plan и исправь: {listed}"
        )
    return {**plan, "finished": True}, "Задача завершена. План больше не меняй."


def _approved(plan: dict) -> tuple[dict, str]:
    """`approve`: кнопка человека. Только на этапе `approval` — повторный
    отказан: «утверждено дважды» неотличимо от «утверждено не то»."""
    stage, _ = stage_of(plan)
    if stage != "approval":
        raise PlanError(f"утверждать нечего: этап сейчас {stage}, а не approval")
    fresh = {**plan, "approved": True}
    return fresh, f"План утверждён человеком.\n{plan_lines(fresh)}"


def _reopened(plan: dict) -> tuple[dict, str]:
    """`reopen`: человек переоткрывает завершённую задачу. Шаги остаются
    `done`, этап становится `validation`: переоткрыли не ради «всё заново».
    Обмен при этом не отправляется — что не так, человек напишет сам."""
    if not plan.get("finished"):
        raise PlanError("переоткрывать нечего: задача не завершена")
    fresh = {**plan, "finished": False}
    return fresh, f"Задача переоткрыта человеком.\n{plan_lines(fresh)}"


def _paused(plan: dict) -> tuple[dict, str]:
    """`pause`: человек приостанавливает работу. Только когда она идёт:
    повторная пауза неотличима от «пауза не сработала»."""
    if plan.get("paused"):
        raise PlanError("задача уже на паузе")
    if plan.get("finished"):
        raise PlanError("задача завершена, приостанавливать нечего")
    fresh = {**plan, "paused": True}
    return fresh, f"Работа приостановлена человеком.\n{plan_lines(fresh)}"


def _resumed(plan: dict) -> tuple[dict, str]:
    """`resume`: человек снимает паузу. Только флажок — этап вернётся тот же,
    он вычисляется из списка, а список не менялся."""
    if not plan.get("paused"):
        raise PlanError("задача не на паузе, снимать нечего")
    fresh = {**plan, "paused": False}
    return fresh, f"Пауза снята человеком.\n{plan_lines(fresh)}"


def _reset(plan: dict) -> tuple[dict, str]:
    """`reset`: пустой список и **все три флажка** сняты. Законен всегда:
    на нём держится «начать другую задачу» и «вернуться в обычный чат».
    Оставленный флажок соврал бы про этап с первого же сообщения."""
    return empty(), "Задача сброшена человеком: плана больше нет."


ALLOWED = {
    "update_plan": "перепиши список шагов целиком через update_plan",
    "finish_task": "дождись, когда план утвердят и все шаги будут done",
    "approve": "утверждать можно только план, который ждёт утверждения",
    "reopen": "переоткрыть можно только завершённую задачу",
    "pause": "приостановить можно только незавершённую работу",
    "resume": "снять паузу можно только с приостановленной задачи",
    "reset": "сбросить задачу можно всегда",
}
"""Что допустимо вместо отказанного — по действию. Директива без выхода
оставляет модель в тупике, а тупик она обходит объявлением успеха. Имя
без подчёркивания: таблица входит в договор, и проверка спрашивает у отказа
дословный выход из неё. Причина вправе назвать свой (`PlanError`)."""


def apply(plan, action: str, args=None) -> tuple[dict, str]:
    """**Единственный источник истины переходов** — один и на инструмент,
    и на кнопку человека. Второй проверки не заводить: две копии правил
    расходятся молча, и тогда кнопка разрешает запрещённое инструменту.

    Действий семь: два зовёт модель, пять — человек; паузу ни один инструмент
    не трогает. Отдаёт `(новый план, текст)`; отказ — `PlanError` с готовой
    директивой. Присланный план не меняется, а записывает новый вызывающий —
    сам `apply` в базу не ходит.
    """
    plan = plan if isinstance(plan, dict) else empty()
    plan = {
        "steps": _steps(plan),
        "approved": bool(plan.get("approved")),
        "finished": bool(plan.get("finished")),
        "paused": bool(plan.get("paused")),
    }
    if action not in ALLOWED:
        raise PlanError(
            refusal(
                action, plan, "такого инструмента нет",
                f"звать можно {', '.join(TOOL_NAMES)}",
            )
        )
    # Приостановленную и завершённую задачу не меняет ни один инструмент
    # модели, и условие стоит **до** разбора аргументов. Пауза держится
    # здесь, а не на правиле в промпте: правило объясняет, гарантирует код.
    if action in ("update_plan", "finish_task"):
        if plan["paused"]:
            raise PlanError(
                refusal(
                    action,
                    plan,
                    "задача на паузе",
                    "дождаться, пока человек снимет паузу",
                )
            )
        if plan["finished"]:
            raise PlanError(
                refusal(
                    action,
                    plan,
                    "задача уже завершена",
                    "менять план может только человек, кнопкой «Переоткрыть»",
                )
            )
    try:
        if action == "update_plan":
            return _updated(plan, args)
        if action == "finish_task":
            return _finished(plan, args)
        if action == "approve":
            return _approved(plan)
        if action == "reopen":
            return _reopened(plan)
        if action == "pause":
            return _paused(plan)
        if action == "resume":
            return _resumed(plan)
        return _reset(plan)
    except PlanError as exc:
        # Причину называют помощники, директиву собирает одно место: имя
        # действия и план известны здесь, а вторая копия формулы отказа
        # разошлась бы с первой молча.
        raise PlanError(
            refusal(action, plan, exc.reason, exc.allowed or ALLOWED[action])
        ) from None
