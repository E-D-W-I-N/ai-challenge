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
* **удаление — это удаление**: `kill()` стирает и из памяти, и из базы.

`restore_all()` на старте процесса поднимает сохранённые сессии обратно
в память — именно поэтому список слева после перезапуска выглядит так же,
как до него, и переписка в нём на месте.

Потолок на число живых агентов обязателен и после появления базы: процесс
стенда живёт часами, новые чаты копятся, и без вытеснения реестр течёт.
"""

from __future__ import annotations

import os
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
    """Словарь id → Agent поверх хранилища сессий. Один на процесс."""

    def __init__(self, max_agents: int | None = None, store: Store | None = None) -> None:
        self._agents: dict[str, Agent] = {}
        self.max_agents = max_agents if max_agents is not None else _max_agents()
        self.evicted = 0
        """Сколько агентов выгружено из памяти за жизнь процесса.

        Именно выгружено, а не удалено: их сессии в базе остались."""

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

    def create(self, spec: AgentSpec, *, context_length: int | None = None) -> Agent:
        self._make_room(1)
        agent = Agent(spec, context_length=context_length, store=self.store)
        self._agents[agent.id] = agent
        return agent

    def create_many(
        self, specs: Iterable[AgentSpec], *, context_lengths: dict[str, int] | None = None
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
        agent = Agent(_placeholder(saved), agent_id=saved["id"], store=self.store)
        self._agents[agent.id] = agent
        return agent

    def restore_all(self) -> list[Agent]:
        """Поднимает сохранённые сессии в память — на старте процесса.

        Без этого список слева после перезапуска был бы пуст: он строится по
        живым агентам. Порядок сохраняется через `created_at`, поэтому и группы
        дней, и чаты пользователя встают на прежние места. Свежие сессии идут
        первыми: если сохранённого больше потолка, в памяти окажутся те, с
        которыми говорили недавно, а остальные поднимутся при обращении.
        """
        rows = self.store.list_sessions(limit=self.max_agents)
        return [agent for row in rows if (agent := self.load(row["id"])) is not None]

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

        Выгруженная сессия отсюда возвращается как ни в чём не бывало — в этом
        и смысл базы. Нет её и в базе — значит, её удалили.
        """
        agent = self.load(agent_id)
        if agent is None:
            raise UnknownAgentError(agent_id)
        return agent

    def list(self) -> list[Agent]:
        """Все агенты, в порядке создания."""
        return sorted(self._agents.values(), key=lambda a: a.created_at)

    # --- удаление ------------------------------------------------------------

    def kill(self, agent_id: str) -> bool:
        """Удаляет сессию из памяти и из базы. False — её нигде нет."""
        unloaded = self._unload(agent_id)
        removed = self.store.delete_session(agent_id)
        return unloaded or removed

    def _unload(self, agent_id: str) -> bool:
        """Убирает агента из памяти и гасит его генерацию. Базу не трогает.

        Это и есть вытеснение: сессия остаётся сохранённой и поднимется
        обратно при первом же обращении.
        """
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return False
        agent.cancel()
        # Сессией владеет тот объект, что лежит в реестре. Выгруженный больше
        # не владелец: иначе придержанная кем-то ссылка пережила бы вытеснение,
        # обращение подняло бы из базы второй объект той же сессии,
        # и запись первого затёрла бы реплики второго.
        agent.detach()
        return True

    def kill_all(self, *, purge: bool = True) -> list[str]:
        """Гасит всех живых. purge — заодно стереть базу (нужно только проверкам)."""
        killed = list(self._agents)
        for agent in self._agents.values():
            agent.cancel()
            agent.detach()
        self._agents.clear()
        if purge:
            self.store.clear()
        return killed

    # --- вытеснение ----------------------------------------------------------

    def _make_room(self, need: int) -> None:
        """Освобождает место под `need` новых агентов, вытесняя самых старых простаивающих.

        Занятый агент не вытесняется никогда: у него идёт обмен, и его ответа
        кто-то прямо сейчас ждёт.

        Вытеснение не удаляет сессию: строка в базе остаётся, и `require()`
        поднимет разговор с того же места. Потолок ограничивает память
        процесса, а не срок жизни диалога.

        Если простаивающих не хватило — новые агенты всё равно создаются:
        отказать в спавне хуже, чем на время превысить потолок, а следующий
        спавн доберёт освободившихся.
        """
        overflow = len(self._agents) + need - self.max_agents
        if overflow <= 0:
            return
        idle = sorted(
            (a for a in self._agents.values() if not a.busy), key=lambda a: a.last_used_at
        )
        for agent in idle[:overflow]:
            # Именно выгрузка, а не удаление: строка сессии остаётся в базе,
            # и разговор можно продолжить, открыв его заново.
            if self._unload(agent.id):
                self.evicted += 1


def _placeholder(saved: dict) -> AgentSpec:
    """Минимальный конфиг под восстановление: настоящий приедет из базы.

    `Agent` в конструкторе перечитывает конфиг сохранённой сессии сам, но
    что-то передать ему надо: датакласс требует модель и имя.
    """
    config = saved.get("config") or {}
    return AgentSpec(
        label=saved.get("label") or "Чат",
        model=str(config.get("model") or "openai/gpt-4o-mini"),
    )


REGISTRY = AgentRegistry()
"""Реестр процесса. Один инстанс — внутри него сколько угодно агентов."""
