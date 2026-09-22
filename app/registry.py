"""Реестр чатов: словарь id → Agent поверх хранилища, один на процесс.

Слоя два: вытеснение по потолку — выгрузка (строка в базе остаётся,
`require()` поднимет чат обратно), `kill()` — удаление из обоих слоёв.
Список слева строится по базе: иначе чаты сверх потолка исчезли бы из него,
хотя лежат в базе целыми.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Iterable

from .agent import Agent, reserve_ids, spec_as_dict, spec_from_config
from .schema import AgentSpec
from .store import Store, shared_store

DEFAULT_MAX_AGENTS = 1000
"""Потолок живых, если AGENT_MAX_LIVE не задан. С запасом больше ста: спавн
сотни не должен вытеснить чат, в котором говорят."""


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
    def __init__(self, max_agents: int | None = None, store: Store | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self.max_agents = max_agents if max_agents is not None else _max_agents()
        self.evicted = 0
        """Выгружено из памяти за жизнь процесса — не удалено."""

        self.store = shared_store() if store is None else store
        # Иначе свежий агент получил бы id уже сохранённого чата
        # и унаследовал бы его историю.
        reserve_ids(self.store.max_agent_seq())

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
        # Сто чатов — одна транзакция, а не сто: спавн обязан остаться мгновенным.
        with self.store.tx():
            agents = [
                Agent(
                    spec,
                    context_length=(context_lengths or {}).get(spec.model),
                    store=self.store,
                )
                for spec in specs
            ]
        for agent in agents:
            self._agents[agent.id] = agent
        return agents

    def fork(self, parent: Agent, at: int, *, label: str) -> Agent:
        """Заводит ветку: отдельный чат с копией начала разговора и конфига.

        Копия читается до создания: `create_many` может вытеснить самого
        родителя, а вытесненный теряет право писать в свою сессию.
        Всё одной транзакцией — оборвись запись, в списке повис бы обрубок.
        """
        carried = parent.carry_off(at)
        with self.store.tx():
            agent = self.create_many(
                # Только имя своё; вглубь конфиг копирует конструктор агента.
                [replace(parent.spec, label=label)],
                # Длина контекста — у родителя, а не из каталога: модель та же,
                # а каталог сетевой запрос, и ветвление не должно его ждать.
                context_lengths={parent.spec.model: parent.context_length},
            )[0]
            agent.take_branch(carried, parent_id=parent.id, forked_at=at)
        return agent

    def load(self, session_id: str) -> Agent | None:
        """Поднимает сохранённый чат в память. Живой возвращается как есть:
        вторая копия раздвоила бы историю одного диалога."""
        live = self._agents.get(session_id)
        if live is not None:
            return live
        saved = self.store.load_session(session_id)
        if saved is None:
            return None
        # Поднятый чат встаёт в общую очередь на вытеснение.
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

        Живой описывает себя сам, выгруженный — по строке из базы тем же
        форматом. Потолка у списка нет: обрезка резала бы по чатам, с которыми
        ещё не говорили.
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
                    # Родство приезжает тем же запросом, что и чат: пометка
                    # ветки не должна зависеть от того, поднят он или нет.
                    branch=row.get("branch"),
                    # План остаётся `None` намеренно: второго запроса в базу
                    # на каждый чат списка нет. Ключ в теле есть у обоих
                    # случаев (`spec_as_dict`), план показывает открытый чат.
                )
            )
        entries.sort(key=lambda entry: entry["created_at"])
        return entries

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> Agent:
        """Живой из памяти, иначе поднятый из базы. Нет и там — чат удалили."""
        agent = self.load(agent_id)
        if agent is None:
            raise UnknownAgentError(agent_id)
        return agent

    def list(self) -> list[Agent]:
        return sorted(self._agents.values(), key=lambda a: a.created_at)

    def kill(self, agent_id: str) -> bool:
        """Удаляет чат из обоих слоёв. False — его нигде нет. Строка,
        оставшаяся в базе, вернула бы чат на следующем запуске."""
        unloaded = self._unload(agent_id)
        removed = self.store.delete_session(agent_id)
        return unloaded or removed

    def _unload(self, agent_id: str) -> bool:
        """Вытеснение: агент уходит из памяти, база цела, чат поднимется
        обратно при первом обращении."""
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return False
        agent.cancel()
        agent.detach()  # сессией владеет тот объект, что лежит в реестре
        return True

    def kill_all(self) -> list[str]:
        """Гасит всех живых и стирает базу. Нужно только проверкам."""
        killed = list(self._agents)
        for agent in self._agents.values():
            agent.cancel()
            agent.detach()
        self._agents.clear()
        self.store.clear()
        return killed

    def _make_room(self, need: int) -> None:
        """Вытесняет самых старых простаивающих. Занятый не вытесняется
        никогда: его ответа ждут. Не хватило простаивающих — агенты всё равно
        создаются: отказать в спавне хуже, чем превысить потолок."""
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
"""Чем заменить модель, которой нет в сохранённом конфиге: показать такой чат
в списке всё равно надо — в нём лежит переписка."""


def _spec_from_row(saved: dict) -> AgentSpec:
    """Тем же разбором, которым агент восстанавливает себя в конструкторе."""
    fallback = AgentSpec(label=saved.get("label") or "Чат", model=_UNKNOWN_MODEL)
    return spec_from_config(saved.get("config") or {}, fallback=fallback)


REGISTRY = AgentRegistry()
