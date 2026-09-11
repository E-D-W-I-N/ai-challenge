"""Агент — отдельная сущность: конфиг, память и один метод обмена.

`Agent` принимает **текст пользователя**, а не готовую ленту: сам склеивает
системный промпт, всю историю и новый вопрос, зовёт `stream_completion`
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
from .schema import CONTEXT_FIELDS, AgentSpec
from .store import Store

USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")
"""Поля метрик, которые складываются по чату. Остальные — про один вызов:
скорость и время до первого токена суммировать бессмысленно."""


def _usage_number(value) -> int | float | None:
    """Число или `None`. В метриках лежит то, что прислал провайдер и что
    пережило дорогу через JSON, — складывать чужой тип нельзя."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


COMPRESS_SYSTEM = (
    "Ты сворачиваешь начало разговора в сжатый пересказ. Не отвечай на реплики "
    "и не обращайся к собеседнику: твой ответ целиком — пересказ, и он встанет "
    "в контекст вместо свёрнутых реплик. Сохрани факты, имена, числа, решения "
    "и договорённости: дальше по ним будут задавать вопросы."
)
"""Системный промпт вызова на сжатие. Свой, а не `spec.system`: чат просили
отвечать, а здесь просят пересказывать, и чужая роль испортила бы пересказ."""


