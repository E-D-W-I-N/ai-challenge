"""Агент — отдельная сущность: конфиг, память и один метод обмена.

`Agent` принимает **текст пользователя**, а не готовую ленту: сам склеивает
системный промпт, хвост истории и новый вопрос, зовёт `stream_completion`
и дописывает ответ себе в историю. Наружу отдаёт поток событий — из него
и SSE веб-клиента, и вывод CLI.

С хранилищем (`app/store.py`) тот же класс переживает перезапуск: конфиг
и историю он достаёт из базы в конструкторе, а не отдельным вызовом,
который можно забыть позвать.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from typing import AsyncIterator

from .llm import SAMPLING_FIELDS, MissingKeyError, stream_completion
from .schema import AgentSpec
from .store import Store

DEFAULT_HISTORY_LIMIT = 20
"""Окно по умолчанию — десять обменов: помнит начало разговора и не разгоняет
prompt_tokens."""

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")
"""Поля метрик, которые складываются по чату. Остальные — про один вызов:
скорость и время до первого токена суммировать бессмысленно."""


def _usage_number(value) -> int | float | None:
    """Число или `None`. В метриках лежит то, что прислал провайдер и что
    пережило дорогу через JSON, — складывать чужой тип нельзя."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


MAX_STORED_MESSAGES = 400
"""Потолок на хранимое, независимо от окна: без него сотня агентов в долгой
сессии течёт — в модель уходит хвост, а список растёт вечно."""

_last_id = 0
_id_lock = threading.Lock()


def new_agent_id() -> str:
    """Следующий id по счётчику процесса. Для агента без хранилища — годится."""
    global _last_id
    with _id_lock:
        _last_id += 1
        return f"ag_{_last_id:05d}"


def reserve_ids(upto: int) -> None:
    """Сдвигает счётчик за самый большой id из базы.

    После перезапуска счётчик процесса начинается с нуля и выдал бы `ag_00001`
    заново — конструктор поднял бы под ним чужую историю. Настоящий арбитр —
    первичный ключ (`Store.claim_agent_id`), а это подсказка, экономящая попытку.
    """
    global _last_id
    with _id_lock:
        _last_id = max(_last_id, int(upto))


def copy_spec(spec: AgentSpec) -> AgentSpec:
    """Копия конфига, и вглубь тоже.

    `replace` копирует только верхний уровень: `stop`, `response_format`
    и `extra_body` остались бы одним объектом на всех, кого подняли из этого
    конфига, — правка у одного задела бы остальных.
    """
    return replace(
        spec,
        stop=list(spec.stop) if spec.stop else None,
        response_format=copy.deepcopy(spec.response_format),
        extra_body=copy.deepcopy(spec.extra_body or {}),
    )


def _history_limit(spec: AgentSpec) -> int:
    return effective_history_limit(spec.history_limit)


def effective_history_limit(limit: int | None) -> int:
    """Окно в сообщениях: None — дефолт агента, отрицательное — ноль.

    Отдельной функцией: то же число нужно списку слева для чата, которого
    сейчас нет в памяти, — у него есть только строка из базы.
    """
    return DEFAULT_HISTORY_LIMIT if limit is None else max(0, int(limit))


class AgentBusyError(RuntimeError):
    """У агента уже идёт обмен. Второй параллельный запрос — ошибка, а не очередь.

    Иначе две вкладки на одном агенте молча перемешали бы историю.
    """


@dataclass
class Turn:
    """Одна реплика в истории агента."""

    role: str
    content: str
    error: str | None = None
    """Заполнен, если ответ оборвался: реплика в истории есть, но она неполная."""

    reasoning: str = ""
    """Рассуждение модели: приходит отдельным полем дельты, в `content`
    не входит и обратно в модель не уходит."""

    metrics: dict | None = None
    """Метрики этого ответа: по ним рисуются плитки внизу справа."""

    at: float = field(default_factory=time.time)

    def as_message(self) -> dict:
        """Реплика в том виде, в каком она уходит обратно в модель: без
        рассуждения и метрик — в контекст возвращается ответ, а не путь к нему."""
        return {"role": self.role, "content": self.content}

    def as_dict(self) -> dict:
        """Реплика для стенограммы — все поля, а не перечисленные руками."""
        return asdict(self)


