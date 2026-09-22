"""Агент — отдельная сущность: конфиг, память и один метод обмена.

Принимает **текст пользователя**, а не готовую ленту: сам склеивает промпт
и дописывает ответ себе в историю. Наружу отдаёт поток событий — из него
и SSE веб-клиента, и вывод CLI. Конфиг и историю достаёт из базы
в конструкторе, а не отдельным вызовом, который можно забыть позвать.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from typing import AsyncIterator

from . import plan as taskplan
from .llm import SAMPLING_FIELDS, MissingKeyError, stream_completion
from .schema import (
    CONTEXT_FIELDS,
    MEMORY_LABELS,
    PROFILE_FIELDS,
    PROFILE_LABELS,
    WORKING_LABELS,
    AgentSpec,
)
from .store import Store

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")
"""Поля метрик, которые складываются по чату. Остальные — про один вызов:
скорость и время до первого токена суммировать бессмысленно."""


def _usage_number(value) -> int | float | None:
    """Число или `None`: в метриках лежит присланное провайдером, и складывать
    чужой тип нельзя."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


TURN_SUM_FIELDS = USAGE_FIELDS + ("reasoning_tokens", "tokens_out", "elapsed_ms")
"""Метрики, которые по оборотам обмена **складываются**: обмен из трёх
оборотов оплачен весь, и токены последнего соврали бы втрое. `elapsed_ms`
тоже сумма — человек ждал все обороты подряд."""

TURN_FIRST_FIELDS = ("ttft_ms", "first_token_ms")
"""Берутся от **первого** оборота: время до первой буквы у обмена одно,
а у второго оборота оно отсчитывалось бы от его собственного начала."""


def _usage_add(left, right):
    """Сумма, в которой `None` — законное значение: `or 0` превратил бы
    «неизвестно» в ноль, а ноль и молчание рисуются по-разному."""
    a, b = _usage_number(left), _usage_number(right)
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _glue(carried: str, piece: str) -> str:
    """Склейка текста через обороты обмена — пустой абзац между кусками.
    Перетирать нельзя: карточка в ленте одна, и слова до вызова инструмента
    такая же её часть, как слова после. Пустой кусок строки не заводит."""
    if not piece:
        return carried
    return f"{carried}\n\n{piece}" if carried else piece


def merge_turn_metrics(carried: dict | None, fresh: dict | None) -> dict | None:
    """Метрики обмена из метрик его оборотов: накопленное плюс ещё один.
    Основа — **последний** оборот (там же и незнакомое новое поле, чтобы
    не терялось молча), поверх складывается `TURN_SUM_FIELDS`
    и берётся от первого `TURN_FIRST_FIELDS`. `turns` — про обмен."""
    if not isinstance(fresh, dict):
        return carried
    if not isinstance(carried, dict):
        return {**fresh, "turns": 1}
    merged = {**fresh}
    for name in TURN_SUM_FIELDS:
        merged[name] = _usage_add(carried.get(name), fresh.get(name))
    # Копейки от сложения float'ов — довод тот же, что в `usage_summary`.
    if merged["cost_usd"] is not None:
        merged["cost_usd"] = round(merged["cost_usd"], 8)
    for name in TURN_FIRST_FIELDS:
        merged[name] = carried.get(name)
    merged["turns"] = (carried.get("turns") or 0) + 1
    return merged

COMPRESS_SYSTEM = (
    "Ты сворачиваешь начало разговора в сжатый пересказ. Не отвечай на сообщения "
    "и не обращайся к собеседнику: твой ответ целиком — пересказ, и он встанет "
    "в контекст вместо свёрнутых сообщений. Сохрани факты, имена, числа, решения "
    "и договорённости: дальше по ним будут задавать вопросы."
)
"""Свой, а не `spec.system`: чат просили отвечать, а здесь пересказывать."""


