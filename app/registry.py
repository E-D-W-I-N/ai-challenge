"""Реестр живых агентов процесса.

Ответ на критерий дня — «моментально заспавнить 100 агентов с разными
конфигами» — здесь: `create_many` кладёт в обычный словарь сто объектов
`Agent`. Ни потоков, ни подпроцессов, ни сети: спавн не делает ни одного
вызова к модели и поэтому бесплатен и мгновенен.

Потолок на число живых агентов обязателен: процесс стенда живёт часами,
каждый `/прогон` спавнит новый набор, и без вытеснения реестр течёт.
"""

from __future__ import annotations

import os
from typing import Iterable

from .agent import Agent
from .schema import AgentSpec

DEFAULT_MAX_AGENTS = 1000
"""Сколько агентов живёт в процессе одновременно, если AGENT_MAX_LIVE не задан.

С запасом больше ста: демонстрация спавна сотни не должна вытеснить агентов,
с которыми в этот момент идёт разговор.
"""


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
    """Словарь id → Agent плюс родительские связи. Один на процесс."""

    def __init__(self, max_agents: int | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self.max_agents = max_agents if max_agents is not None else _max_agents()
        self.evicted = 0
        """Сколько агентов вытеснено за жизнь процесса — видно в /api/agents."""

    def __len__(self) -> int:
        return len(self._agents)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._agents

    # --- создание ------------------------------------------------------------

    def create(
        self,
        spec: AgentSpec,
        *,
        parent_id: str | None = None,
        context_length: int | None = None,
    ) -> Agent:
        self._make_room(1)
        agent = Agent(spec, parent_id=parent_id, context_length=context_length)
        self._agents[agent.id] = agent
        return agent

    def create_many(
        self,
        specs: Iterable[AgentSpec],
        *,
        parent_id: str | None = None,
        context_lengths: dict[str, int] | None = None,
    ) -> list[Agent]:
        """Пачка агентов одним вызовом — сто конфигов, сто объектов, один процесс."""
        specs = list(specs)
        self._make_room(len(specs))
        agents = []
        for spec in specs:
            agent = Agent(
                spec,
                parent_id=parent_id,
                context_length=(context_lengths or {}).get(spec.model),
            )
            self._agents[agent.id] = agent
            agents.append(agent)
        return agents

    # --- чтение --------------------------------------------------------------

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> Agent:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise UnknownAgentError(agent_id)
        return agent

    def list(self, *, parent_id: str | None = None, only_children: bool = False) -> list[Agent]:
        """Все агенты, в порядке создания. only_children — только дети parent_id."""
        agents = sorted(self._agents.values(), key=lambda a: a.created_at)
        if only_children:
            return [a for a in agents if a.parent_id == parent_id]
        return agents

    def children(self, parent_id: str) -> list[Agent]:
        return self.list(parent_id=parent_id, only_children=True)

    # --- удаление ------------------------------------------------------------

    def kill(self, agent_id: str) -> list[str]:
        """Убивает агента и всех его детей. Возвращает id убитых."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return []
        killed = [agent_id]
        for child in self.children(agent_id):
            killed.extend(self.kill(child.id))
        agent.cancel()
        self._agents.pop(agent_id, None)
        return killed

    def kill_children(self, parent_id: str) -> list[str]:
        """Убивает набор субагентов родителя. «Старт» зовёт это перед новым набором."""
        killed: list[str] = []
        for child in self.children(parent_id):
            killed.extend(self.kill(child.id))
        return killed

    def kill_all(self) -> list[str]:
        killed = list(self._agents)
        for agent in self._agents.values():
            agent.cancel()
        self._agents.clear()
        return killed

    # --- вытеснение ----------------------------------------------------------

    def _evictable(self, agent: Agent) -> bool:
        """Можно ли вытеснить агента вместе со всем его поддеревом.

        Занятый агент не вытесняется никогда: у него идёт обмен, и его ответа
        кто-то прямо сейчас ждёт. Занятый **потомок** запрещает вытеснять
        и родителя: вытеснение идёт каскадом, и иначе оно оборвало бы живой
        прогон, начатый из родительской сессии.
        """
        if agent.busy:
            return False
        return all(self._evictable(child) for child in self.children(agent.id))

    def _make_room(self, need: int) -> None:
        """Освобождает место под `need` новых агентов, вытесняя самых старых простаивающих.

        Вытеснение каскадное, как и `kill`: субагенты прогона уходят вместе
        с родителем. Иначе после вытеснения родителя дети остались бы в реестре
        с `parent_id` в никуда — их не найти по родителю и не убить каскадом,
        то есть потолок от них уже не защищает.

        Если простаивающих не хватило — новые агенты всё равно создаются:
        отказать в спавне хуже, чем на время превысить потолок, а следующий
        спавн доберёт освободившихся.
        """
        overflow = len(self._agents) + need - self.max_agents
        if overflow <= 0:
            return
        # Сначала листья: у поддерева младший потомок и есть самый старый
        # кандидат, а родителя каскад заберёт вместе с ним.
        idle = sorted(
            (a for a in self._agents.values() if self._evictable(a)),
            key=lambda a: a.last_used_at,
        )
        for agent in idle:
            if overflow <= 0:
                break
            if agent.id not in self._agents:
                continue  # уже ушёл каскадом вместе с родителем
            killed = self.kill(agent.id)
            self.evicted += len(killed)
            overflow -= len(killed)


REGISTRY = AgentRegistry()
"""Реестр процесса. Один инстанс — внутри него сколько угодно агентов."""
