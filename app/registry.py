"""Реестр чатов: живые агенты в памяти, все чаты — в базе.

`create_many` кладёт в обычный словарь сто объектов `Agent`: ни потоков,
ни подпроцессов, ни сети — спавн бесплатен и мгновенен.

Слоя два, и различие между ними — главное в дне:

* **вытеснение по потолку — это выгрузка**: агент уходит из памяти, строка
  в базе остаётся, и `require()` поднимет чат обратно с его историей;
* **удаление — это удаление**: `kill()` стирает из обоих слоёв, и удалённый
  чат не возвращается на следующем запуске.

Список слева строится **по базе**: иначе чаты сверх потолка реестра исчезли
бы из него, хотя лежат в базе целыми, — а список единственный путь к диалогу.
"""

from __future__ import annotations

import os
from typing import Iterable

from .agent import Agent, spec_as_dict, spec_from_config
from .schema import AgentSpec
from .store import Store, shared_store

DEFAULT_MAX_AGENTS = 1000
"""Потолок живых, если AGENT_MAX_LIVE не задан. С запасом больше ста: спавн
сотни не должен вытеснить чат, в котором прямо сейчас говорят."""


def _max_agents() -> int:
    raw = os.environ.get("AGENT_MAX_LIVE", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGENTS
    return max(1, value)


class UnknownAgentError(KeyError):
    pass


class AgentRegistry:
    """Словарь id → Agent поверх хранилища чатов. Один на процесс."""

    def __init__(self, max_agents: int | None = None, store: Store | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self.max_agents = max_agents if max_agents is not None else _max_agents()
        self.evicted = 0
        """Сколько чатов выгружено из памяти за жизнь процесса. Именно
        выгружено, а не удалено: в базе они остались."""

        self.store = shared_store() if store is None else store

    def __len__(self) -> int:
        return len(self._agents)

    def create(self, spec: AgentSpec) -> Agent:
        return self.create_many([spec])[0]

    def create_many(
        self, specs: Iterable[AgentSpec], *, context_lengths: dict[str, int] | None = None
    ) -> list[Agent]:
        """Пачка агентов одним вызовом — сто конфигов, сто объектов, один процесс."""
        specs = list(specs)
        self._make_room(len(specs))
        agents = []
        # Сто чатов — одна транзакция, а не сто: спавн обязан остаться
        # мгновенным и после появления базы.
        with self.store.tx():
            for spec in specs:
                agent = Agent(
                    spec,
                    context_length=(context_lengths or {}).get(spec.model),
                    store=self.store,
                )
                self._agents[agent.id] = agent
                agents.append(agent)
        return agents


    def load(self, session_id: str) -> Agent | None:
        """Поднимает сохранённый чат в память. Живой возвращается как есть:
        вторая копия раздвоила бы историю одного диалога."""
        live = self._agents.get(session_id)
        if live is not None:
            return live
        saved = self.store.load_session(session_id)
        if saved is None:
            return None
        # Место освобождаем до создания: поднятый чат встаёт в общую очередь
        # на вытеснение.
        self._make_room(1)
        agent = Agent(_spec_from_row(saved), agent_id=saved["id"], store=self.store)
        self._agents[agent.id] = agent
        return agent

    def sessions(self) -> list[dict]:
        """Все сохранённые чаты, а не только живые в процессе."""
        live = set(self._agents)
        rows = self.store.list_sessions()
        for row in rows:
            row["live"] = row["id"] in live
        return rows

    def catalogue(self) -> list[dict]:
        """Список слева: каждый сохранённый чат одной записью, в порядке заведения.

        Живой описывает себя сам — у него точные `busy` и длина истории;
        выгруженный описывается по строке из базы тем же форматом. Потолка
        у списка нет: обрезка резала бы по времени последней записи, то есть
        по чатам, с которыми ещё не говорили.
        """
        entries = []
        for row in self.store.list_sessions():
            live = self._agents.get(row["id"])
            if live is not None:
                entries.append(live.as_dict())
                continue
            entries.append(
                spec_as_dict(
                    _spec_from_row(row),
                    agent_id=row["id"],
                    history_len=row["history_len"],
                    created_at=row["created_at"],
                    last_used_at=row["updated_at"],
                )
            )
        entries.sort(key=lambda entry: entry["created_at"])
        return entries


    def require(self, agent_id: str) -> Agent:
        """Агент по id: живой из памяти, иначе поднятый из базы. Нет и там —
        значит, чат удалили."""
        agent = self.load(agent_id)
        if agent is None:
            raise UnknownAgentError(agent_id)
        return agent

    def list(self) -> list[Agent]:
        """Все агенты, в порядке создания."""
        return sorted(self._agents.values(), key=lambda a: a.created_at)

    def kill(self, agent_id: str) -> bool:
        """Удаляет чат из обоих слоёв. False — его нигде нет.

        Именно из обоих: строка, оставшаяся в базе, вернула бы чат
        на следующем запуске.
        """
        unloaded = self._unload(agent_id)
        removed = self.store.delete_session(agent_id)
        return unloaded or removed

    def _unload(self, agent_id: str) -> bool:
        """Убирает агента из памяти и гасит его генерацию, базу не трогая.
        Это и есть вытеснение: чат поднимется обратно при первом обращении."""
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return False
        agent.cancel()
        # Сессией владеет тот объект, что лежит в реестре (см. Agent.detach).
        agent.detach()
        return True

    def _make_room(self, need: int) -> None:
        """Освобождает место, вытесняя самых старых простаивающих.

        Занятый не вытесняется никогда: его ответа кто-то ждёт. Простаивающих
        не хватило — агенты всё равно создаются: отказать в спавне хуже, чем
        на время превысить потолок.
        """
        overflow = len(self._agents) + need - self.max_agents
        if overflow <= 0:
            return
        idle = sorted(
            (a for a in self._agents.values() if not a.busy), key=lambda a: a.last_used_at
        )
        for agent in idle[:overflow]:
            if self._unload(agent.id):
                self.evicted += 1


_UNKNOWN_MODEL = "openai/gpt-4o-mini"
"""Чем заменить модель, если в сохранённом конфиге её нет: показать такой чат
в списке всё равно надо — в нём лежит переписка."""


def _spec_from_row(saved: dict) -> AgentSpec:
    """Конфиг сохранённого чата из строки базы — тем же разбором, которым
    агент восстанавливает себя в конструкторе."""
    fallback = AgentSpec(label=saved.get("label") or "Чат", model=_UNKNOWN_MODEL)
    return spec_from_config(saved.get("config") or {}, fallback=fallback)


REGISTRY = AgentRegistry()