def build_compress_prompt(chunk: list["Turn"], previous: str | None = None) -> list[dict]:
    """Промпт вызова на сжатие: прошлая сводка плюс **новые** реплики.
    Инкрементально: пересказ всей истории на каждом сворачивании съел бы
    экономию, ради которой сжатие заведено."""
    parts = []
    if previous:
        parts.append("Пересказ начала разговора, который надо продолжить:\n" + previous)
    lines = [
        f"{'Пользователь' if turn.role == 'user' else 'Ассистент'}: {turn.content}"
        for turn in chunk
    ]
    parts.append("Сообщения, которые надо добавить в пересказ:\n" + "\n".join(lines))
    return [
        {"role": "system", "content": COMPRESS_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def summary_message(content: str, covered: int) -> dict:
    """Роль `user` и явная подпись, **не** `system`: врезка это сведения,
    а системное сообщение распоряжение. Без подписи модель приняла бы
    пересказ за реплику пользователя."""
    return {
        "role": "user",
        "content": (
            f"[пересказ начала разговора, свёрнуто сообщений: {covered}]\n"
            f"{content}\n"
            "[дальше — последние сообщения как есть]"
        ),
    }


def working_lines(items: list[dict]) -> str:
    """Строками «подпись типа: содержимое», как уезжает в промпт. Номеров
    нет: читает её модель, а не правящий список. Подписи — по одной карте
    с интерфейсом (`WORKING_LABELS`)."""
    return "\n".join(
        f"{WORKING_LABELS.get(item['kind'], item['kind'])}: {item['content']}"
        for item in items
    )


def working_message(items: list[dict]) -> dict:
    """Роль `user` и явная подпись — довод тот же, что у сводки. Подпись
    и форма — то, что **видит модель**: менять их заодно со схемой базы
    нельзя. Скобка нейтральная: за врезкой может встать сводка."""
    return {
        "role": "user",
        "content": (
            "[факты о разговоре]\n"
            f"{working_lines(items)}\n"
            "[конец фактов о разговоре]"
        ),
    }


def task_message(plan: dict) -> dict:
    """Врезка `[задача]`: роль `user`, а не `system` — распоряжение уезжает
    правилом этапа в системное сообщение. Содержимое — `plan_lines`, та же
    карта, какой план возвращается модели результатом её вызова."""
    return {
        "role": "user",
        "content": f"[задача]\n{taskplan.plan_lines(plan)}\n[конец задачи]",
    }


def memory_lines(records: list[dict]) -> str:
    """Долговременная память строками «подпись типа: содержимое». Подписи —
    по одной карте с интерфейсом (`MEMORY_LABELS`): второй таблицей модель
    читала бы одно слово, а пользователь видел бы другое."""
    return "\n".join(
        f"{MEMORY_LABELS.get(record['kind'], record['kind'])}: {record['content']}"
        for record in records
    )


def memory_message(records: list[dict]) -> dict:
    """Долговременная память так, как она встаёт в промпт: роль `user` и явная
    подпись — довод тот же, что у сводки. Закрывающая скобка нейтральная:
    память стоит первой, и за ней может встать врезка стратегии."""
    return {
        "role": "user",
        "content": (
            "[долговременная память]\n"
            f"{memory_lines(records)}\n"
            "[конец долговременной памяти]"
        ),
    }


def profile_block(values: dict) -> str:
    """Профиль строками «подпись поля: текст» под заголовком `[как отвечать]`.
    Только **заполненные** поля: пустое уехало бы строкой «формат: » и заняло
    бы место распоряжения. Подписи — по одной карте с интерфейсом."""
    lines = [
        f"{PROFILE_LABELS.get(field, field)}: {values[field]}"
        for field in PROFILE_FIELDS
        if values.get(field)
    ]
    return "[как отвечать]\n" + "\n".join(lines)


def system_message(system: str, profile: dict, rule: str = "") -> dict | None:
    """Три части одного сообщения — промпт чата, профиль, правило этапа —
    или `None`, если нет ни одной. **Роль определяется источником**:
    системным едет распоряжение, врезки памяти едут `user`. Заводится
    и у чата без `spec.system`, и тогда сдвигает номера всех врезок."""
    block = profile_block(profile) if profile else ""
    parts = [part for part in (system, block, rule) if part]
    if not parts:
        return None
    return {"role": "system", "content": "\n\n".join(parts)}


PROMPT_SLOTS = ("memory_at", "working_at", "plan_at", "summary_at")
"""Врезки перед историей — в том порядке, в каком их кладёт `prompt_head`,
и теми именами, какими они уезжают в кадре `start`. Список, а не имена
по месту: врезка добавляется в одном месте, а называется в двух."""





CUT_METRIC = {"summary": "summarized", "window": "dropped"}
"""Каким ключом обмен говорит, что начало не уехало дословно. Слова разные
не для красоты: сводка начало **заменила**, окно его **отбросило**.
У полной истории ключа нет, и в суммы по чату он не идёт."""

MAX_TURNS = 6
"""Сколько оборотов обмену отпущено: цикл замкнут на модель, а платит
человек. Сторожей у предела **два**: `turn_tools` (последний оборот без
`tools` — предел обязан упираться в слова, а не в тишину) и жёсткий выход
по счётчику в `ask`, не смотрящий на `tools`: первый легко потерять."""


_last_id = 0
_id_lock = threading.Lock()


def new_agent_id() -> str:
    """Следующий id по счётчику процесса — для агента без хранилища."""
    global _last_id
    with _id_lock:
        _last_id += 1
        return f"ag_{_last_id:05d}"


def reserve_ids(upto: int) -> None:
    """Сдвигает счётчик за самый большой id из базы: после перезапуска он
    начался бы с нуля и поднял бы под `ag_00001` чужую историю. Настоящий
    арбитр — первичный ключ, а это подсказка, экономящая попытку."""
    global _last_id
    with _id_lock:
        _last_id = max(_last_id, int(upto))


def copy_spec(spec: AgentSpec) -> AgentSpec:
    """Копия конфига, и вглубь тоже: `replace` копирует только верхний
    уровень, и правка `stop` у одного агента задела бы остальных."""
    return replace(
        spec,
        stop=list(spec.stop) if spec.stop else None,
        response_format=copy.deepcopy(spec.response_format),
        extra_body=copy.deepcopy(spec.extra_body or {}),
    )


class AgentBusyError(RuntimeError):
    """У агента уже идёт обмен. Второй запрос — ошибка, а не очередь: иначе
    две вкладки молча перемешали бы историю."""


@dataclass
class Turn:
    """Одна реплика в истории агента."""

    role: str
    content: str

    error: str | None = None
    """Заполнен, если ответ оборвался: реплика в истории есть, но она неполная."""

    reasoning: str = ""
    """Рассуждение модели: в `content` не входит и обратно в модель не уходит."""

    metrics: dict | None = None
    """Метрики этого ответа: по ним рисуются плитки внизу справа."""

    at: float = field(default_factory=time.time)
    """Время реплики: с ним она уезжает в базу и возвращается оттуда."""

    def as_message(self) -> dict:
        """В контекст возвращается ответ, а не путь к нему."""
        return {"role": self.role, "content": self.content}


@dataclass
class Exchange:
    """Снятая с истории пара «вопрос — ответ» — то, что можно вернуть назад."""

    question: str
    turns: list[Turn]
    depth: int
    """Длина истории сразу после снятия: по ней видно, занял ли место кто-то другой."""


class Agent:
    """Один агент: конфиг + история + один метод обмена. Инстанцируется дёшево
    и много: ни сети, ни потоков, ни подпроцессов в конструкторе."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        agent_id: str | None = None,
        context_length: int | None = None,
        store: Store | None = None,
    ) -> None:
        # Свежий id занимает база, а не процесс: сервер и консоль ходят
        # в один файл, и локальный счётчик выдал бы обоим один номер.
        claimed = False
        if agent_id is not None:
            self.id = agent_id
        elif store is not None:
            self.id = store.claim_agent_id()
            claimed = True
        else:
            self.id = new_agent_id()

        try:
            self._setup(spec, agent_id, context_length, store)
        except BaseException:
            # Строку под этот id уже заняли: не достроились — убираем
            # за собой, иначе в списке повис бы чат без объекта.
            if claimed and store is not None:
                with contextlib.suppress(Exception):
                    store.delete_session(self.id)
            raise

    def _setup(
        self,
        spec: AgentSpec,
        agent_id: str | None,
        context_length: int | None,
        store: Store | None,
    ) -> None:
        """Всё, что собирается после того, как id занят. Вынесено ради
        уборки: конструктор ловит отсюда исключение и освобождает строку."""
        # Свой экземпляр конфига каждому: один spec может поднять сотню.
        self.spec = copy_spec(spec)
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Сжатие её **не трогает**: она
        полная всегда, иначе сломалась бы перегенерация — `take_last_exchange`
        ждёт хвост `["user", "assistant"]`, а `restore` сверяет длину."""

        self.summaries: list[dict] = []
        """Сводки по порядку сворачивания: `{upto, content, metrics, at}`.
        Последняя действующая. Список, а не одна: у каждого сворачивания свои
        метрики, и без них счёт стоимости сжатия был бы неполным."""

        self.working: list[dict] = []
        """Рабочая память чата: `{seq, kind, content, at}`, в порядке номеров.
        Просто список: числа рядом были про **вызов**, а вызова нет — отсюда
        ни курсора прочитанного, ни зажима окна, ни автора у записи."""

        self._working_seq = 0
        """Последний выданный номер — на случай агента **без хранилища**.
        С хранилищем номера выдаёт база; правило у обоих одно: только вперёд,
        номер удалённой записи заново не выдаётся."""

        self.plan: dict = taskplan.empty()
        """Состояние задачи: `{steps, approved, finished, paused}` — **план
        и есть состояние**, этап из него вычисляется (`app/plan.py`).
        Поднимается из базы в конструкторе: иначе продолжение разговора
        начиналось бы с «плана ещё нет» посреди работы."""

        self.branch: dict | None = None
        """Происхождение чата: `{parent_id, forked_at}` или `None`. Не
        в `spec`: происхождение не настройка. Имени родителя здесь нет,
        только id — копия разошлась бы с ним на первом переименовании."""

        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._reserved = False

        self.store = store
        """Хранилище сессии; None — агент живёт только в памяти. Обнуляется
        при выгрузке: с этого момента объект не владелец сессии (`detach`)."""

        if store is not None:
            # Здесь, а не отдельным вызовом: забыть позвать невозможно.
            saved = store.load_session(self.id) if agent_id else None
            if saved is not None:
                self.spec = spec_from_config(saved["config"], fallback=self.spec)
                self.created_at = saved["created_at"]
                self.last_used_at = saved["updated_at"]
                if saved.get("context_length") is not None and context_length is None:
                    # Каталог моделей — сетевой запрос, и восстановление
                    # не должно его ждать ради context_fill_pct.
                    self.context_length = saved["context_length"]
                self.history = [
                    Turn(
                        role=m["role"],
                        content=m["content"],
                        error=m["error"],
                        metrics=m["metrics"],
                        at=m["at"],
                    )
                    for m in store.load_messages(self.id)
                ]
                # Сводки лежат отдельно от истории: перезапись истории их
                # не трогает, и свёрнутое остаётся свёрнутым.
                self.summaries = store.load_summaries(self.id)
                self.working = store.list_working(self.id)
                self._working_seq = max(
                    (item["seq"] for item in self.working), default=0
                )
                # Строки нет вовсе — значит «планом не занимались», и это
                # пустой план. Решает это одно место, здесь.
                self.plan = store.load_plan(self.id) or taskplan.empty()
                self.branch = store.load_branch(self.id)
            self.save_config()

    # --- состояние -----------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._lock.locked() or self._reserved

    def reserve(self) -> None:
        """Занимает агента синхронно, до первого await: иначе второй запрос
        пролезет между проверкой и первым `await` и получит 200 с ошибкой
        внутри потока вместо честного 409. Освобождает `release`, в finally."""
        if self.busy:
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")
        self._reserved = True

    def release(self) -> None:
        self._reserved = False

    def cancel(self) -> None:
        """Просит прекратить генерацию. Задачу извне не отменяет: агент сам
        выходит на ближайшем чанке и закрывает поток штатным `done`."""
        self._cancel.set()

    # --- история -------------------------------------------------------------

    def summary_cover(self, spec: AgentSpec | None = None) -> int:
        """Сколько первых реплик заменено сводкой; 0 — сводки нет или сжатие
        выключено. Больше длины истории не бывает: перегенерация снимает пару
        с конца, и без зажима свёрнутого вышло бы больше, чем было."""
        spec = spec if spec is not None else self.spec
        if spec.keep_last is None or not self.summaries:
            return 0
        upto = self.summaries[-1].get("upto")
        if not isinstance(upto, int) or upto <= 0:
            return 0
        return min(upto, len(self.history))

    def working_items(self, spec: AgentSpec | None = None) -> list[dict]:
        """Что из рабочей памяти уедет врезкой — записи или пустой список.
        Пусто значит, что врезки нет вовсе: довод и форма те же, что
        у `memory_items`."""
        return list(self.working)

    def context_cut(self, spec: AgentSpec | None = None) -> tuple[int, dict | None]:
        """Что стратегия делает с началом истории: сколько первых реплик
        не уехало дословно и что встало вместо них (`None` — ничего).

        **Единственный разбор по `spec.strategy`**, и обе половины берутся
        разом: разъедься число с врезкой — промпт заявил бы сводку на десять
        реплик, а срезал бы восемь. Врезок памяти здесь нет: обе едут при
        любой стратегии. Незнакомое значение читается как `full`.
        """
        spec = spec if spec is not None else self.spec
        if spec.strategy == "window":
            # Пустое поле — отбрасывать нечем: None это «не делать»,
            # а не «делать с нулём».
            if spec.keep_last is None:
                return 0, None
            # Окно режет ровно столько, сколько просили: зажима «не дальше
            # прочитанного» нет — записи вписал человек, и от длины разговора
            # они не зависят.
            return max(0, len(self.history) - spec.keep_last), None
        if spec.strategy == "summary":
            covered = self.summary_cover(spec)
            if not covered:
                return 0, None
            return covered, summary_message(self.summaries[-1]["content"], covered)
        return 0, None

    def memory_items(self, spec: AgentSpec | None = None, memory=None) -> list[dict]:
        """Записи или пустой список. Условие одно на два случая (пусто или
        хранилища нет): **пустая память неотличима от отсутствующей**, иначе
        всякая последовательность ролей сдвинулась бы. `memory` — уже
        прочитанный список: обмен читает его один раз."""
        spec = spec if spec is not None else self.spec
        if memory is not None:
            return list(memory)
        if self.store is None:
            return []
        return self.store.list_memory()

    def profile_items(self, profile=None) -> dict:
        """Заполненные поля или пустой словарь. Условие одно на два случая,
        как у `memory_items`, и здесь строже: профиль заводит собой
        **системное** сообщение и сдвинул бы номера всех врезок разом."""
        if profile is not None:
            return dict(profile)
        if self.store is None:
            return {}
        return self.store.load_profile()

    def prompt_head(
        self, spec: AgentSpec | None = None, memory=None, profile=None
    ) -> tuple[list[dict], dict[str, int | None], int]:
        """Начало промпта разом: сообщения **до** истории, номер каждой врезки
        и срез, с которого история уезжает дальше. Номер не **считается**,
        а берётся из длины собранного начала: сумма врезок, переписанная
        вторым местом, разошлась бы молча. Порядок от общего к частному;
        ноль законен, «врезки нет» это только `None`."""
        spec = spec if spec is not None else self.spec

        messages: list[dict] = []
        slots: dict[str, int | None] = {name: None for name in PROMPT_SLOTS}
        # Своего номера у системного сообщения нет: оно всегда первое,
        # и клиент подписывает его по позиции. А на номера остальных врезок
        # влияет прямо — потому и собрано здесь же, где они берутся.
        head = system_message(
            spec.system, self.profile_items(profile), self.plan_rule(spec)
        )
        if head is not None:
            messages.append(head)

        records = self.memory_items(spec, memory)
        if records:
            slots["memory_at"] = len(messages)
            messages.append(memory_message(records))

        items = self.working_items(spec)
        if items:
            slots["working_at"] = len(messages)
            messages.append(working_message(items))

        # Блок задачи едет **всегда, когда процесс включён**, — даже
        # с пустым списком: без него модель не знает, что план ведётся.
        if self.plan_on(spec):
            slots["plan_at"] = len(messages)
            messages.append(task_message(self.plan))

        cut, insert = self.context_cut(spec)
        if insert is not None:
            slots["summary_at"] = len(messages)
            messages.append(insert)

        return messages, slots, cut

    def build_prompt(
        self, user_text: str, *, spec: AgentSpec | None = None, memory=None, profile=None
    ) -> list[dict]:
        """Системный промпт + долговременная память + рабочая память +
        состояние задачи + начало истории по стратегии + хвост + вопрос.
        Единственное место, где решается состав промпта. `spec`, `memory`
        и `profile` передаёт обмен: он читает их один раз на обмен."""
        messages, _, cut = self.prompt_head(spec, memory, profile)
        messages.extend(turn.as_message() for turn in self.history[cut:])
        messages.append({"role": "user", "content": user_text})
        return messages

    def prompt_slots(
        self, spec: AgentSpec | None = None, memory=None, profile=None
    ) -> dict[str, int | None]:
        """Номера всех врезок промпта разом; `None` — врезки нет вовсе.
        Одним ответом, а не по одному: четыре ответа считались бы по четырём
        копиям одной формулы и разошлись бы молча."""
        return self.prompt_head(spec, memory, profile)[1]

    def compress_plan(self, spec: AgentSpec) -> tuple[int, int] | None:
        """Что предстоит свернуть: `(свёрнуто, новая граница)` или `None`.
        Отдельно от сворачивания, потому что спросить надо **до** него:
        по этому же ответу шлётся `compressing`."""
        # Стратегия спрашивается здесь же, где порог: «сворачивать ли»
        # решается одним кодом, второе условие разошлось бы молча.
        if spec.strategy != "summary":
            return None
        keep, every = spec.keep_last, spec.compress_every
        if keep is None or every is None or keep < 0 or every <= 0:
            return None
        covered = self.summary_cover(spec)
        # Граница не рвёт пару: свёрнутый вопрос без своего ответа сделал бы
        # хвост бессмысленным. Округляем вниз до чётного.
        border = ((len(self.history) - keep) // 2) * 2
        if border - covered < every:
            return None
        return covered, border

    async def compress(self, spec: AgentSpec, context_length: int | None = None) -> None:
        """Сворачивает начало истории, если несвёрнутого больше порога.
        Зовётся из `ask` **до** сборки промпта: запустивший сворачивание
        обмен уже сам едет сжатым. Историю не трогает, и не вышло сжать —
        обмен просто уедет полным."""
        plan = self.compress_plan(spec)
        if plan is None:
            return
        covered, border = plan
        chunk = self.history[covered:border]
        if not chunk:
            return

        # Формат ответа и стоп-строки сняты: первый вернул бы объект вместо
        # пересказа, вторая оборвала бы его на середине. Модель та же:
        # вторая развалила бы счёт на две цены.
        folding = replace(spec, response_format=None, stop=None)
        prompt = build_compress_prompt(chunk, self.summaries[-1]["content"] if covered else None)

        content = ""
        metrics: dict | None = None
        try:
            stream = stream_completion(folding, prompt_override=prompt, context_length=context_length)
            async with contextlib.aclosing(stream):
                async for event in stream:
                    if event["type"] == "done":
                        content = event["text"]
                        metrics = event["metrics"]
                    elif event["type"] == "error":
                        return
        except Exception:  # noqa: BLE001 — падает сжатие, обмен живёт
            # Сжатие — не сам обмен: не вышло свернуть, значит история уедет
            # целиком. Про отсутствие ключа расскажет сам обмен, следом.
            return
        if not content.strip():
            return

        self.summaries.append(
            {"upto": border, "content": content, "metrics": metrics, "at": time.time()}
        )
        if self.store is not None:
            self.store.save_summaries(self.id, self.summaries)

    def service_plan(self, spec: AgentSpec) -> list[str]:
        """Какие служебные вызовы предстоят обмену: `summary` — сжатие.
        Одна точка решения, и та же, что зовёт вызов: по этому же списку
        уходят события `compressing`."""
        return ["summary"] if self.compress_plan(spec) is not None else []

    # --- рабочая память: записи человека --------------------------------------
    # Пишет сюда **только человек**, и через эти три метода, а не в хранилище
    # напрямую: список в памяти обязан совпадать с базой. Методов
    # долговременного слоя здесь нет вовсе — пути записать туда у агента нет.

    def _working_find(self, seq) -> dict | None:
        """Запись по номеру — или `None`, если такой в этом чате нет."""
        if not isinstance(seq, int):
            return None
        return next((item for item in self.working if item["seq"] == seq), None)

    def _working_add(self, kind: str, content: str, at=None) -> dict:
        """Заводит запись и отдаёт её вместе с номером. Номер выдаёт база
        (AUTOINCREMENT), и номер удалённой не достаётся следующей; без
        хранилища счёт ведёт агент — тем же правилом и только вперёд."""
        if self.store is not None:
            record = self.store.add_working(self.id, kind, content, at)
        else:
            self._working_seq += 1
            record = {
                "seq": self._working_seq,
                "kind": kind,
                "content": content,
                "at": time.time() if at is None else at,
            }
        self._working_seq = max(self._working_seq, record["seq"])
        self.working.append(record)
        return record

    def _working_write(self, record: dict, kind: str, content: str) -> dict:
        """Переписывает запись на месте. Номер не трогается: он и есть её
        идентичность, и сдвиг номеров соседей удалил бы у второй вкладки
        не ту запись."""
        stamp = time.time()
        if self.store is not None:
            saved = self.store.update_working(
                self.id, record["seq"], kind=kind, content=content, at=stamp
            )
            if saved is not None:
                record.update(saved)
                return record
        record.update(kind=kind, content=content, at=stamp)
        return record

    def _working_drop(self, record: dict) -> None:
        """Убирает запись. Номер после этого не достаётся никому."""
        if self.store is not None:
            self.store.delete_working(self.id, record["seq"])
        self.working.remove(record)

    def add_working_record(self, kind: str, content: str) -> dict:
        """Запись, сделанная человеком, — других здесь и не бывает."""
        return self._working_add(kind, content)

    def edit_working_record(self, seq: int, *, kind=None, content=None) -> dict | None:
        """Правка руками: меняет названное, остальное оставляет. `None` —
        записи с таким номером в этом чате нет."""
        record = self._working_find(seq)
        if record is None:
            return None
        return self._working_write(
            record,
            kind or record["kind"],
            record["content"] if content is None else content,
        )

    def drop_working_record(self, seq: int) -> bool:
        """Удаление руками. False — записи с таким номером в этом чате
        не было."""
        record = self._working_find(seq)
        if record is None:
            return False
        self._working_drop(record)
        return True

    # --- состояние задачи: план и есть состояние -------------------------------
    # Этап вычисляется из списка шагов (`app/plan.py`), а переходы решает
    # `plan.apply` — один и на инструмент модели, и на кнопку человека.

    def plan_on(self, spec: AgentSpec | None = None) -> bool:
        """Ведёт ли этот чат план — **единственное** место, где читается
        `spec.workflow`. Незнакомое значение читается как `off`: зеркало
        того, как незнакомая `strategy` читается как `full`."""
        spec = spec if spec is not None else self.spec
        return spec.workflow == "plan"

    def plan_view(self) -> dict:
        """План плюс вычисленные `stage` и `current` — **одна форма** на всё:
        `as_dict`, ручки кнопок, кадр события. Копия глубокая: правка
        у вызывающего не вправе доехать до состояния мимо `apply`."""
        stage, current = taskplan.stage_of(self.plan)
        return {**copy.deepcopy(self.plan), "stage": stage, "current": current}

    def plan_rule(self, spec: AgentSpec | None = None) -> str:
        """Правило этапа для системного сообщения — или пустая строка при
        выключенном процессе: тогда в промпте не меняется ни одно слово."""
        return taskplan.stage_rule(self.plan) if self.plan_on(spec) else ""

    def plan_stage(self, spec: AgentSpec | None = None) -> str | None:
        """Этап задачи — или `None` при выключенном процессе («задачи нет
        вовсе»). Одно место на все три вопроса обмена: запомнить на входе,
        сравнить перед оборотом, назвать в `done`."""
        return taskplan.stage_of(self.plan)[0] if self.plan_on(spec) else None

    def restage(
        self,
        messages: list[dict],
        spec: AgentSpec,
        profile: dict,
        plan_at: int | None,
    ) -> None:
        """Переписывает **на месте** правило этапа и блок задачи перед каждым
        оборотом: иначе распоряжение оказывается старше приехавших сведений.

        **На месте, а не дописыванием**: номера врезок уехали кадром `start`
        и обязаны сходиться с лентой любого оборота, а у чата завелось бы
        второе системное сообщение. Номера обоих мест **берутся**, а не
        ищутся разбором. Словарь заменяется целиком — тот же лежит в кадре
        `start`, и кадр обязан остаться прежним.
        """
        if not self.plan_on(spec):
            return
        rule = self.plan_rule(spec)
        messages[0] = system_message(spec.system, self.profile_items(profile), rule)
        if plan_at is not None:
            messages[plan_at] = task_message(self.plan)

    def tool_specs(self, spec: AgentSpec | None = None) -> list[dict]:
        """Два инструмента при включённом процессе, пустой список при
        выключенном. Пустой и есть «не объявлять ничего»: ключа `tools`
        в теле тогда не будет вовсе — `tools: []` значит другое."""
        return list(taskplan.TOOLS) if self.plan_on(spec) else []

    def turn_tools(
        self, spec: AgentSpec, turn: int, stage: str | None = None
    ) -> list[dict] | None:
        """Что объявить модели на этом обороте: инструменты чата — или ничего
        на последнем разрешённом (`MAX_TURNS`) и ничего после смены этапа.

        **Одно названное место** на решение «объявлять ли»: у предела два
        сторожа, и проверить второй можно лишь в мире, где первый снят.
        Первый и есть эта строка: предел обязан упираться в слова, а не
        в тишину. Третье условие — **один этап, один обмен**: обрывать цикл
        на месте нельзя по тому же доводу, поэтому инструменты гаснут, модель
        договаривает словами, и цикл кончается сам.
        """
        if turn >= MAX_TURNS:
            return None
        if stage is not None and stage != self.plan_stage(spec):
            return None
        return self.tool_specs(spec) or None

    def turn_choice(
        self, spec: AgentSpec, turn: int, stage: str | None = None
    ) -> dict | None:
        """Принуждать ли модель к вызову на этом обороте и к какому; `None` —
        не принуждать.

        **Инструкция в промпте просьба, гарантию даёт код**: живой прогон
        `openai/gpt-4o-mini` показывал на планировании решение вместо плана.
        Именно `update_plan`, а не `"required"`: это единственное действие,
        которое `apply` здесь не отказывает. Первой строкой спрашивает
        `turn_tools` — поле без `tools` провайдер отвергнет, и отсюда сразу
        три следствия: нет принуждения на последнем обороте, после смены
        этапа и при выключенном процессе.
        """
        if not self.turn_tools(spec, turn, stage):
            return None
        return taskplan.FORCE_UPDATE_PLAN if stage == "planning" else None

    def _save_plan(self, plan: dict | None) -> None:
        """Пишет состояние задачи в хранилище; без хранилища — ничего,
        как `persist`. План приходит **параметром**: пишется он раньше,
        чем меняется память (`_move_plan`)."""
        if self.store is not None:
            self.store.save_plan(self.id, plan)

    def _move_plan(self, action: str, args=None) -> str:
        """Один переход через `plan.apply`, запись — и только потом память:
        упади запись после присваивания, модель получила бы отказ «состояние
        не изменилось» с уже изменённым планом под ним. `PlanError` уходит
        наружу — чем он станет, решает вызывающий."""
        fresh, message = taskplan.apply(self.plan, action, args)
        self._save_plan(fresh)
        self.plan = fresh
        return message

    def run_tool(self, name: str, arguments: str) -> tuple[bool, str]:
        """Исполняет вызов инструмента: `(получилось, текст для модели)`.

        Аргументы разбираются **под `try`**: сломанный аргумент не вправе
        уронить разговор, а текст отказа — блокирующая директива, а не код
        ошибки. И здесь же **ворота**: отсекается всё, чего модели
        не объявляли (`plan.TOOL_NAMES`), иначе она утвердила бы себе план
        сама. Второй сторож рядом: чату с выключенным процессом писать план
        нельзя вовсе.
        """
        if not self.plan_on():
            return False, taskplan.refusal(
                name,
                self.plan,
                "рабочий процесс в этом чате выключен",
                "отвечать словами — плана в этом чате нет и вести его нечем",
            )
        if name not in taskplan.TOOL_NAMES:
            return False, taskplan.refusal(
                name,
                self.plan,
                "это не инструмент модели, а кнопка человека",
                f"звать можно {', '.join(taskplan.TOOL_NAMES)}",
            )
        try:
            args = json.loads(arguments) if (arguments or "").strip() else {}
        except (TypeError, ValueError):
            return False, taskplan.broken_args(name, self.plan)
        if not isinstance(args, dict):
            return False, taskplan.broken_args(name, self.plan)
        try:
            return True, self._move_plan(name, args)
        except taskplan.PlanError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — падает вызов, разговор живёт
            return False, taskplan.broken_args(
                name, self.plan, f"аргументы не подошли: {type(exc).__name__}"
            )

    def approve_plan(self) -> dict:
        """Кнопка «Утвердить план». Через тот же `plan.apply`, что и вызовы
        модели: второй проверки переходов в продукте нет."""
        self._move_plan("approve")
        return self.plan_view()

    def reopen_plan(self) -> dict:
        """Кнопка «Переоткрыть задачу». Обмен при этом **не отправляется** —
        что именно не так, человек пишет сам."""
        self._move_plan("reopen")
        return self.plan_view()

    def pause_plan(self) -> dict:
        """Кнопка «Пауза». Ставит её **только человек**, и держится она
        не на правиле в промпте, а на отказе обоих инструментов
        (`plan.apply`)."""
        self._move_plan("pause")
        return self.plan_view()

    def resume_plan(self) -> dict:
        """Кнопка «Продолжить». Снимает один флажок и больше ничего
        не трогает: этап вернётся тот же, что был до паузы, потому что он
        вычисляется из списка, а список не менялся."""
        self._move_plan("resume")
        return self.plan_view()

    def reset_plan(self) -> dict:
        """Кнопка «Сбросить задачу»: пустой список и все три флажка сняты.
        На ней держится и «начать другую задачу», и «вернуться в обычный
        чат»."""
        self._move_plan("reset")
        return self.plan_view()

    def remember(
        self,
        role: str,
        content: str,
        error: str | None = None,
        *,
        reasoning: str = "",
        metrics: dict | None = None,
        persist: bool = True,
    ) -> None:
        self.history.append(
            Turn(role=role, content=content, error=error, reasoning=reasoning, metrics=metrics)
        )
        if persist:
            self.persist()

    def carry_off(self, at: int) -> dict:
        """Что унесёт ветка, отделённая по `at`-е сообщение включительно.

        Копия глубокая, а родителя метод **только читает**: `save_history`
        начинается с `DELETE`. **Сводка едет не всегда** — она заменяет собой
        начало истории, а заменять можно только то начало, которое в ветке
        есть (правило `upto <= at`). **А рабочая память и план едут целиком**:
        их вписал человек, реплик они не заменяют, задача та же.
        """
        return {
            "history": [
                replace(turn, metrics=copy.deepcopy(turn.metrics))
                for turn in self.history[:at]
            ],
            "summaries": [
                copy.deepcopy(item)
                for item in self.summaries
                if isinstance(item.get("upto"), int) and item["upto"] <= at
            ],
            "working": [copy.deepcopy(item) for item in self.working],
            # План едет целиком, с флажками: задача у ветки та же,
            # и утверждённый план обязан остаться утверждённым.
            "plan": copy.deepcopy(self.plan),
        }

    def take_branch(self, carried: dict, *, parent_id: str, forked_at: int) -> None:
        """Принимает унесённое ветвлением и записывает происхождение. Всё
        одной транзакцией: умри процесс посередине — в базе остался бы чат
        с историей, но без родства, и пометки ветки у него не было бы."""
        self.history = carried["history"]
        self.summaries = carried["summaries"]
        # Копия плана глубокая и в памяти тоже: общий список шагов —
        # и отметка у родителя молча переставила бы шаг у ветки.
        self.plan = carried["plan"]
        self.branch = {"parent_id": parent_id, "forked_at": forked_at}

        # Записи заводятся заново, а не переносятся с номерами: номер
        # принадлежит одному чату. Содержимое, тип и время у копий прежние.
        self.working = []
        if self.store is None:
            for item in carried["working"]:
                self._working_add(item["kind"], item["content"], item["at"])
            return
        with self.store.tx():
            self.store.save_branch(self.id, parent_id=parent_id, forked_at=forked_at)
            self.store.save_summaries(self.id, self.summaries)
            self.store.save_plan(self.id, self.plan)
            for item in carried["working"]:
                self._working_add(item["kind"], item["content"], item["at"])
            self.persist()

    def detach(self) -> None:
        """Снимает право писать в сессию — зовётся при выгрузке. Владеет ею
        тот объект, что лежит в реестре: иначе придержанная ссылка пережила
        бы вытеснение и затёрла реплики второго объекта."""
        self.store = None

    def persist(self) -> None:
        """Пишет историю в хранилище. Без хранилища — тихо ничего не делает."""
        if self.store is not None:
            self.store.save_history(self.id, self.history)

    def save_config(self) -> None:
        """Пишет конфиг сессии одним JSON-полем: `asdict(spec)`, поэтому
        новое поле сохраняется само."""
        if self.store is None:
            return
        self.store.save_session(
            self.id,
            label=self.spec.label,
            config=asdict(self.spec),
            created_at=self.created_at,
            context_length=self.context_length,
        )

    def forget(self) -> None:
        """Забывает разговор целиком — историю, сводки, рабочую память
        и план. Оставленная сводка накрыла бы начало следующего разговора,
        а врезка рабочей памяти встаёт и вовсе без зажима по длине истории.
        Долговременная память и происхождение **не трогаются**: чат памяти
        не владелец, а читатель, и ветка осталась веткой того же родителя."""
        self.history.clear()
        self.summaries = []
        self.working = []
        # План — содержимое разговора: оставленные шаги врали бы про этап
        # с первого же сообщения.
        self.plan = taskplan.empty()
        if self.store is not None:
            self.store.save_summaries(self.id, [])
            self.store.clear_working(self.id)
            self.store.save_plan(self.id, None)
        self.persist()

    def usage_summary(self) -> dict | None:
        """Итог по чату: вход, выход, всего, стоимость и число ответов
        с числами. На сервере, иначе плитки и лента — два источника правды.
        Проходов два, `messages` и `summaries`: сжатие тоже оплачено. `None`
        **пропускается**, а не считается нулём."""
        totals: dict = {name: None for name in USAGE_FIELDS}
        answers = 0

        def add(metrics) -> bool:
            """Прибавляет один набор метрик. False — чисел в нём не нашлось."""
            if not isinstance(metrics, dict):
                return False
            counted = False
            for name in USAGE_FIELDS:
                value = _usage_number(metrics.get(name))
                if value is None:
                    continue
                totals[name] = value if totals[name] is None else totals[name] + value
                counted = True
            return counted

        for turn in self.history:
            if turn.role != "assistant":
                continue
            # Метрики без чисел обменом не считаются: делитель рос бы зря.
            if add(turn.metrics):
                answers += 1

        # Второй проход — по сводкам: вызов на сжатие оплачен, и экономия,
        # не вычитающая его, — враньё. Счётчик сообщений он при этом
        # не растит: сводки живут не в истории.
        for item in self.summaries:
            if add(item.get("metrics")):
                answers += 1

        if not answers:
            return None
        if totals["cost_usd"] is not None:
            # Копейки от сложения float'ов: цена показывается до шестого знака.
            totals["cost_usd"] = round(totals["cost_usd"], 8)
        return totals

    def transcript(self) -> list[dict]:
        """Ровно реплики диалога: системный промпт это конфиг, а не реплика."""
        return [asdict(turn) for turn in self.history]

    def as_dict(self, *, with_transcript: bool = False) -> dict:
        data = spec_as_dict(
            self.spec,
            agent_id=self.id,
            history_len=len(self.history),
            usage_total=self.usage_summary(),
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            busy=self.busy,
            branch=self.branch,
            # У чата из базы ключ тоже есть, но `None` (`spec_as_dict`).
            plan=self.plan_view(),
        )
        if with_transcript:
            data["transcript"] = self.transcript()
        return data

    # --- обмен ---------------------------------------------------------------

    async def ask(self, user_text: str) -> AsyncIterator[dict]:
        """Один обмен: вопрос → обороты «модель ↔ инструменты» → запись
        в историю.

        Оборотов бывает несколько, и это **условие видимости**: модель,
        которой объявили инструменты, возвращает вызов и ни слова текста.
        Кончаются они словами всегда — последний идёт без `tools`. Обмен
        при этом **один этап**: сменился он, и следующий оборот идёт без
        инструментов, а `done` называет `stage_from`/`stage_to`.

        В историю пишется **одна** пара «вопрос — ответ», склеенная из всех
        оборотов. До конца обмена история не трогается, и откат выходит
        по построению: ответа не случилось — не пишется ничего, вопрос
        возвращается в `done` полем `question`; частичный — пишутся обе,
        у ответа `error`.

        События: `compressing`, `start`, `reasoning`, `delta`, `metrics`,
        `tool`, `error`, `done`.
        """
        if self._lock.locked():
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")

        async with self._lock:
            self._cancel = asyncio.Event()
            cancel = self._cancel
            self.last_used_at = time.time()

            # Слепок конфига на весь обмен: промпт и тело запроса собираются
            # в двух точках, и правка панели между ними дала бы смешанный
            # запрос — новую модель со старым системным промптом.
            spec = copy_spec(self.spec)
            context_length = self.context_length

            # Служебные вызовы — **до** сборки промпта: этот же обмен уедет
            # уже сжатым. Перед каждым — событие о том, что он будет: иначе
            # клиент узнал бы о паузе, когда она уже кончилась.
            for call in self.service_plan(spec):
                yield {"type": "compressing", "strategy": call}
                await self.compress(spec, context_length)
            cut, _ = self.context_cut(spec)

            # Память читается **один раз на обмен** и уезжает и в промпт,
            # и в слоты: иначе запись от соседней вкладки попала бы в промпт,
            # но не в номера врезок. После служебного вызова, а не до.
            memory = self.memory_items(spec)
            # Профиль — тем же порядком и по тому же доводу, и здесь оно
            # важнее: он заводит собой системное сообщение.
            profile = self.profile_items()

            prompt = self.build_prompt(
                user_text, spec=spec, memory=memory, profile=profile
            )

            # Места всех четырёх врезок; `None` — врезки нет. Без них
            # клиент различал бы врезки разбором текста, то есть повторял бы
            # `build_prompt` и расходился бы с ним молча.
            slots = self.prompt_slots(spec, memory, profile)
            yield {
                "type": "start",
                "resolved_messages": prompt,
                "question": user_text,
                **slots,
                "strategy": spec.strategy,
            }

            text = ""
            reasoning = ""
            final_metrics: dict | None = None
            failure: str | None = None
            cancelled = False

            # Лента растёт по ходу обмена, но промпт **не пересобирается**:
            # пересборка развела бы запрос с номерами слотов из кадра
            # `start`. Устаревающее переписывается на месте (`restage`).
            messages = list(prompt)
            turn = 0
            # Этап, с которого обмен начался: сравнивать надо с ним, а не
            # с прошлым оборотом — работа и проверка внутри одной карточки
            # это и есть беда, которую чиним.
            entry_stage = self.plan_stage(spec)

            while True:
                turn += 1
                # Свежее правило и свежий список — перед каждым запросом:
                # распоряжение не вправе быть старше сведений.
                self.restage(messages, spec, profile, slots["plan_at"])
                # Первый сторож предела: последний оборот идёт без
                # инструментов. Второй, жёсткий, стоит ниже.
                tools = self.turn_tools(spec, turn, entry_stage)
                # Принуждение — тем же местом: поле без `tools` отвергнут.
                choice = self.turn_choice(spec, turn, entry_stage)
                turn_text = ""
                turn_reasoning = ""
                turn_metrics: dict | None = None
                calls: list[dict] = []

                try:
                    stream = stream_completion(
                        spec,
                        prompt_override=list(messages),
                        context_length=context_length,
                        tools=tools,
                        tool_choice=choice,
                    )
                    async with contextlib.aclosing(stream):
                        async for chunk in stream:
                            kind = chunk["type"]
                            if kind == "delta":
                                turn_text += chunk["text"]
                                yield chunk
                            elif kind == "reasoning":
                                turn_reasoning += chunk["text"]
                                yield chunk
                            elif kind == "metrics":
                                yield chunk
                            elif kind == "tool_calls":
                                # Наружу не пересылается: клиент узнает
                                # о вызове кадром `tool`, когда тот исполнен.
                                # Метрики назовёт `done` — он свежее.
                                calls = chunk["calls"]
                            elif kind == "error":
                                failure = chunk["message"]
                                turn_metrics = chunk["metrics"]
                                yield chunk
                            elif kind == "done":
                                turn_text = chunk["text"]
                                turn_reasoning = chunk.get("reasoning") or turn_reasoning
                                turn_metrics = chunk["metrics"]
                            if cancel.is_set():
                                cancelled = True
                                break
                except MissingKeyError as exc:
                    failure = str(exc)
                    yield {"type": "error", "message": failure, "metrics": None}
                except asyncio.CancelledError:
                    # Клиент ушёл: частичный ответ всё равно записываем —
                    # он оплачен. Весь обмен, а не последний оборот.
                    self._commit(
                        user_text,
                        _glue(text, turn_text),
                        "вызов прерван",
                        _glue(reasoning, turn_reasoning),
                        merge_turn_metrics(final_metrics, turn_metrics),
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 — падает обмен, процесс живёт
                    failure = f"{type(exc).__name__}: {exc}"
                    yield {"type": "error", "message": failure, "metrics": None}

                # Склеиваются, а не перетираются: карточка в ленте одна.
                text = _glue(text, turn_text)
                reasoning = _glue(reasoning, turn_reasoning)
                final_metrics = merge_turn_metrics(final_metrics, turn_metrics)

                # **Жёсткий выход по счётчику** и первым из всех: первый
                # сторож легко потерять правкой, а повисший обмен оплачен
                # весь и держит слот `call_slots`.
                if turn >= MAX_TURNS:
                    break
                if failure or cancelled:
                    break
                if not calls or tools is None:
                    # Вызовов нет — модель ответила словами. Инструментов
                    # не объявляли — вызов, которого не просили, не исполняем.
                    break

                # Ход ассистента едет обратно целиком. Ключ `type` ставим
                # мы — транспорт его не накапливает; `arguments` уезжают
                # **строкой**, как приехали: провайдер ждёт своё.
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn_text or None,
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {
                                    "name": call["name"],
                                    "arguments": call["arguments"],
                                },
                            }
                            for call in calls
                        ],
                    }
                )
                # По ответу `tool` на каждый вызов, а не один на все:
                # провайдер отвергнет запрос с неотвеченным `tool_call_id`.
                for call in calls:
                    # Отмена — **перед каждым** вызовом: неисполненные
                    # не исполняются вовсе (полуприменённый план хуже
                    # неприменённого), а исполненное остаётся.
                    if cancel.is_set():
                        cancelled = True
                        break
                    ok, message = self.run_tool(call["name"], call["arguments"])
                    yield {
                        "type": "tool",
                        "name": call["name"],
                        "ok": ok,
                        "message": message,
                        # План к кадру прикладывается **сразу**, а не после
                        # цикла: человек видит шаги тогда же, когда их увидела
                        # модель, и видит их столько раз, сколько было правок.
                        "plan": self.plan_view(),
                    }
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": message}
                    )
                if cancelled:
                    break

            if cancelled and failure is None:
                failure = "генерация отменена"

            # Сколько реплик не уехало дословно — рядом с числами обмена,
            # там же, где уменьшенные им входные токены. Ключ у каждой
            # стратегии свой (`CUT_METRIC`), в суммы по чату не идёт.
            if cut and isinstance(final_metrics, dict):
                final_metrics = {**final_metrics, CUT_METRIC[spec.strategy]: cut}

            committed = self._commit(user_text, text, failure, reasoning, final_metrics)
            if not committed and failure is None:
                # Модель отвечала вызовами и ни разу не сказала ни слова:
                # молчаливый `done` человек прочитал бы как сбой сети.
                failure = (
                    "модель не прислала ни слова текста — только вызовы "
                    f"инструментов (оборотов: {turn}). Записывать в историю "
                    "нечего: спросите ещё раз или попросите ответить словами"
                )

            done: dict = {
                "type": "done",
                "text": text,
                "reasoning": reasoning,
                "metrics": final_metrics,
                "cancelled": cancelled,
                "error": failure,
                "committed": committed,
                # **Данными**, а не догадкой клиента. Полей два: текст
                # продолжения привязан к **переходу** — проверка и работа
                # обе ведут в `execution`, но просить надо разного.
                "stage_from": entry_stage,
                "stage_to": self.plan_stage(spec),
            }
            if not committed:
                # Обмена не было: вопрос вернётся в поле ввода клиента.
                done["question"] = user_text
            yield done

    def _commit(
        self,
        user_text: str,
        answer: str,
        failure: str | None,
        reasoning: str = "",
        metrics: dict | None = None,
    ) -> bool:
        """Пишет обмен в историю. Возвращает False, если писать было нечего."""
        if not answer.strip():
            return False

        # Парой, одной транзакцией: иначе умри процесс между ними —
        # в базе остался бы вопрос без ответа.
        with self.store.tx() if self.store is not None else contextlib.nullcontext():
            self.remember("user", user_text, persist=False)
            # Оборванный ответ — часть диалога, но помечен `error`.
            self.remember(
                "assistant", answer, error=failure, reasoning=reasoning, metrics=metrics
            )
        return True

    def take_last_exchange(self) -> Exchange | None:
        """Снимает последнюю пару: перегенерация обязана **заменить** ответ,
        а не дописать второй. В базу снятое не пишется — пока не удалось,
        в файле лежит исходная пара, а удачная перепишет историю целиком."""
        if [turn.role for turn in self.history[-2:]] != ["user", "assistant"]:
            return None
        taken = self.history[-2:]
        del self.history[-2:]
        return Exchange(question=taken[0].content, turns=taken, depth=len(self.history))

    def restore(self, exchange: Exchange) -> bool:
        """Кладёт снятую пару обратно, если её место никто не занял: иначе
        в ленте было бы два ответа на один вопрос."""
        if len(self.history) != exchange.depth:
            return False
        self.history.extend(exchange.turns)
        # Если новый обмен успел записаться и откатиться — файл обязан
        # сойтись с памятью.
        self.persist()
        return True


