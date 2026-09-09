"""Реестр сессий: живые агенты в памяти, все сессии — в базе.

Ответ на критерий Дня 6 — «моментально заспавнить 100 агентов с разными
конфигами» — здесь: `create_many` кладёт в обычный словарь сто объектов
`Agent`. Ни потоков, ни подпроцессов, ни сети: спавн не делает ни одного
вызова к модели и поэтому бесплатен и мгновенен.

С Дня 7 у реестра два слоя. В памяти живут те агенты, с которыми говорят
прямо сейчас; в базе (`app/store.py`) лежат все сессии, включая те, что
пережили перезапуск. Отсюда главное различие дня:

* **вытеснение по потолку — это выгрузка**: агент уходит из памяти, строка
  в базе остаётся, и `require()` поднимет сессию обратно с её историей;
* **удаление — это удаление**: `kill()` стирает и из памяти, и из базы,
  каскадом по детям в обоих слоях.

Потолок на число живых агентов обязателен и после появления базы: процесс
стенда живёт часами, каждый `/прогон` спавнит новый набор, и без вытеснения
реестр течёт. Поднятая из базы сессия честно встаёт в очередь на вытеснение
наравне с остальными.
"""

from __future__ import annotations

import os
from dataclasses import fields
from typing import Iterable

from .agent import Agent, reserve_ids
from .schema import AgentSpec
from .store import Store, shared_store

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

    def __init__(self, max_agents: int | None = None, store: Store | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self.max_agents = max_agents if max_agents is not None else _max_agents()
        self.evicted = 0
        """Сколько агентов выгружено из памяти за жизнь процесса — видно в /api/agents.

        Именно выгружено, а не удалено: сессии этих агентов в базе остались."""

        self.store = shared_store() if store is None else store
        # Счётчик id живёт в процессе и после перезапуска начинается с нуля.
        # Сдвигаем его за самый большой id из базы: иначе свежий агент получил
        # бы id уже сохранённой сессии и унаследовал бы её историю.
        reserve_ids(self.store.max_agent_seq())

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
        agent = Agent(spec, parent_id=parent_id, context_length=context_length, store=self.store)
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
        # Сто сессий — одна транзакция, а не сто: спавн обязан остаться
        # мгновенным и после появления базы.
        with self.store.bulk():
            for spec in specs:
                agent = Agent(
                    spec,
                    parent_id=parent_id,
                    context_length=(context_lengths or {}).get(spec.model),
                    store=self.store,
                )
                self._agents[agent.id] = agent
                agents.append(agent)
        return agents

    # --- восстановление из базы ----------------------------------------------

    def load(self, session_id: str) -> Agent | None:
        """Поднимает сохранённую сессию в память: конфиг и история — из базы.

        Живой агент возвращается как есть: поднимать поверх него вторую копию
        значило бы раздвоить историю одного диалога.
        """
        live = self._agents.get(session_id)
        if live is not None:
            return live
        saved = self.store.load_session(session_id)
        if saved is None:
            return None
        # Место освобождаем до создания: поднятая сессия встаёт в общую очередь
        # на вытеснение, а не живёт сверх потолка.
        self._make_room(1)
        spec = _spec_from_config(saved["config"])
        agent = Agent(
            spec,
            agent_id=saved["id"],
            parent_id=saved["parent_id"],
            store=self.store,
        )
        self._agents[agent.id] = agent
        return agent

    def sessions(self, *, limit: int = 500) -> list[dict]:
        """Все сохранённые сессии, а не только живые в процессе."""
        live = set(self._agents)
        rows = self.store.list_sessions(limit=limit)
        for row in rows:
            row["live"] = row["id"] in live
        return rows

    # --- чтение --------------------------------------------------------------

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    def require(self, agent_id: str) -> Agent:
        """Агент по id: живой из памяти, иначе поднятый из базы.

        Вытесненная сессия отсюда возвращается как ни в чём не бывало —
        в этом и смысл базы. Нет её и в базе — значит, её удалили.
        """
        agent = self.load(agent_id)
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
        """Удаляет агента и всех его детей — из памяти и из базы.

        Каскад идёт по обоим слоям: в памяти по `parent_id` живых агентов,
        в базе — по `parent_id` строк. Ребёнок, вытесненный из памяти раньше
        родителя, иначе остался бы в базе сиротой навсегда.
        """
        known = agent_id in self._agents or self.store.load_session(agent_id) is not None
        if not known:
            return []
        killed = self._unload(agent_id)
        removed = self.store.delete_session(agent_id)
        for session_id in removed:
            if session_id not in killed:
                killed.append(session_id)
        return killed

    def _unload(self, agent_id: str) -> list[str]:
        """Убирает агента и его живых детей из памяти. Базу не трогает.

        Это и есть вытеснение: сессия остаётся сохранённой и поднимется
        обратно при первом же обращении.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            return []
        unloaded = [agent_id]
        for child in self.children(agent_id):
            unloaded.extend(self._unload(child.id))
        agent.cancel()
        self._agents.pop(agent_id, None)
        return unloaded

    def kill_children(self, parent_id: str) -> list[str]:
        """Удаляет набор субагентов родителя. «Старт» зовёт это перед новым набором."""
        killed: list[str] = []
        children = {c.id for c in self.children(parent_id)} | set(self.store.children(parent_id))
        for child_id in sorted(children):
            killed.extend(self.kill(child_id))
        return killed

    def kill_all(self, *, purge: bool = True) -> list[str]:
        """Гасит всех живых. purge — заодно стереть базу (нужно только проверкам)."""
        killed = list(self._agents)
        for agent in self._agents.values():
            agent.cancel()
        self._agents.clear()
        if purge:
            self.store.clear()
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
        """Освобождает место под `need` новых агентов, выгружая самых старых простаивающих.

        Вытеснение каскадное, как и `kill`: субагенты прогона уходят вместе
        с родителем. Иначе после вытеснения родителя дети остались бы в реестре
        с `parent_id` в никуда — их не найти по родителю и не убить каскадом,
        то есть потолок от них уже не защищает.

        Вытеснение не удаляет сессию: строка в базе остаётся, и `require()`
        поднимет разговор обратно с того же места. Потолок ограничивает
        память процесса, а не срок жизни диалога.

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
            # Именно выгрузка, а не удаление: строка сессии остаётся в базе,
            # и разговор можно продолжить, открыв её заново.
            unloaded = self._unload(agent.id)
            self.evicted += len(unloaded)
            overflow -= len(unloaded)


_SPEC_FIELDS = {f.name for f in fields(AgentSpec)}


def _spec_from_config(config: dict) -> AgentSpec:
    """Конфиг из базы обратно в `AgentSpec`.

    Лишние ключи отбрасываются молча: базу мог записать стенд другой версии,
    и падать на незнакомом поле — значит потерять весь сохранённый диалог.
    Обязательные поля подстраховываем дефолтом по той же причине.
    """
    fields_ = {k: v for k, v in (config or {}).items() if k in _SPEC_FIELDS}
    fields_.setdefault("label", "сессия")
    fields_.setdefault("model", "")
    fields_.setdefault("messages", [])
    return AgentSpec(**fields_)


REGISTRY = AgentRegistry()
"""Реестр процесса. Один инстанс — внутри него сколько угодно агентов."""
