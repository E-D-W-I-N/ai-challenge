"""Агент — отдельная сущность: конфиг, память и один метод обмена.

`Agent` принимает **текст пользователя**, а не готовую ленту: сам склеивает
системный промпт, всю историю и новый вопрос, зовёт `stream_completion`
и дописывает ответ себе в историю. Наружу отдаёт поток событий — из него
и SSE веб-клиента, и вывод CLI.

Агент ничего не знает ни про FastAPI, ни про SSE, ни про реестр: сто агентов —
это сто объектов в одном процессе, а не сто процессов.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import itertools
import time
from dataclasses import asdict, dataclass, replace
from typing import AsyncIterator

from .llm import SAMPLING_FIELDS, MissingKeyError, stream_completion
from .schema import AgentSpec

_ids = itertools.count(1)


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

    def as_message(self) -> dict:
        """Реплика в том виде, в каком она уходит в модель: в контекст
        возвращается ответ, а не путь к нему."""
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
    """Один агент: конфиг + история + один метод обмена. Инстанцируется дёшево
    и много: ни сети, ни потоков, ни подпроцессов в конструкторе."""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        agent_id: str | None = None,
        context_length: int | None = None,
    ) -> None:
        # Свой экземпляр конфига каждому агенту: один и тот же spec может
        # поднять сотню, и правка у одного не должна задеть остальных.
        self.spec = copy_spec(spec)
        self.id = agent_id or f"ag_{next(_ids):05d}"
        self.created_at = time.time()
        self.last_used_at = self.created_at
        self.context_length = context_length

        self.history: list[Turn] = []
        """Только то, что наговорили в диалоге. Системный промпт — в spec."""

        self._lock = asyncio.Lock()
        self._cancel = asyncio.Event()
        self._reserved = False

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

    def build_prompt(self, user_text: str, *, spec: AgentSpec | None = None) -> list[dict]:
        """Системный промпт + вся история + вопрос этого хода.

        История уезжает целиком: чат помнит начало разговора, сколько бы он
        ни длился. Конфиг читается каждый раз, поэтому правка в панели видна
        со следующего сообщения; `spec` передаёт обмен — он собирает промпт
        и тело запроса из одного слепка.
        """
        spec = spec if spec is not None else self.spec

        messages: list[dict] = []
        if spec.system:
            messages.append({"role": "system", "content": spec.system})
        messages.extend(turn.as_message() for turn in self.history)
        messages.append({"role": "user", "content": user_text})
        return messages

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

    def forget(self) -> None:
        self.history.clear()

    def transcript(self) -> list[dict]:
        """Ровно реплики диалога. Системного промпта здесь нет: он конфиг,
        а не реплика, и виден в панели полем `system`."""
        return [turn.as_dict() for turn in self.history]

    def as_dict(self, *, with_transcript: bool = False) -> dict:
        data = {
            "id": self.id,
            "label": self.spec.label,
            "model": self.spec.model,
            "stop": self.spec.stop,
            "response_format": self.spec.response_format,
            "extra_body": self.spec.extra_body,
            "system": self.spec.system,
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

        self.remember("user", user_text)
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
        `restore` кладёт обратно и вопрос, и прежний ответ."""
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
        return True
