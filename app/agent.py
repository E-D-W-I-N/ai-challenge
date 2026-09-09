"""Агент — отдельная сущность: конфиг, память и один метод обмена.

До Дня 6 логика вызова была размазана: ленту диалога хранил браузер и слал её
целиком в каждый запрос, а сервер не знал ни про диалог, ни про то, кто
спрашивает. Здесь она собрана в один класс.

`Agent` принимает **текст пользователя**, а не готовую ленту: сам склеивает
системный промпт, стартовые сообщения, хвост истории и новый вопрос, зовёт
`stream_completion` и дописывает ответ себе в историю. Наружу отдаёт поток
событий — из него и SSE стенда, и вывод CLI.

С Дня 7 у агента есть хранилище (`app/store.py`). Восстановление истории
сделано **в конструкторе**: агенту, которому дали `store` и чужой `agent_id`,
история приезжает сама. Отдельного `restore()`, который можно забыть позвать,
нет и не должно быть. Запись идёт после каждого завершённого обмена — тем же
правилом, что уже действует в памяти: несостоявшийся обмен не пишется вовсе,
частичный ответ пишется с пометкой ошибки.

Агент ничего не знает ни про FastAPI, ни про SSE, ни про реестр: сто агентов —
это сто объектов в одном процессе, а не сто процессов.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import AsyncIterator

from .llm import MissingKeyError, stream_completion
from .schema import AgentSpec
from .store import Store

DEFAULT_HISTORY_LIMIT = 20
"""Сколько последних сообщений уходит в окно, если spec.history_limit не задан.

Десять обменов: достаточно, чтобы разговор помнил начало, и мало настолько,
чтобы длинная сессия не разогнала prompt_tokens до цены отдельного дня.
"""

MAX_STORED_MESSAGES = 400
"""Жёсткий потолок на хранимое, независимо от окна.

