"""Агент — отдельная сущность: конфиг, память и один метод обмена.

До Дня 6 логика вызова была размазана: ленту диалога хранил браузер и слал её
целиком в каждый запрос, а сервер не знал ни про диалог, ни про то, кто
спрашивает. Здесь она собрана в один класс.

`Agent` принимает **текст пользователя**, а не готовую ленту: сам склеивает
системный промпт, стартовые сообщения, хвост истории и новый вопрос, зовёт
`stream_completion` и дописывает ответ себе в историю. Наружу отдаёт поток
событий — из него и SSE стенда, и вывод CLI.

Агент ничего не знает ни про FastAPI, ни про SSE, ни про реестр: сто агентов —
это сто объектов в одном процессе, а не сто процессов.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from dataclasses import dataclass, field, replace
from typing import AsyncIterator

from .llm import MissingKeyError, stream_completion
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
    ) -> None:
        # Копия конфига: spec может быть колонкой из SCENARIOS, а смена модели
        # на живом агенте не должна править сценарий дня для всех остальных.
        self.spec = replace(spec)
        self.id = agent_id or f"ag_{next(_ids):05d}"
        self.parent_id = parent_id
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Стартовые сообщения — в spec."""

        self.seed_messages: list[dict] = [dict(m) for m in (spec.messages or [])]
        """Стартовый промпт агента. У колонки с depends_on подменяется на старте
        прогона результатом подстановки — поэтому это поле, а не spec.messages."""

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

    def build_prompt(self, user_text: str | None = None) -> list[dict]:
        """Системный промпт + стартовые сообщения + окно истории + новый вопрос.

        `user_text=None` — прогон стартового промпта: с пустой историей это
        ровно `spec.messages`, то есть в точности то, что уходило в модель
        до появления агентов.
        """
        messages: list[dict] = []
        has_system = any(m.get("role") == "system" for m in self.seed_messages)
        if self.spec.system and not has_system:
            messages.append({"role": "system", "content": self.spec.system})
        messages.extend(dict(m) for m in self.seed_messages)
        messages.extend(self.window())
        if user_text is not None:
            messages.append({"role": "user", "content": user_text})
        return messages

    # --- история -------------------------------------------------------------

    def remember(self, role: str, content: str, error: str | None = None) -> None:
        self.history.append(Turn(role=role, content=content, error=error))
        self._trim()

    def _trim(self) -> None:
        if len(self.history) > MAX_STORED_MESSAGES:
            del self.history[: len(self.history) - MAX_STORED_MESSAGES]

    def forget(self) -> None:
        self.history.clear()

    def transcript(self) -> list[dict]:
        """Стартовый промпт и всё, что наговорили после него, — одним списком."""
        prompt = self.build_prompt()
        seed_size = len(prompt) - len(self.window())
        seed = [
            {"role": m.get("role", "?"), "content": m.get("content", ""), "seed": True}
            for m in prompt[:seed_size]
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
            "history_len": len(self.history),
            "busy": self.busy,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }
        if with_transcript:
            data["seed_messages"] = [dict(m) for m in self.seed_messages]
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
            total = max(1, int(self.spec.repeats or 1))

            yield {
                "type": "start",
                "resolved_messages": prompt,
                "repeats": total,
                "question": user_text,
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
                    self._commit(user_text, texts, partial or text, failure, commit)
                    return
                except asyncio.CancelledError:
                    # Клиент ушёл: частичный ответ всё равно записываем — он уже
                    # оплачен, а следующий вопрос должен видеть, чем кончилось.
                    self._commit(user_text, texts, partial or text, "вызов прерван", commit)
                    raise
                except Exception as exc:  # noqa: BLE001 — падает обмен, процесс живёт
                    failure = f"{type(exc).__name__}: {exc}"
                    yield {"type": "error", "message": failure, "metrics": None}
                    self._commit(user_text, texts, partial or text, failure, commit)
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

            committed = self._commit(user_text, texts, partial, failure, commit)

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
            if not committed and user_text is not None:
                # Обмена не было: вопрос нельзя оставлять в ленте клиента —
                # он вернётся в поле ввода и его можно будет повторить.
                done["question"] = user_text
            yield done

    def _commit(
        self,
        user_text: str | None,
        texts: list[str],
        partial: str,
        failure: str | None,
        commit: bool,
    ) -> bool:
        """Пишет обмен в историю. Возвращает False, если писать было нечего."""
        if not commit:
            return False

        answer = texts[-1] if texts else ""
        error = None
        if not answer and partial.strip():
            answer, error = partial, (failure or "ответ не дописан")
        if not answer.strip():
            return False

        if user_text is not None:
            self.remember("user", user_text)
        self.remember("assistant", answer, error=error)
        return True