@dataclass
class Exchange:
    """Снятая с истории пара «вопрос — ответ» — то, что можно вернуть назад."""

    question: str
    turns: list[Turn]
    depth: int
    """Длина истории сразу после снятия: по ней видно, занял ли место кто-то другой."""


class Agent:
    """Один агент: конфиг + история + один метод обмена.

    Инстанцируется дёшево и много: ни сети, ни потоков, ни подпроцессов
    в конструкторе — только словарь полей.
    """

    def __init__(
        self,
        spec: AgentSpec,
        *,
        agent_id: str | None = None,
        context_length: int | None = None,
        store: Store | None = None,
    ) -> None:
        # Свежий id занимает база, а не процесс: сервер и консоль ходят в один
        # файл, и локальный счётчик выдал бы обоим один номер (см. app/store.py).
        # Без хранилища агент живёт только в памяти — там и счётчика хватает.
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
            # Строку под этот id мы уже заняли. Не достроились — убираем
            # за собой, иначе в списке слева повис бы чат без объекта.
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
        """Всё, что собирается после того, как id уже занят.

        Вынесено из `__init__` ради уборки: конструктор ловит любое исключение
        отсюда и освобождает занятую строку.
        """
        # Один и тот же spec может поднять несколько агентов — каждому нужна
        # своя копия, иначе правка у одного задела бы всех, а день ровно
        # про то, что у ста агентов конфиги **разные**.
        self.spec = copy_spec(spec)
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Системный промпт — в spec."""

        self.detached = False
        """Агента выгрузили из реестра: писать в сессию он больше не вправе."""

        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._reserved = False

        self.store = store
        """Хранилище сессии. None — агент живёт только в памяти процесса.

        Обнуляется при выгрузке из реестра: с этого момента объект больше
        не владелец сессии и писать в неё ему нельзя (см. `detach`).
        """

        if store is not None:
            # Восстановление здесь, а не отдельным вызовом: забыть его позвать
            # должно быть невозможно.
            saved = store.load_session(self.id) if agent_id else None
            if saved is not None:
                self.spec = spec_from_config(saved["config"], fallback=self.spec)
                self.created_at = saved["created_at"]
                self.last_used_at = saved["updated_at"]
                if saved.get("context_length") is not None and context_length is None:
                    # Метрикам нужен context_fill_pct, а каталог моделей —
                    # сетевой запрос: восстановление не должно его ждать.
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
            self.save_config()

    # --- состояние -----------------------------------------------------------

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def busy(self) -> bool:
        return self._lock.locked() or self._reserved

    def reserve(self) -> None:
        """Занимает агента синхронно, до первого await.

        Иначе между проверкой «занят?» и первым `await` в генераторе пролезает
        второй запрос и получает 200 с ошибкой внутри потока вместо честного
        409. Освобождает `release`, и обязательно в finally.
        """
        if self.busy:
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")
        self._reserved = True

    def release(self) -> None:
        self._reserved = False

    @property
    def history_limit(self) -> int:
        return _history_limit(self.spec)

    def cancel(self) -> None:
        """Просит прекратить генерацию. Задачу извне не отменяет: агент сам
        выходит на ближайшем чанке, дописывает частичный ответ и закрывает
        поток штатным `done`."""
        self._cancel.set()

    # --- сборка промпта ------------------------------------------------------

    def window(self, spec: AgentSpec | None = None) -> list[dict]:
        """Хвост истории, который уезжает в модель. При history_limit=0 — пусто."""
        limit = _history_limit(spec if spec is not None else self.spec)
        if not limit:
            return []
        return [turn.as_message() for turn in self.history[-limit:]]

    def build_prompt(self, user_text: str, *, spec: AgentSpec | None = None) -> list[dict]:
        """Системный промпт + окно истории + вопрос этого хода.

        Промпт берётся из конфига каждый раз, поэтому правка в панели видна
        со следующего сообщения. При `history_limit=0` окно пусто, и агент
        отвечает каждый вопрос как первый.
        """
        # `spec` передаёт обмен: он собирает промпт и тело запроса из одного
        # слепка, чтобы правка панели не могла попасть между ними.
        spec = spec if spec is not None else self.spec

        messages: list[dict] = []
        if spec.system:
            messages.append({"role": "system", "content": spec.system})
        messages.extend(self.window(spec))
        messages.append({"role": "user", "content": user_text})
        return messages

    # --- история -------------------------------------------------------------

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
        self._trim()
        if persist:
            self.persist()

    def detach(self) -> None:
        """Снимает с объекта право писать в сессию — зовётся при выгрузке.

        Сессией владеет тот объект, что лежит в реестре. Иначе придержанная
        ссылка пережила бы вытеснение, обращение по id подняло бы из базы
        **второй** объект той же сессии, и запись первого затёрла бы реплики
        второго. Терять нечего: и история, и конфиг уже записаны.
        """
        self.store = None
        self.detached = True

    def persist(self) -> None:
        """Пишет историю в хранилище. Без хранилища — тихо ничего не делает."""
        if self.store is not None:
            self.store.save_history(self.id, self.history)

    def save_config(self) -> None:
        """Пишет конфиг сессии целиком, одним JSON-полем.

        Конфиг едет как `asdict(spec)`, поэтому новое поле сохраняется само,
        а не ждёт, пока про него вспомнят.
        """
        if self.store is None:
            return
        self.store.save_session(
            self.id,
            label=self.spec.label,
            config=asdict(self.spec),
            created_at=self.created_at,
            context_length=self.context_length,
        )

    def _trim(self) -> None:
        if len(self.history) > MAX_STORED_MESSAGES:
            del self.history[: len(self.history) - MAX_STORED_MESSAGES]

    def forget(self) -> None:
        self.history.clear()
        self.persist()

    def usage_summary(self) -> dict | None:
        """Итог по чату: вход, выход, всего, стоимость и число ответов с числами.

        Считается здесь, а не в браузере: иначе цифры в плитках и цифры
        в истории — два разных источника правды, и разъезжаются они молча.
        Отдельной колонки в базе у сводки нет — она выводится из метрик
        реплик, которые уже лежат в `messages.metrics`.

        Реплика без метрик и поле с `None` **пропускаются**, а не считаются
        нулём: неизвестное и ноль на экране обязаны выглядеть по-разному.
        Поэтому чат, в котором ни один ответ не принёс чисел, даёт `None`,
        а не сводку из нулей, и клиент рисует прочерк.
        """
        totals: dict = {name: None for name in USAGE_FIELDS}
        answers = 0
        for turn in self.history:
            if turn.role != "assistant" or not isinstance(turn.metrics, dict):
                continue
            counted = False
            for name in USAGE_FIELDS:
                value = _usage_number(turn.metrics.get(name))
                if value is None:
                    continue
                totals[name] = value if totals[name] is None else totals[name] + value
                counted = True
            # Ответ, у которого метрики есть, но чисел в них нет, обменом
            # не считается: иначе делитель рос бы на пустом месте.
            if counted:
                answers += 1
        if not answers:
            return None
        if totals["cost_usd"] is not None:
            # Копейки от сложения float'ов: цена показывается до шестого знака.
            totals["cost_usd"] = round(totals["cost_usd"], 8)
        totals["answers"] = answers
        return totals

    def transcript(self) -> list[dict]:
        """Ровно реплики диалога, в порядке разговора.

        Системного промпта здесь нет: он конфиг, а не реплика, и виден
        в панели полем `system`.
        """
        return [turn.as_dict() for turn in self.history]

    def as_dict(self, *, with_transcript: bool = False) -> dict:
        data = spec_as_dict(
            self.spec,
            agent_id=self.id,
            history_len=len(self.history),
            usage_total=self.usage_summary(),
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            busy=self.busy,
        )
        if with_transcript:
            data["transcript"] = self.transcript()
        return data

    # --- обмен ---------------------------------------------------------------

    async def ask(self, user_text: str, *, commit: bool = True) -> AsyncIterator[dict]:
        """Один обмен: вопрос → поток событий → запись в историю.

        События: `start`, `reasoning`, `delta`, `metrics`, `error`, `done`.

        История не трогается до конца обмена — откат получается по построению:

        * ответа не случилось вовсе → в историю не пишется ничего, вопрос
          возвращается в событии `done` полем `question`;
        * ответ частичный (обрыв, отмена) → пишутся обе реплики, у ответа
          проставлен `error`.
        """
        if self._lock.locked():
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")

        async with self._lock:
            self._cancel = asyncio.Event()
            cancel = self._cancel
            self.last_used_at = time.time()

            # Слепок конфига на весь обмен. Промпт и тело запроса собираются
            # в двух разных точках, и между ними стоит `yield` события `start`:
            # правка панели, попавшая туда, дала бы смешанный запрос — новую
            # модель со старым системным промптом. Сейчас через этот `yield`
            # никто не приостанавливается, но держится это на устройстве
            # доставки событий, а не на самом обмене: ограничат очередь или
            # добавят один `await` — и окно откроется молча. Со слепком
            # «текущий вызов идёт целиком на одном конфиге» верно
            # по построению, а не по совпадению.
            spec = copy_spec(self.spec)
            context_length = self.context_length

            prompt = self.build_prompt(user_text, spec=spec)

            yield {"type": "start", "resolved_messages": prompt, "question": user_text}

            text = ""
            reasoning = ""
            final_metrics: dict | None = None
            failure: str | None = None
            cancelled = False

            try:
                stream = stream_completion(
                    spec, prompt_override=prompt, context_length=context_length
                )
                async with contextlib.aclosing(stream):
                    async for chunk in stream:
                        kind = chunk["type"]
                        if kind == "delta":
                            text += chunk["text"]
                            yield chunk
                        elif kind == "reasoning":
                            reasoning += chunk["text"]
                            yield chunk
                        elif kind == "metrics":
                            yield chunk
                        elif kind == "error":
                            failure = chunk["message"]
                            final_metrics = chunk["metrics"]
                            yield chunk
                        elif kind == "done":
                            text = chunk["text"]
                            reasoning = chunk.get("reasoning") or reasoning
                            final_metrics = chunk["metrics"]
                        if cancel.is_set():
                            cancelled = True
                            break
            except MissingKeyError as exc:
                failure = str(exc)
                yield {"type": "error", "message": failure, "metrics": None}
            except asyncio.CancelledError:
                # Клиент ушёл: частичный ответ всё равно записываем — он уже
                # оплачен, а следующий вопрос должен видеть, чем кончилось.
                self._commit(user_text, text, "вызов прерван", commit, reasoning, final_metrics)
                raise
            except Exception as exc:  # noqa: BLE001 — падает обмен, процесс живёт
                failure = f"{type(exc).__name__}: {exc}"
                yield {"type": "error", "message": failure, "metrics": None}

            if cancelled and failure is None:
                failure = "генерация отменена"

            committed = self._commit(user_text, text, failure, commit, reasoning, final_metrics)

            done: dict = {
                "type": "done",
                "text": text,
                "reasoning": reasoning,
                "metrics": final_metrics,
                "cancelled": cancelled,
                "error": failure,
                "committed": committed,
            }
            if not committed:
                # Обмена не было: вопрос нельзя оставлять в ленте клиента —
                # он вернётся в поле ввода и его можно будет повторить.
                done["question"] = user_text
            yield done

    def _commit(
        self,
        user_text: str,
        answer: str,
        failure: str | None,
        commit: bool,
        reasoning: str = "",
        metrics: dict | None = None,
    ) -> bool:
        """Пишет обмен в историю. Возвращает False, если писать было нечего."""
        if not commit or not answer.strip():
            return False

        # Вопрос и ответ ложатся в базу парой, одной транзакцией: иначе,
        # умри процесс между ними, в базе остался бы вопрос без ответа.
        with self.store.tx() if self.store is not None else contextlib.nullcontext():
            self.remember("user", user_text, persist=False)
            # Ответ, оборванный на середине, всё равно часть диалога — но помечен:
            # следующий вопрос должен видеть, что предыдущий ответ неполный.
            self.remember(
                "assistant", answer, error=failure, reasoning=reasoning, metrics=metrics
            )
        return True

    def take_last_exchange(self) -> Exchange | None:
        """Снимает с истории последнюю пару «вопрос — ответ» целиком.

        Пара уходит до вызова: перегенерация обязана **заменить** ответ,
        а не дописать второй, и модель должна увидеть тот же контекст.
        Возвращается снятое целиком, а не только вопрос: если вызов не отдаст
        ни токена, `restore` кладёт обратно и вопрос, и прежний ответ.

        В базу снятое не пишется: пока перегенерация не удалась, в файле лежит
        ровно исходная пара, а удачная перепишет историю целиком. Ни дублей,
        ни дыр в нумерации ни в одном исходе.
        """
        if not self.history or self.history[-1].role != "assistant":
            return None
        taken = [self.history.pop()]
        if self.history and self.history[-1].role == "user":
            taken.insert(0, self.history.pop())
        question = taken[0].content if taken[0].role == "user" else None
        if question is None:
            # Ответ без вопроса переспрашивать нечем — кладём обратно.
            self.history.extend(taken)
            return None
        return Exchange(question=question, turns=taken, depth=len(self.history))

    def restore(self, exchange: Exchange) -> bool:
        """Кладёт снятую пару обратно, если её место никто не занял.

        Записался новый обмен — перегенерация удалась (или оборвалась
        с частичным ответом, что тоже записано), и класть старое поверх
        нельзя: в ленте было бы два ответа на один вопрос.
        """
        if len(self.history) != exchange.depth:
            return False
        self.history.extend(exchange.turns)
        # В норме база с момента снятия не менялась, но если новый обмен успел
        # записаться и откатиться — файл обязан сойтись с памятью.
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
) -> dict:
    """Конфиг чата так, как его ждут список слева и панель справа.

    Функция, а не метод: тем же форматом описывается чат, которого сейчас нет
    в памяти. Клиент не должен различать «поднято» и «лежит в базе».
    """
    data = {
        "id": agent_id,
        "label": spec.label,
        "model": spec.model,
        "stop": spec.stop,
        "response_format": spec.response_format,
        "extra_body": spec.extra_body,
        "system": spec.system,
        "history_limit": effective_history_limit(spec.history_limit),
        "history_len": history_len,
        # Итог по чату едет тем же путём, что и длина истории: клиент не должен
        # различать «чат поднят в память» и «чат лежит в базе» — у выгруженного
        # сводки нет, и это `None`, то есть прочерк, а не ноль.
        "usage_total": usage_total,
        "busy": busy,
        "created_at": created_at,
        "last_used_at": last_used_at,
    }
    # Параметры сэмплирования уходят наружу как есть, включая None:
    # панель справа отличает «не задано» от нуля, и ей нужно и то и другое.
    for name in SAMPLING_FIELDS:
        data[name] = getattr(spec, name)
    return data


_SPEC_FIELDS = {f.name for f in fields(AgentSpec)}


def spec_from_config(config: dict, *, fallback: AgentSpec) -> AgentSpec:
    """Конфиг из базы обратно в `AgentSpec`.

    Незнакомые ключи отбрасываются молча: базу мог записать сервер другой
    версии, и падать на чужом поле — значит потерять сохранённый диалог.
    """
    known = {key: value for key, value in (config or {}).items() if key in _SPEC_FIELDS}
    if not known.get("model"):
        return fallback
    known.setdefault("label", fallback.label)
    return AgentSpec(**known)
