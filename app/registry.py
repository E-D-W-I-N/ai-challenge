"""Реестр живых агентов процесса: словарь id → Agent, один на процесс.

`create_many` кладёт в обычный словарь сто объектов `Agent`: ни потоков,
ни подпроцессов, ни сети — спавн не делает ни одного вызова к модели.

Потолок на число живых обязателен: процесс живёт часами, новые чаты копятся,
и без вытеснения реестр течёт.
"""

from __future__ import annotations

import os
from typing import Iterable

from .agent import Agent
from .schema import AgentSpec

DEFAULT_MAX_AGENTS = 1000
"""Потолок живых, если AGENT_MAX_LIVE не задан. С запасом больше ста: спавн
сотни не должен вытеснить агентов, с которыми идёт разговор."""


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
    def __init__(self, max_agents: int | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self.max_agents = max_agents if max_agents is not None else _max_agents()
        self.evicted = 0
        """Сколько агентов вытеснено за жизнь процесса — видно в /api/agents."""

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
        agents = [Agent(spec, context_length=(context_lengths or {}).get(spec.model)) for spec in specs]
        for agent in agents:
            self._agents[agent.id] = agent
        return agents

    def require(self, agent_id: str) -> Agent:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise UnknownAgentError(agent_id)
        return agent

    def list(self) -> list[Agent]:
        """Все агенты, в порядке создания."""
        return sorted(self._agents.values(), key=lambda a: a.created_at)

    def kill(self, agent_id: str) -> bool:
        """Убирает агента из реестра и гасит его генерацию. False — его уже нет."""
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return False
        agent.cancel()
        return True

    def _make_room(self, need: int) -> None:
        """Освобождает место под `need` новых, вытесняя самых старых простаивающих.

        Занятый агент не вытесняется никогда: у него идёт обмен, и его ответа
        кто-то прямо сейчас ждёт. Если простаивающих не хватило — новые агенты
        всё равно создаются: отказать в спавне хуже, чем на время превысить
        потолок, а следующий спавн доберёт освободившихся.
        """
        overflow = len(self._agents) + need - self.max_agents
        if overflow <= 0:
            return
        idle = sorted(
            (a for a in self._agents.values() if not a.busy), key=lambda a: a.last_used_at
        )
        for agent in idle[:overflow]:
            if self.kill(agent.id):
                self.evicted += 1


REGISTRY = AgentRegistry()