Окно ограничивает то, что уезжает в модель, а этот потолок — то, что живёт
в памяти процесса. Без него сотня агентов в долгой сессии течёт: в модель
уходит хвост, а список растёт вечно.
"""

_last_id = 0
_id_lock = threading.Lock()


def new_agent_id() -> str:
    global _last_id
    with _id_lock:
        _last_id += 1
        return f"ag_{_last_id:05d}"


def reserve_ids(upto: int) -> None:
    """Сдвигает счётчик так, чтобы новые агенты не заняли id из базы.

    Счётчик живёт в процессе и после перезапуска начинается с нуля. Без этой
    поправки второй запуск выдал бы `ag_00001` заново, а конструктор поднял бы
    под этим id чужую историю: свежий агент молча унаследовал бы прошлый диалог.
    """
    global _last_id
    with _id_lock:
        _last_id = max(_last_id, int(upto))


class AgentBusyError(RuntimeError):
    """У агента уже идёт обмен. Второй параллельный запрос — ошибка, а не очередь.

    Без этого две вкладки, открытые на одного агента, молча перемешали бы
    историю: обе дописали бы свой вопрос и свой ответ в произвольном порядке.
    """


@dataclass
class Turn:
    """Одна реплика в истории агента."""

    role: str
    content: str
    error: str | None = None
    """Заполнен, если ответ оборвался: реплика в истории есть, но она неполная."""

    at: float = field(default_factory=time.time)

    def as_message(self) -> dict:
        return {"role": self.role, "content": self.content}

    def as_dict(self) -> dict:
        return {"role": self.role, "content": self.content, "error": self.error, "at": self.at}


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
        parent_id: str | None = None,
        context_length: int | None = None,
        store: Store | None = None,
    ) -> None:
        # Копия конфига, и вглубь тоже: spec может быть колонкой из SCENARIOS,
        # общей на все прогоны, а `replace` копирует только верхний уровень —
        # messages, stop, response_format и extra_body остались бы одним
        # объектом на сотню агентов и на сам день. Правка любого из них
        # у одного агента задела бы всех остальных, а день ровно про то,
        # что у ста агентов конфиги **разные**.
        self.spec = replace(
            spec,
            messages=[dict(m) for m in (spec.messages or [])],
            stop=list(spec.stop) if spec.stop else None,
            response_format=copy.deepcopy(spec.response_format),
            extra_body=copy.deepcopy(spec.extra_body or {}),
        )
        self.id = agent_id or new_agent_id()
        self.parent_id = parent_id
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Стартовые сообщения — в spec."""

        self.seed_messages: list[dict] = [dict(m) for m in (spec.messages or [])]
        """Стартовый промпт агента. У колонки с depends_on подменяется на старте
        прогона результатом подстановки — поэтому это поле, а не spec.messages."""

        self.overrides: dict = {}
        """Поля, которые пользователь сменил руками через PATCH.

        Нужны, чтобы выбор в дропдауне пережил «Старт»: прогон спавнит свежий
        набор субагентов вместо предыдущего, и без этого списка он поднял бы
        колонки на моделях из day.py, молча отменив выбор пользователя.
        """

        self.seed_used = False
        """Стартовый вопрос уже лёг в историю обычным ходом.

        До этого момента он подклеивается к промпту: колонка показывает его
        в ленте ещё до «Старта», и промпт обязан сходиться с тем, что видно
        на экране. После «Старта» вопрос приезжает окном истории, и второй раз
        его подклеивать нельзя — иначе `history_limit=0` перестал бы значить
        хоть что-нибудь.
        """

        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._reserved = False

        self.store = store
        """Хранилище сессии. None — агент живёт только в памяти процесса."""

        if store is not None:
            # Восстановление — здесь, а не отдельным вызовом из UI: забыть
            # позвать restore() должно быть невозможно. У свежего id в базе
            # ничего нет, и агент просто заводит себе строку.
            saved = store.load_session(self.id) if agent_id else None
            if saved is not None:
                self.parent_id = saved["parent_id"]
                self.created_at = saved["created_at"]
                self.last_used_at = saved["updated_at"]
                self.seed_messages = [dict(m) for m in saved["seed"]]
                self.overrides = dict(saved["overrides"])
                self.seed_used = saved["seed_used"]
                self.history = [
                    Turn(role=m["role"], content=m["content"], error=m["error"], at=m["at"])
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

        Ручка отвечает 409 ещё до того, как начнёт стримить, — иначе между
        проверкой «занят?» и первым `await` в генераторе успевает пролезть
        второй запрос, и вместо честного 409 он получит 200 с ошибкой внутри
        потока. Освобождает `release`, и обязательно в finally.
        """
        if self.busy:
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")
        self._reserved = True

    def release(self) -> None:
        self._reserved = False

    @property
    def history_limit(self) -> int:
        limit = self.spec.history_limit
        return DEFAULT_HISTORY_LIMIT if limit is None else max(0, int(limit))

    def cancel(self) -> None:
        """Просит агента прекратить текущую генерацию.

        Не отменяет задачу извне: агент сам выходит из стрима на ближайшем
        чанке, дописывает частичный ответ в историю и закрывает поток событий
        штатным `done`.
        """
        self._cancel.set()

    # --- сборка промпта ------------------------------------------------------

    def window(self) -> list[dict]:
        """Хвост истории, который уезжает в модель. При history_limit=0 — пусто."""
        limit = self.history_limit
        if not limit:
            return []
        return [turn.as_message() for turn in self.history[-limit:]]

    def _seed_split(self) -> tuple[list[dict], str | None]:
        """Делит стартовые сообщения на обстановку и первый вопрос.

        Обстановка — системная инструкция и всё, что не последняя реплика
        пользователя. Это конфиг агента, он уезжает в модель всегда.

        Последняя реплика пользователя — не конфиг, а первый **ход** разговора.
        Агент без памяти забывает его так же, как забыл бы любой другой ход:
        иначе `history_limit=0` не значил бы ничего, если автор дня положил
        задачу в `messages`, — а он её туда и кладёт все пять прошлых дней.
        """
        seed = self.seed_messages
        if seed and seed[-1].get("role") == "user":
            return [dict(m) for m in seed[:-1]], seed[-1].get("content", "")
        return [dict(m) for m in seed], None

    @property
    def seed_question(self) -> str | None:
        """Первый вопрос агента: его задаёт прогон, если своего вопроса нет."""
        return self._seed_split()[1]

    def starting_prompt(self) -> list[dict]:
        """Стартовый промпт целиком — системная инструкция и `messages`.

        Не зависит от истории: это то, что показывают в колонке до «Старта»,
        и то, с чего начинается стенограмма.
        """
        messages: list[dict] = []
        has_system = any(m.get("role") == "system" for m in self.seed_messages)
        if self.spec.system and not has_system:
            messages.append({"role": "system", "content": self.spec.system})
        messages.extend(dict(m) for m in self.seed_messages)
        return messages

    def build_prompt(self, user_text: str | None = None) -> list[dict]:
        """Обстановка + окно истории + вопрос этого хода.

        `user_text=None` — прогон стартового промпта: с пустой историей это
        ровно `spec.messages`, то есть в точности то, что уходило в модель
        до появления агентов.

        С заданным `user_text` первый вопрос из `messages` в промпт больше
        не подклеивается — но только после того, как он **сам стал ходом**,
        то есть после «Старта». Именно здесь `history_limit=0` и становится
        правдой: колонка «без памяти» на втором вопросе не знает ни вопроса,
        ни своего ответа.

        Пока «Старта» не было, вопрос едет в каждом ходу: колонка показывает
        его в ленте как стартовый, и промпт обязан сходиться с тем, что видно
        на экране. Признак — `seed_used`, а не пустая история: разговор с
        колонкой можно завести и до прогона, и после первого же ручного обмена
        вводная иначе молча исчезала бы из промпта, оставаясь на экране.
        """
        setting, question = self._seed_split()
        messages: list[dict] = []
        has_system = any(m.get("role") == "system" for m in setting)
        if self.spec.system and not has_system:
            messages.append({"role": "system", "content": self.spec.system})
        messages.extend(setting)

        if user_text is None:
            messages.extend(self.window())
            if question is not None:
                messages.append({"role": "user", "content": question})
            return messages

        if question is not None and not self.seed_used:
            messages.append({"role": "user", "content": question})
        messages.extend(self.window())
        messages.append({"role": "user", "content": user_text})
        return messages

    # --- история -------------------------------------------------------------

    def remember(
        self, role: str, content: str, error: str | None = None, *, persist: bool = True
    ) -> None:
        self.history.append(Turn(role=role, content=content, error=error))
        self._trim()
        if persist:
            self.persist()

    def remember_exchange(
        self, question: str | None, answer: str, error: str | None = None
    ) -> None:
        """Вопрос и ответ ложатся в историю парой и пишутся одной транзакцией.

        Отдельная запись вопроса оставила бы в базе вопрос без ответа, если
        процесс умрёт между двумя `remember`. В памяти такого не бывает —
        в базе тоже не должно.
        """
        if question is not None:
            self.remember("user", question, persist=False)
        self.remember("assistant", answer, error=error)

    def persist(self) -> None:
        """Пишет историю в хранилище. Без хранилища — тихо ничего не делает."""
        if self.store is not None:
            self.store.save_history(self.id, self.history)

    def save_config(self) -> None:
        """Пишет конфиг сессии: модель, семплирование, стартовые сообщения, overrides.

        Зовётся при создании и после каждой правки конфига — смены модели
        через PATCH, подстановки depends_on, переноса overrides на «Старте».
        Иначе после перезапуска сессия поднялась бы на модели из day.py,
        молча отменив выбор пользователя.
        """
        if self.store is None:
            return
        self.store.save_session(
            self.id,
            parent_id=self.parent_id,
            label=self.spec.label,
            config=asdict(self.spec),
            seed=[dict(m) for m in self.seed_messages],
            overrides=dict(self.overrides),
            seed_used=self.seed_used,
            created_at=self.created_at,
        )

    def _trim(self) -> None:
        if len(self.history) > MAX_STORED_MESSAGES:
            del self.history[: len(self.history) - MAX_STORED_MESSAGES]

    def forget(self) -> None:
        self.history.clear()
        self.persist()

    def transcript(self) -> list[dict]:
        """Стартовый промпт и всё, что наговорили после него, — одним списком."""
        seed = [
            {"role": m.get("role", "?"), "content": m.get("content", ""), "seed": True}
            for m in self.starting_prompt()
        ]
        return seed + [turn.as_dict() for turn in self.history]

    def as_dict(self, *, with_transcript: bool = False) -> dict:
        data = {
            "id": self.id,
            "parent_id": self.parent_id,
            "label": self.spec.label,
            "model": self.spec.model,
            "temperature": self.spec.temperature,
            "max_tokens": self.spec.max_tokens,
            "stop": self.spec.stop,
            "response_format": self.spec.response_format,
            "extra_body": self.spec.extra_body,
            "system": self.spec.system,
            "note": self.spec.note,
            "repeats": self.spec.repeats,
            "depends_on": self.spec.depends_on,
            "history_limit": self.history_limit,
            "overrides": dict(self.overrides),
            "history_len": len(self.history),
            "busy": self.busy,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }
        if with_transcript:
            data["seed_messages"] = self.starting_prompt()
            data["transcript"] = self.transcript()
        return data

    # --- обмен ---------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def hold(self):
        """Занимает агента, не делая вызова: под этим идёт `/прогон`.

        Пока родитель раздаёт работу субагентам, он занят так же, как если бы
        сам говорил с моделью, — второй `/прогон` в ту же сессию получит 409.
        """
        if self._lock.locked():
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")
        async with self._lock:
            self.last_used_at = time.time()
            yield

    async def ask(self, user_text: str | None = None, *, commit: bool = True) -> AsyncIterator[dict]:
        """Один обмен: вопрос → поток событий → запись в историю.

        События: start, repeat_start, delta, metrics, repeat_error, repeat_done,
        error, done. Имена намеренно близки к событиям стенда: наверху к ним
        добавляют label колонки и id агента, но не переводят.

        История не трогается до конца обмена — откат получается по построению:

        * ответа не случилось вовсе → в историю не пишется ничего, вопрос
          возвращается в событии `done` полем `question`;
        * ответ частичный (обрыв, отмена) → пишутся обе реплики, у ответа
          проставлен `error`;
        * серия `repeats` коммитит только последний удачный прогон, а не все N.
        """
        if self._lock.locked():
            raise AgentBusyError(f"агент {self.id} уже занят: дождитесь текущего ответа")

        async with self._lock:
            self._cancel = asyncio.Event()
            cancel = self._cancel
            self.last_used_at = time.time()

            prompt = self.build_prompt(user_text)
            # Прогон задаёт стартовый вопрос из конфига — и коммитит его
            # в историю как обычный ход: агент с памятью обязан помнить,
            # на что он отвечал, а не только чем ответил.
            question = user_text if user_text is not None else self.seed_question
            is_seed = user_text is None
            total = max(1, int(self.spec.repeats or 1))

            yield {
                "type": "start",
                "resolved_messages": prompt,
                "repeats": total,
                "question": question,
            }

            texts: list[str] = []
            last_metrics: dict | None = None
            failure: str | None = None
            partial = ""
            cancelled = False

            for index in range(total):
                if cancel.is_set():
                    cancelled = True
                    break

                yield {"type": "repeat_start", "repeat": index, "repeats": total}

                text = ""
                final_metrics: dict | None = None
                broken = False
                try:
                    stream = stream_completion(
                        self.spec, prompt_override=prompt, context_length=self.context_length
                    )
                    async with contextlib.aclosing(stream):
                        async for chunk in stream:
                            kind = chunk["type"]
                            if kind == "delta":
                                text += chunk["text"]
                                yield {
                                    "type": "delta",
                                    "repeat": index,
                                    "text": chunk["text"],
                                    "metrics": chunk["metrics"],
                                }
                            elif kind == "metrics":
                                yield {
                                    "type": "metrics",
                                    "repeat": index,
                                    "metrics": chunk["metrics"],
                                }
                            elif kind == "error":
                                broken = True
                                failure = chunk["message"]
                                # Упавший прогон не отменяет остальные: серия идёт
                                # дальше, агент жив, если удался хоть один прогон.
                                yield {
                                    "type": "repeat_error",
                                    "repeat": index,
                                    "message": chunk["message"],
                                    "metrics": chunk["metrics"],
                                }
                            elif kind == "done":
                                text = chunk["text"]
                                final_metrics = chunk["metrics"]
                            if cancel.is_set():
                                cancelled = True
                                break
                except MissingKeyError as exc:
                    failure = str(exc)
                    yield {"type": "error", "message": failure, "metrics": None}
                    self._commit(question, texts, partial or text, failure, commit, seed=is_seed)
                    return
                except asyncio.CancelledError:
                    # Клиент ушёл: частичный ответ всё равно записываем — он уже
                    # оплачен, а следующий вопрос должен видеть, чем кончилось.
                    self._commit(question, texts, partial or text, "вызов прерван", commit, seed=is_seed)
                    raise
                except Exception as exc:  # noqa: BLE001 — падает обмен, процесс живёт
                    failure = f"{type(exc).__name__}: {exc}"
                    yield {"type": "error", "message": failure, "metrics": None}
                    self._commit(question, texts, partial or text, failure, commit, seed=is_seed)
                    return

                if text:
                    partial = text
                if cancelled:
                    break
                if not broken:
                    texts.append(text)
                    last_metrics = final_metrics or last_metrics
                    yield {
                        "type": "repeat_done",
                        "repeat": index,
                        "text": text,
                        "metrics": final_metrics,
                    }

            if cancelled and failure is None:
                failure = "генерация отменена"

            committed = self._commit(question, texts, partial, failure, commit, seed=is_seed)

            done: dict = {
                "type": "done",
                "text": texts[-1] if texts else "",
                "metrics": last_metrics,
                "repeats": total,
                "texts": list(texts),
                "unique": len({t.strip() for t in texts}),
                "cancelled": cancelled,
                "error": failure,
                "committed": committed,
            }
            if not committed and question is not None:
                # Обмена не было: вопрос нельзя оставлять в ленте клиента —
                # он вернётся в поле ввода и его можно будет повторить.
                done["question"] = question
            yield done

    def _commit(
        self,
        user_text: str | None,
        texts: list[str],
        partial: str,
        failure: str | None,
        commit: bool,
        *,
        seed: bool = False,
    ) -> bool:
        """Пишет обмен в историю и в базу. Возвращает False, если писать было нечего.

        `seed=True` — обмен был стартовым вопросом из конфига: с этого момента
        он живёт в истории обычным ходом и в промпт отдельно не подклеивается.
        """
        if not commit:
            return False

        answer = texts[-1] if texts else ""
        error = None
        if not answer and partial.strip():
            answer, error = partial, (failure or "ответ не дописан")
        if not answer.strip():
            return False

        # Флаг и обмен пишутся одной транзакцией: иначе процесс, умерший между
        # ними, оставил бы колонку с поднятым seed_used и без записанного хода —
        # и стартовый вопрос исчез бы из промпта, ни разу не прозвучав.
        with self.store.tx() if self.store is not None else contextlib.nullcontext():
            if seed and not self.seed_used:
                # Флаг — часть конфига сессии: после перезапуска колонка
                # не должна заново считать стартовый вопрос незаданным.
                self.seed_used = True
                self.save_config()
            self.remember_exchange(user_text, answer, error=error)
        return True
