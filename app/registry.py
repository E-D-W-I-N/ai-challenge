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

Список слева строится **по базе**, а не по памяти: `catalogue()` отдаёт все
сохранённые сессии, подставляя живой объект там, где он есть. Иначе сессии
сверх потолка реестра просто исчезли бы из списка, хотя лежат в базе целыми, —
а список слева теперь единственный способ добраться до диалога. В память
сессия поднимается при открытии, `require()`.

Потолок на число живых агентов обязателен и после появления базы: процесс
стенда живёт часами, новые чаты копятся, и без вытеснения реестр течёт.
"""

from __future__ import annotations

import os
from typing import Iterable

from .agent import Agent, reserve_ids, spec_as_dict, spec_from_config
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
        agent = Agent(_spec_from_row(saved), agent_id=saved["id"], store=self.store)
        self._agents[agent.id] = agent
        return agent

    def sessions(self, *, limit: int = 1000) -> list[dict]:
        """Все сохранённые сессии, а не только живые в процессе."""
        live = set(self._agents)
        rows = self.store.list_sessions(limit=limit)
        for row in rows:
            row["live"] = row["id"] in live
        return rows

    def catalogue(self, *, limit: int = 1000) -> list[dict]:
        """Список слева: каждая сохранённая сессия одной записью, в порядке заведения.

        Живой агент описывает себя сам — у него точные `busy` и длина истории.
        Выгруженная сессия описывается по строке из базы тем же форматом:
        клиент не должен различать «поднято в память» и «лежит в базе», для
        него это один список, и открывается из него любая запись.
        """
        entries = []
        for row in self.store.list_sessions(limit=limit):
            live = self._agents.get(row["id"])
            if live is not None:
                entries.append(live.as_dict())
                continue
            spec = _spec_from_row(row)
            entries.append(
                spec_as_dict(
                    spec,
                    agent_id=row["id"],
                    history_len=row["history_len"],
                    created_at=row["created_at"],
                    last_used_at=row["updated_at"],
                )
            )
        entries.sort(key=lambda entry: entry["created_at"])
        return entries

    def origins(self) -> set[str]:
        """Какие конфиги ростера уже подняты — по всем сохранённым сессиям.

        Именно по базе и именно по `origin`, а не по имени: имя правится
        в панели справа, и переименованный агент дня перестал бы совпадать
        со своим конфигом. На следующем старте рядом с ним завёлся бы второй.
        """
        found = set()
        for row in self.store.list_sessions(limit=10_000):
            origin = (row["config"] or {}).get("origin")
            if origin:
                found.add(origin)
        return found

    def delete_where(self, keep_roster: bool = True) -> list[str]:
        """Стирает чаты пользователя — все, а не только поднятые в память.

        «Очистить все чаты» после перезапуска обязана дотянуться и до тех
        сессий, которых сейчас нет в памяти: в списке слева они видны, значит
        и уйти должны вместе с остальными.
        """
        killed = []
        for row in self.store.list_sessions(limit=10_000):
            is_roster = bool((row["config"] or {}).get("group"))
            if keep_roster and is_roster:
                continue
            if self.kill(row["id"]):
                killed.append(row["id"])
        return killed

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


def _spec_from_row(saved: dict) -> AgentSpec:
    """Конфиг сохранённой сессии из строки базы.

    Разбор конфига один на весь стенд — тот же, которым агент восстанавливает
    себя в конструкторе: незнакомые ключи отбрасываются молча, потому что базу
    мог записать стенд другой версии, и падать на чужом поле значит потерять
    весь список.
    """
    fallback = AgentSpec(label=saved.get("label") or "Чат", model=_UNKNOWN_MODEL)
    return spec_from_config(saved.get("config") or {}, fallback=fallback)


_UNKNOWN_MODEL = "openai/gpt-4o-mini"
"""Чем заменить модель, если в сохранённом конфиге её нет.

Строка сессии без модели — это или чужая версия схемы, или недописанная
строка. Показать такую сессию в списке всё равно надо: в ней лежит переписка.
"""


REGISTRY = AgentRegistry()
"""Реестр процесса. Один инстанс — внутри него сколько угодно агентов."""
