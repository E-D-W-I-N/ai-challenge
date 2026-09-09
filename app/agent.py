"""Агент — отдельная сущность: конфиг, память и один метод обмена.

До Дня 6 логика вызова была размазана: ленту диалога хранил браузер и слал её
целиком в каждый запрос, а сервер не знал ни про диалог, ни про то, кто
спрашивает. Здесь она собрана в один класс.

`Agent` принимает **текст пользователя**, а не готовую ленту: сам склеивает
системный промпт, стартовые сообщения, хвост истории и новый вопрос, зовёт
`stream_completion` и дописывает ответ себе в историю. Наружу отдаёт поток
событий — из него и SSE веб-клиента, и вывод CLI.

Агент ничего не знает ни про FastAPI, ни про SSE, ни про реестр: сто агентов —
это сто объектов в одном процессе, а не сто процессов.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import time
from dataclasses import dataclass, field, replace
from typing import AsyncIterator

from .llm import SAMPLING_FIELDS, MissingKeyError, stream_completion
from .schema import AgentSpec

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

_ids = itertools.count(1)


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

    reasoning: str = ""
    """Рассуждение модели, если она его прислала. В модель обратно не уходит.

    OpenRouter отдаёт его отдельным полем дельты, и в `content` оно не входит.
    Клиент рисует его свёрнутым блоком «Рассуждение» над ответом.
    """

    metrics: dict | None = None
    """Метрики этого ответа: по ним рисуются плитки внизу справа."""

    at: float = field(default_factory=time.time)

    def as_message(self) -> dict:
        """Реплика в том виде, в каком она уходит обратно в модель.

        Ни рассуждения, ни метрик здесь нет: в контекст возвращается ответ,
        а не то, как модель к нему шла.
        """
        return {"role": self.role, "content": self.content}

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "error": self.error,
            "reasoning": self.reasoning,
            "metrics": self.metrics,
            "at": self.at,
        }


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
    ) -> None:
        # Копия конфига, и вглубь тоже: один и тот же spec из day.py может
        # поднять несколько агентов, а `replace` копирует только верхний уровень —
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
        self.id = agent_id or f"ag_{next(_ids):05d}"
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Стартовые сообщения — в spec."""

        # Системный промпт живёт ровно в одном месте — `spec.system`, и читается
        # оттуда на каждом обращении. Если он приехал внутри `messages`, его
        # переносят сюда прямо здесь: иначе панель правила бы `spec.system`,
        # а в модель уезжала бы копия из заготовки, зафиксированная в момент
        # создания агента.
        seed = [dict(m) for m in (spec.messages or [])]
        carried = [m.get("content", "") for m in seed if m.get("role") == "system"]
        if carried and not self.spec.system:
            self.spec.system = "\n\n".join(carried)

        self.seed_messages: list[dict] = [m for m in seed if m.get("role") != "system"]
        """Заготовка диалога до первого вопроса — без системных сообщений.

        Отдельное поле, а не `spec.messages`, чтобы правка заготовки у одного
        агента не задела конфиг, из которого его подняли."""

        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._reserved = False

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

        Обстановка — всё, что не последняя реплика пользователя: она уезжает
        в модель всегда. Системных сообщений здесь уже нет — они переехали
        в `spec.system` при создании агента.

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
        """Первый вопрос из `messages`, если он там есть."""
        return self._seed_split()[1]

    def starting_prompt(self) -> list[dict]:
        """Стартовый промпт целиком — системная инструкция и заготовка.

        Не зависит от истории: с него начинается стенограмма, и он же виден
        в ленте до первого вопроса. Системный промпт берётся из конфига
        каждый раз, поэтому правка в панели видна сразу.
        """
        messages: list[dict] = []
        if self.spec.system:
            messages.append({"role": "system", "content": self.spec.system})
        messages.extend(dict(m) for m in self.seed_messages)
        return messages

    def build_prompt(self, user_text: str | None = None) -> list[dict]:
        """Обстановка + окно истории + вопрос этого хода.

        `user_text=None` — обмен стартовым промптом: с пустой историей это
        ровно `spec.messages`.

        С заданным `user_text` первый вопрос из `messages` в промпт больше
        не подклеивается: он либо приедет окном истории, либо забыт. Именно
        здесь `history_limit=0` и становится правдой — агент без памяти
        на втором вопросе не знает ни вопроса, ни своего ответа. Пока история
        пуста, вопрос всё же едет: он часть заготовки, и промпт обязан
        сходиться с тем, что показано в стенограмме.
        """
        setting, question = self._seed_split()
        messages: list[dict] = []
        if self.spec.system:
            messages.append({"role": "system", "content": self.spec.system})
        messages.extend(setting)

        if user_text is None:
            messages.extend(self.window())
            if question is not None:
                messages.append({"role": "user", "content": question})
            return messages

        if question is not None and not self.history:
            messages.append({"role": "user", "content": question})
        messages.extend(self.window())
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
    ) -> None:
        self.history.append(
            Turn(role=role, content=content, error=error, reasoning=reasoning, metrics=metrics)
        )
        self._trim()

    def _trim(self) -> None:
        if len(self.history) > MAX_STORED_MESSAGES:
            del self.history[: len(self.history) - MAX_STORED_MESSAGES]

    def forget(self) -> None:
        self.history.clear()

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
            "label": self.spec.label,
            "model": self.spec.model,
            "stop": self.spec.stop,
            "response_format": self.spec.response_format,
            "extra_body": self.spec.extra_body,
            "system": self.spec.system,
            "draft": self.spec.draft,
            "history_limit": self.history_limit,
            "history_len": len(self.history),
            "busy": self.busy,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }
        # Параметры сэмплирования уходят наружу как есть, включая None:
        # панель справа отличает «не задано» от нуля, и ей нужно и то и другое.
        for name in SAMPLING_FIELDS:
            data[name] = getattr(self.spec, name)
        if with_transcript:
            data["seed_messages"] = self.starting_prompt()
            data["transcript"] = self.transcript()
        return data

    # --- обмен ---------------------------------------------------------------

    async def ask(self, user_text: str | None = None, *, commit: bool = True) -> AsyncIterator[dict]:
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

            prompt = self.build_prompt(user_text)
            # Вопрос коммитится в историю как обычный ход: агент с памятью
            # обязан помнить, на что он отвечал, а не только чем ответил.
            question = user_text if user_text is not None else self.seed_question

            yield {"type": "start", "resolved_messages": prompt, "question": question}

            text = ""
            reasoning = ""
            final_metrics: dict | None = None
            failure: str | None = None
            cancelled = False

            try:
                stream = stream_completion(
                    self.spec, prompt_override=prompt, context_length=self.context_length
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
                self._commit(question, text, "вызов прерван", commit, reasoning, final_metrics)
                raise
            except Exception as exc:  # noqa: BLE001 — падает обмен, процесс живёт
                failure = f"{type(exc).__name__}: {exc}"
                yield {"type": "error", "message": failure, "metrics": None}

            if cancelled and failure is None:
                failure = "генерация отменена"

            committed = self._commit(question, text, failure, commit, reasoning, final_metrics)

            done: dict = {
                "type": "done",
                "text": text,
                "reasoning": reasoning,
                "metrics": final_metrics,
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
        answer: str,
        failure: str | None,
        commit: bool,
        reasoning: str = "",
        metrics: dict | None = None,
    ) -> bool:
        """Пишет обмен в историю. Возвращает False, если писать было нечего."""
        if not commit or not answer.strip():
            return False

        if user_text is not None:
            self.remember("user", user_text)
        # Ответ, оборванный на середине, всё равно часть диалога — но помечен:
        # следующий вопрос должен видеть, что предыдущий ответ неполный.
        self.remember(
            "assistant", answer, error=failure, reasoning=reasoning, metrics=metrics
        )
        return True

    def take_last_exchange(self) -> Exchange | None:
        """Снимает с истории последнюю пару «вопрос — ответ» целиком.

        Нужна перегенерации: она должна **заменить** последний ответ, а не
        дописать второй, поэтому пара уходит из истории до вызова — модель
        обязана увидеть тот же контекст, что и в первый раз.

        Возвращает снятое целиком, а не только вопрос: если вызов не отдаст
        ни одного токена, возвращать в чат будет нечего, и пользователь
        потеряет и свой вопрос, и уже полученный ответ. `restore` кладёт
        снятое обратно.
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

        Новый обмен успел записаться — значит перегенерация удалась (или
        оборвалась с частичным ответом, что тоже записано), и возвращать
        старое поверх нельзя: в ленте оказалось бы два ответа на один вопрос.
        """
        if len(self.history) != exchange.depth:
            return False
        self.history.extend(exchange.turns)
        return True