def build_compress_prompt(chunk: list["Turn"], previous: str | None = None) -> list[dict]:
    """Промпт вызова на сжатие: прошлая сводка плюс **новые** реплики.

    Сворачивание идёт инкрементально, а не пересказывает разговор с начала
    каждый раз: пересказ всей истории на каждом сворачивании съел бы ту самую
    экономию, ради которой сжатие заведено, — и рос бы вместе с разговором.
    """
    parts = []
    if previous:
        parts.append("Пересказ начала разговора, который надо продолжить:\n" + previous)
    lines = [
        f"{'Пользователь' if turn.role == 'user' else 'Ассистент'}: {turn.content}"
        for turn in chunk
    ]
    parts.append("Реплики, которые надо добавить в пересказ:\n" + "\n".join(lines))
    return [
        {"role": "system", "content": COMPRESS_SYSTEM},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def summary_message(content: str, covered: int) -> dict:
    """Сводка так, как она встаёт в промпт: роль `user` и явная подпись,
    **не** `system`. Системный промпт по проекту живёт ровно в одном месте —
    `spec.system`, — и голый чат обязан уходить в модель без системной реплики.
    Подпись не украшение: без неё модель приняла бы пересказ за реплику
    пользователя и стала бы отвечать на него."""
    return {
        "role": "user",
        "content": (
            f"[пересказ начала разговора, свёрнуто реплик: {covered}]\n"
            f"{content}\n"
            "[дальше — последние реплики как есть]"
        ),
    }


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


class AgentBusyError(RuntimeError):
    """У агента уже идёт обмен. Второй параллельный запрос — ошибка, а не очередь:
    иначе две вкладки на одном агенте молча перемешали бы историю."""


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
        """Реплика в том виде, в каком она уходит в модель: в контекст
        возвращается ответ, а не путь к нему."""
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
        # Свой экземпляр конфига каждому агенту: один и тот же spec может
        # поднять сотню, и правка у одного не должна задеть остальных.
        self.spec = copy_spec(spec)
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Системный промпт — в spec.

        Сжатие её **не трогает**: она полная всегда, сколько бы сворачиваний
        ни прошло. Иначе сломалась бы перегенерация — `take_last_exchange`
        ждёт хвост `["user", "assistant"]`, а `restore` сверяет длину.
        """

        self.summaries: list[dict] = []
        """Сводки начала разговора, по порядку сворачивания: `{upto, content,
        metrics, at}`. Последняя — действующая, `upto` у неё говорит, сколько
        первых реплик истории она собой заменяет. Список, а не одна сводка:
        у каждого сворачивания свои метрики, и без них счёт стоимости сжатия
        был бы неполным."""

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
                # Сводки лежат отдельно от истории и поднимаются отдельно:
                # перезапись истории их не трогает, и после перезапуска
                # свёрнутое начало разговора остаётся свёрнутым.
                self.summaries = store.load_summaries(self.id)
            self.save_config()

    # --- состояние -----------------------------------------------------------

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

    def cancel(self) -> None:
        """Просит прекратить генерацию. Задачу извне не отменяет: агент сам
        выходит на ближайшем чанке, дописывает частичный ответ и закрывает
        поток штатным `done`."""
        self._cancel.set()

    # --- история -------------------------------------------------------------

    def summary_cover(self, spec: AgentSpec | None = None) -> int:
        """Сколько первых реплик истории заменено сводкой — 0, если сводки нет
        или сжатие выключено.

        Больше длины истории не бывает: перегенерация снимает пару **с конца**,
        и сводка не вправе покрывать то, чего в истории уже нет. Без этого
        зажима короткий чат после перегенерации дал бы промпт, в котором
        свёрнутого больше, чем было.
        """
        spec = spec if spec is not None else self.spec
        if spec.keep_last is None or not self.summaries:
            return 0
        upto = self.summaries[-1].get("upto")
        if not isinstance(upto, int) or upto <= 0:
            return 0
        return min(upto, len(self.history))

    def build_prompt(self, user_text: str, *, spec: AgentSpec | None = None) -> list[dict]:
        """Системный промпт + сводка и хвост истории (или вся история) + вопрос.

        **Без сводки история не режется.** Сжатие выключено (`keep_last is
        None`) или сворачиваться ещё не успело — уезжает вся история целиком,
        как в Дне 8. Есть сводка — уезжает она и ровно те реплики, которых
        она не покрывает. Молчаливой обрезки нет ни в одном состоянии: число
        свёрнутых плюс длина хвоста всегда равно длине истории.

        Конфиг читается каждый раз, поэтому правка в панели видна со следующего
        сообщения; `spec` передаёт обмен — он собирает промпт и тело запроса
        из одного слепка.
        """
        spec = spec if spec is not None else self.spec

        messages: list[dict] = []
        if spec.system:
            messages.append({"role": "system", "content": spec.system})
        covered = self.summary_cover(spec)
        if covered:
            messages.append(summary_message(self.summaries[-1]["content"], covered))
        messages.extend(turn.as_message() for turn in self.history[covered:])
        messages.append({"role": "user", "content": user_text})
        return messages

    async def compress(self, spec: AgentSpec, context_length: int | None = None) -> None:
        """Сворачивает начало истории в сводку, если несвёрнутого накопилось
        больше порога. Зовётся из `ask` **до** сборки промпта: обмен, который
        запустил сворачивание, уже сам едет сжатым, и экономия видна во
        входных токенах этого же ответа, а не следующего.

        Сама история не трогается — сводка лишь заменяет её начало при сборке
        промпта. Не удалось сжатие (ошибка, пустой ответ) — история не режется:
        обмен просто уедет полным.
        """
        keep, every = spec.keep_last, spec.compress_every
        if keep is None or every is None or keep < 0 or every <= 0:
            return
        covered = self.summary_cover(spec)
        # Граница не рвёт пару: история идёт парами «вопрос — ответ», обе
        # реплики пишутся разом, и свёрнутый вопрос без своего ответа сделал бы
        # хвост бессмысленным. Округляем вниз до чётного.
        border = ((len(self.history) - keep) // 2) * 2
        if border - covered < every:
            return
        chunk = self.history[covered:border]
        if not chunk:
            return

        # Формат ответа и стоп-строки на время сжатия сняты: чат с
        # {"type": "json_object"} вернул бы вместо пересказа объект, а
        # стоп-строка оборвала бы пересказ на середине. Модель и параметры
        # сэмплирования — те же: вторая модель развалила бы счёт на две цены.
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

    def detach(self) -> None:
        """Снимает с объекта право писать в сессию — зовётся при выгрузке.

        Сессией владеет тот объект, что лежит в реестре. Иначе придержанная
        ссылка пережила бы вытеснение, обращение по id подняло бы из базы
        **второй** объект той же сессии, и запись первого затёрла бы реплики
        второго. Терять нечего: и история, и конфиг уже записаны.
        """
        self.store = None

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

    def forget(self) -> None:
        """Забывает разговор целиком — и историю, и сводки.

        Сводка заменяла начало истории; истории больше нет, и покрывать ей
        нечего. Оставленная, она накрыла бы собой начало **следующего**
        разговора в этом же чате: `summary_cover` зажат длиной истории, и на
        отросшей заново истории мёртвая сводка снова стала бы действующей.
        """
        self.history.clear()
        self.summaries = []
        if self.store is not None:
            self.store.save_summaries(self.id, [])
        self.persist()

    def usage_summary(self) -> dict | None:
        """Итог по чату: вход, выход, всего, стоимость и число ответов с числами.

        Считается здесь, а не в браузере: иначе плитки и лента — два источника
        правды, и разъезжаются они молча. Колонки в базе у неё нет: она
        выводится из `messages.metrics` и `summaries.metrics`.

        Реплика без метрик и поле с `None` **пропускаются**, а не считаются
        нулём: неизвестное и ноль на экране обязаны выглядеть по-разному —
        чат без чисел даёт `None`, и клиент рисует прочерк.
        """
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
            # Ответ, у которого метрики есть, но чисел в них нет, обменом
            # не считается: иначе делитель рос бы на пустом месте.
            if add(turn.metrics):
                answers += 1

        # Второй проход — по сводкам. Вызов на сжатие тоже уехал в модель и
        # тоже оплачен: экономия, не вычитающая стоимость сжатия, — враньё.
        # Сводки живут не в истории, поэтому складываются отдельно; карточкой
        # в ленте сжатие не становится и `exchanges()` не трогает.
        for item in self.summaries:
            if add(item.get("metrics")):
                answers += 1

        if not answers:
            return None
        if totals["cost_usd"] is not None:
            # Копейки от сложения float'ов: цена показывается до шестого знака.
            totals["cost_usd"] = round(totals["cost_usd"], 8)
        return totals

    def exchanges(self) -> int:
        """Сколько ответов модели в истории — столько карточек в ленте.

        Считаются **все**, а не только принёсшие числа: плитка «Сообщений»
        отвечает на «сколько раз поговорили». Слагаемых в суммах может быть
        меньше — провайдер вправе смолчать о usage, — но это видно по самим
        суммам, а не по счётчику.
        """
        return sum(1 for turn in self.history if turn.role == "assistant")

    def transcript(self) -> list[dict]:
        """Ровно реплики диалога. Системного промпта здесь нет: он конфиг,
        а не реплика, и виден в панели полем `system`."""
        return [asdict(turn) for turn in self.history]

    def as_dict(self, *, with_transcript: bool = False) -> dict:
        data = spec_as_dict(
            self.spec,
            agent_id=self.id,
            history_len=len(self.history),
            usage_total=self.usage_summary(),
            exchanges=self.exchanges(),
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            busy=self.busy,
        )
        if with_transcript:
            data["transcript"] = self.transcript()
        return data

    # --- обмен ---------------------------------------------------------------

    async def ask(self, user_text: str) -> AsyncIterator[dict]:
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

            # Сворачиваем лениво и **до** сборки промпта: этот же обмен уедет
            # сжатым, и экономия видна во входных токенах его собственного
            # ответа, а не следующего.
            await self.compress(spec, context_length)
            covered = self.summary_cover(spec)

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
                self._commit(user_text, text, "вызов прерван", reasoning, final_metrics)
                raise
            except Exception as exc:  # noqa: BLE001 — падает обмен, процесс живёт
                failure = f"{type(exc).__name__}: {exc}"
                yield {"type": "error", "message": failure, "metrics": None}

            if cancelled and failure is None:
                failure = "генерация отменена"

            # Сколько реплик уехало сводкой вместо себя — знание агента, а не
            # провайдера, и место ему рядом с числами обмена: строка под
            # ответом покажет его там же, где входные токены, которые оно
            # уменьшило. В суммы по чату ключ не идёт — их считает USAGE_FIELDS.
            if covered and isinstance(final_metrics, dict):
                final_metrics = {**final_metrics, "summarized": covered}

            committed = self._commit(user_text, text, failure, reasoning, final_metrics)

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
        reasoning: str = "",
        metrics: dict | None = None,
    ) -> bool:
        """Пишет обмен в историю. Возвращает False, если писать было нечего."""
        if not answer.strip():
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
        """Снимает последнюю пару «вопрос — ответ»: перегенерация обязана
        **заменить** ответ, а не дописать второй, и модель должна увидеть тот же
        контекст. Снятое возвращается целиком — если вызов не отдаст ни токена,
        `restore` кладёт обратно и вопрос, и прежний ответ.

        В базу снятое не пишется: пока перегенерация не удалась, в файле лежит
        ровно исходная пара, а удачная перепишет историю целиком. Ни дублей,
        ни дыр в нумерации ни в одном исходе.
        """
        if [turn.role for turn in self.history[-2:]] != ["user", "assistant"]:
            return None
        taken = self.history[-2:]
        del self.history[-2:]
        return Exchange(question=taken[0].content, turns=taken, depth=len(self.history))

    def restore(self, exchange: Exchange) -> bool:
        """Кладёт снятую пару обратно, если её место никто не занял. Записался
        новый обмен — перегенерация удалась (или оборвалась с частичным ответом,
        что тоже записано), и класть старое поверх нельзя: в ленте было бы два
        ответа на один вопрос."""
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
    exchanges: int | None = None,
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
        "history_len": history_len,
        # Итог по чату едет тем же путём, что и длина истории: клиент не должен
        # различать «чат поднят в память» и «чат лежит в базе» — у выгруженного
        # сводки нет, и это `None`, то есть прочерк, а не ноль.
        "usage_total": usage_total,
        # Число сообщений едет отдельно от сумм: у чата без единого usage сумм
        # нет вовсе (`None`), а ответы в нём всё равно были, и плитка обязана
        # их назвать.
        "exchanges": exchanges,
        "busy": busy,
        "created_at": created_at,
        "last_used_at": last_used_at,
    }
    # Параметры сэмплирования уходят наружу как есть, включая None:
    # панель справа отличает «не задано» от нуля, и ей нужно и то и другое.
    for name in SAMPLING_FIELDS:
        data[name] = getattr(spec, name)
    # То же и про управление контекстом: пустое окно памяти значит «сжатия
    # нет», и панель обязана показать именно пустое поле, а не ноль.
    for name in CONTEXT_FIELDS:
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