def spec_as_dict(
    spec: AgentSpec,
    *,
    agent_id: str,
    history_len: int,
    created_at: float,
    last_used_at: float,
    busy: bool = False,
    usage_total: dict | None = None,
    branch: dict | None = None,
    plan: dict | None = None,
) -> dict:
    """Конфиг чата так, как его ждут список слева и панель справа. Функция,
    а не метод: тем же форматом описывается чат, которого нет в памяти —
    клиент не должен различать «поднято» и «лежит в базе»."""
    data = {
        "id": agent_id,
        "label": spec.label,
        "model": spec.model,
        "stop": spec.stop,
        "response_format": spec.response_format,
        "extra_body": spec.extra_body,
        "system": spec.system,
        # Она же плитка «Сообщений»: реплика к реплике. Сжатие её не растит —
        # сводка живёт вне истории. У выгруженного чата то же считает SQL.
        "history_len": history_len,
        # У выгруженного чата это `None`, то есть прочерк, а не ноль.
        "usage_total": usage_total,
        "busy": busy,
        # `{parent_id, forked_at}` у ветки, `None` у чата самого по себе.
        # Имени родителя нет, только id: имя по нему находит клиент.
        "branch": branch,
        # `plan_view()` у живого чата, `None` у лежащего в базе. **Ключ есть
        # всегда**: список строится двумя ветвями кода, и иначе после
        # перезапуска он приехал бы с `workflow: "plan"` и без плана.
        "plan": plan,
        "created_at": created_at,
        "last_used_at": last_used_at,
    }
    # Наружу как есть, включая None: панель отличает «не задано» от нуля.
    for name in SAMPLING_FIELDS:
        data[name] = getattr(spec, name)
    # То же и про управление контекстом: пустое окно — не ноль.
    for name in CONTEXT_FIELDS:
        data[name] = getattr(spec, name)
    return data


_SPEC_FIELDS = {f.name for f in fields(AgentSpec)}


def spec_from_config(config: dict, *, fallback: AgentSpec) -> AgentSpec:
    """Конфиг из базы обратно в `AgentSpec`. Незнакомые ключи отбрасываются
    молча: базу мог записать сервер другой версии, и падать на чужом поле —
    значит потерять сохранённый диалог."""
    known = {key: value for key, value in (config or {}).items() if key in _SPEC_FIELDS}
    if not known.get("model"):
        return fallback
    known.setdefault("label", fallback.label)
    return AgentSpec(**known)
