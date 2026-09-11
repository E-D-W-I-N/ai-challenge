"""Хранилище чатов и сообщений: SQLite из стандартной библиотеки.

Четыре свойства, каждое из которых стоит того, чтобы за ним следить:

* **`session_id` в первичном ключе сообщений.** Без него два чата, поднятые
  из базы, читали бы одни и те же строки, и список слева слился бы в один
  диалог. Это не соглашение, а ключ: строку нельзя записать, не сказав, чья она;
* **история пишется целиком и одной транзакцией.** Поэтому `seq` не получает
  дыр, а оборванная запись откатывается вся: вопроса без ответа не остаётся;
* **id выдаёт база, а не процесс.** Консоль запускают рядом с сервером, файл
  у них один, и счётчик в памяти выдал бы обоим `ag_00004` — второй стёр бы
  диалог первого (`save_history` начинается с `DELETE`);
* **`redact()` на всех колонках сразу.** Транзакция отдаёт не соединение,
  а обёртку, которая чистит строковые параметры любого запроса, — забыть
  про новую колонку нельзя, она чистится по построению.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from .config import ROOT, api_key

DEFAULT_DB_PATH = ROOT / "data" / "agents.db"
"""Куда пишется база, если AGENT_DB_PATH не задан. Каталог в .gitignore вместе
с `*.db`, `*.db-wal` и `*.db-shm`: в базе лежат тексты диалогов."""

MEMORY = ":memory:"
"""Особый путь sqlite: база живёт в памяти и перезапуска не переживает."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    config      TEXT NOT NULL DEFAULT '{}',
    context_length INTEGER,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    error       TEXT,
    metrics     TEXT,
    at          REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_ID_RE = re.compile(r"^ag_(\d+)$")

BUSY_TIMEOUT_MS = 5000
"""Сколько ждать освобождения базы. Пять секунд с запасом покрывают любую
запись сервера; дольше нельзя — ожидание блокировки замораживает цикл событий."""

_CLAIM_ATTEMPTS = 50
"""Сколько раз пробуем занять id. В норме хватает двух: первая ловит конфликт,
вторая берёт свободный номер. Полсотни — запас, а не рабочий режим."""


def db_path() -> Path | str:
    """Путь к базе. Через переменную окружения — иначе проверки писали бы
    в рабочий файл."""
    raw = os.environ.get("AGENT_DB_PATH", "").strip()
    if not raw:
        return DEFAULT_DB_PATH
    return MEMORY if raw == MEMORY else Path(raw)


def _dumps(value) -> str:
    # default=str: в extra_body приезжает что угодно из панели, и падение
    # сериализации не должно ронять обмен, который уже оплачен.
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: str, fallback):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


MIN_SECRET_LENGTH = 16
"""Короче этого значение ключом не считается и не вырезается.

Редакция работает подстрокой, а чистит **любой** строковый параметр — включая
`session_id` и роль реплики. С вырожденным ключом (буквально `1`) она изрезала
бы `ag_00001` в `ag_***0000***`. Настоящий ключ OpenRouter — 73 символа.
"""


def redact(value):
    """Вырезает ключ OpenRouter из всего, что уезжает в базу.

    В конфиг он не попадает по построению, но `extra_body` приходит от клиента,
    да и в реплику его можно вставить, перепутав окно. Репозиторий публичный:
    дешевле вырезать, чем потом отзывать ключ.
    """
    key = api_key()
    if not key or len(key) < MIN_SECRET_LENGTH:
        return value
    return value.replace(key, "***") if isinstance(value, str) else value


class StoreBusyError(RuntimeError):
    """База занята другим процессом дольше `busy_timeout`.

    Отдельный класс, а не голый `sqlite3.OperationalError`: наверху из него
    делают 503 с объяснением. Ситуация штатная, а не поломка.
    """


def _busy(exc: sqlite3.OperationalError, path) -> StoreBusyError | None:
    """Переводит «database is locked» в человеческий текст.

    None — ошибка не про блокировку: «no such table» это баг схемы, и
    подменять его успокаивающим текстом нельзя.
    """
    text = str(exc).lower()
    if "locked" not in text and "busy" not in text:
        return None
    return StoreBusyError(
        f"база {path} занята другим процессом дольше {BUSY_TIMEOUT_MS} мс. "
        "Так бывает, если рядом идёт длинная запись из второй копии сервера "
        "или из консоли: подождите пару секунд и повторите. Ничего не потеряно — "
        "незавершённая запись откатывается целиком."
    )


@contextlib.contextmanager
def _translating(path):
    """Переводит занятую базу в StoreBusyError, остальное пропускает как есть."""
    try:
        yield
    except sqlite3.OperationalError as exc:
        busy = _busy(exc, path)
        if busy is None:
            raise
        raise busy from exc


class _Writer:
    """Соединение, которое чистит строковые параметры любого запроса.

    Единственный способ записать — взять транзакцию, а она отдаёт эту обёртку.
    Поэтому обещание держится на всех колонках сразу, включая те, которых ещё
    нет: новую в обход `redact()` не добавить.
    """

    __slots__ = ("_conn", "_path")

    def __init__(self, conn: sqlite3.Connection, path) -> None:
        self._conn = conn
        self._path = path

    @staticmethod
    def _clean(params):
        if isinstance(params, dict):
            return {key: redact(value) for key, value in params.items()}
        return tuple(redact(value) for value in params)

    def execute(self, sql: str, params=()):
        with _translating(self._path):
            return self._conn.execute(sql, self._clean(params))

    def executemany(self, sql: str, rows):
        with _translating(self._path):
            return self._conn.executemany(sql, (self._clean(row) for row in rows))


class Store:
    """Файл базы плюс несколько запросов к нему. Один на процесс.

    Соединение одно и живёт до `close()`: открывать файл на каждую реплику —
    терять WAL-кеш и упираться в блокировки. Запросы идут под общим `RLock`:
    писать могут и цикл событий, и рабочий поток TestClient.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = db_path() if path is None else path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._depth = 0

    # --- открытие и закрытие -------------------------------------------------

    def init(self) -> "Store":
        """Создаёт каталог и таблицы. Вызывать можно сколько угодно раз."""
        if self._conn is not None:
            return self
        if self.path != MEMORY:
            path = Path(self.path)
            # Без mkdir sqlite падает «unable to open database file»
            # и сервер не поднимается вовсе.
            path.parent.mkdir(parents=True, exist_ok=True)
            target: str = str(path)
        else:
            target = MEMORY
        conn = sqlite3.connect(
            target,
            check_same_thread=False,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        conn.row_factory = sqlite3.Row
        # WAL: читатель не ждёт писателя. Побочный эффект — файлы -wal и -shm
        # рядом с базой, и .gitignore обязан ловить их тоже.
        if target != MEMORY:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        self._conn = conn
        return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.init()
        assert self._conn is not None
        return self._conn

    @contextlib.contextmanager
    def reading(self):
        """Соединение для чтения. Занятую базу переводит в StoreBusyError."""
        with self._lock, _translating(self.path):
            yield self.conn

    @contextlib.contextmanager
    def tx(self):
        """Одна транзакция. Вложенные коммитятся один раз, самым внешним:
        спавну пачки нужна одна транзакция на сто чатов, а не сто.

        Отдаёт не соединение, а `_Writer` — с `redact()` на любом параметре.
        """
        with self._lock:
            conn = self.conn
            outer = self._depth == 0
            if outer:
                # IMMEDIATE, а не голый BEGIN: отложенная транзакция,
                # начавшаяся с чтения, при попытке записи получила бы
                # SQLITE_BUSY без ретрая по busy_timeout. Берём блокировку
                # сразу — тогда второй писатель честно ждёт очереди.
                with _translating(self.path):
                    conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield _Writer(conn, self.path)
            except BaseException:
                self._depth -= 1
                if outer:
                    conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    conn.execute("COMMIT")

    # --- сессии --------------------------------------------------------------

    def save_session(
        self,
        session_id: str,
        *,
        label: str,
        config: dict,
        created_at: float,
        context_length: int | None = None,
    ) -> None:
        """Заводит сессию или обновляет её конфиг. `created_at` не перетирается.

        Конфиг едет одним JSON-полем целиком, поэтому новое поле в `AgentSpec`
        сохраняется само: имя, системный промпт и все параметры
        сэмплирования — это `asdict(spec)`, а не список колонок, который надо
        не забыть дополнить.
        """
        now = time.time()
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, label, config, context_length, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label     = excluded.label,
                    config    = excluded.config,
                    context_length = excluded.context_length,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    label,
                    _dumps(config),
                    context_length,
                    created_at,
                    now,
                ),
            )

    def load_session(self, session_id: str) -> dict | None:
        with self.reading() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session_row(row) if row is not None else None

    @staticmethod
    def _session_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "label": row["label"],
            "config": _loads(row["config"], {}),
            "context_length": row["context_length"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sessions(self) -> list[dict]:
        """Сохранённые чаты, свежие сверху, с числом реплик у каждой.

        Все до одной, без потолка: по этому списку строится список слева,
        а он не вправе молча что-то скрывать. Запрос дешёвый — пять тысяч
        чатов читаются за 13 мс.
        """
        with self.reading() as conn:
            rows = conn.execute(
                """
                SELECT s.*, (
                    SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id
                ) AS history_len
                FROM sessions s
                ORDER BY s.updated_at DESC
                """
            ).fetchall()
        out = []
        for row in rows:
            data = self._session_row(row)
            data["history_len"] = row["history_len"]
            out.append(data)
        return out

    def count_sessions(self) -> int:
        with self.reading() as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def delete_session(self, session_id: str) -> bool:
        """Стирает чат вместе с репликами, обе таблицы одной транзакцией.
        False — его и не было."""
        with self.tx() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            return bool(cursor.rowcount)

    def clear(self) -> None:
        """Стирает базу целиком, включая `meta`. Нужно только проверкам:
        оставленный счётчик имён отдал бы следующей номер посередине."""
        with self.tx() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM meta")

    # --- сообщения -----------------------------------------------------------

    def save_history(self, session_id: str, turns) -> None:
        """Переписывает историю чата целиком, одной транзакцией.

        Номера расставляются заново от нуля: откат обмена и снятая
        перегенерацией пара укорачивают историю, и «дописать хвост» оставил бы
        дыры в нумерации.

        Рассуждение не пишется: в контекст оно не возвращается, а места
        занимает больше ответа. Метрики пишутся — по ним рисуются плитки.
        """
        rows = [
            (
                session_id,
                seq,
                turn.role,
                turn.content,
                turn.error,
                _dumps(turn.metrics) if turn.metrics else None,
                turn.at,
            )
            for seq, turn in enumerate(turns)
        ]
        with self.tx() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            if rows:
                conn.executemany(
                    "INSERT INTO messages (session_id, seq, role, content, error, metrics, at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
            )

    def load_messages(self, session_id: str) -> list[dict]:
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT role, content, error, metrics, at FROM messages "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "role": r["role"],
                "content": r["content"],
                "error": r["error"],
                "metrics": _loads(r["metrics"], None) if r["metrics"] else None,
                "at": r["at"],
            }
            for r in rows
        ]

    # --- meta: счётчики, общие на всю базу -----------------------------------

    def next_counter(self, key: str) -> int:
        """Следующее число счётчика из базы. Никогда не повторяется.

        В памяти процесса он не годится дважды: после перезапуска начался бы
        с единицы, а консоль рядом с сервером вела бы свой счёт. Инкремент —
        одним запросом внутри транзакции, поэтому числа у процессов разные.
        """
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, '0') ON CONFLICT(key) DO NOTHING",
                (key,),
            )
            row = conn.execute(
                "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                "WHERE key = ? RETURNING value",
                (key,),
            ).fetchone()
        return int(row["value"])

    # --- идентификаторы сессий -----------------------------------------------

    def claim_agent_id(self) -> str:
        """Занимает свободный id, вставляя пустую строку чата.

        Счётчик процесса тут подсказка, арбитр — первичный ключ: строку завёл
        другой процесс, `INSERT` упал на конфликте, счётчик догнал базу, берём
        следующий. Иначе `save_history` стёр бы чужой диалог — `DELETE`
        по `session_id` его первая строчка.

        Нарушение ограничения в SQLite откатывает только сам запрос, поэтому
        цикл безопасно живёт и внутри общей транзакции.
        """
        from .agent import new_agent_id, reserve_ids

        now = time.time()
        with self.tx() as conn:
            for _ in range(_CLAIM_ATTEMPTS):
                candidate = new_agent_id()
                try:
                    conn.execute(
                        "INSERT INTO sessions (id, created_at, updated_at) VALUES (?, ?, ?)",
                        (candidate, now, now),
                    )
                except sqlite3.IntegrityError:
                    # Догоняем базу разом: разрыв счётчиков между процессами
                    # бывает и в сотни чатов.
                    reserve_ids(self.max_agent_seq())
                    continue
                return candidate
        raise RuntimeError(
            f"не удалось занять свободный id за {_CLAIM_ATTEMPTS} попыток — "
            f"похоже, база {self.path} занята кем-то ещё"
        )

    def max_agent_seq(self) -> int:
        """Наибольший номер в id вида `ag_00007` среди сохранённых чатов —
        по нему `reserve_ids` сдвигает счётчик процесса после перезапуска."""
        with self.reading() as conn:
            rows = conn.execute("SELECT id FROM sessions").fetchall()
        best = 0
        for row in rows:
            match = _ID_RE.match(row["id"] or "")
            if match:
                best = max(best, int(match.group(1)))
        return best


_STORE: Store | None = None
_STORE_LOCK = threading.Lock()


def shared_store() -> Store:
    """Хранилище процесса. Открывается один раз, по пути из AGENT_DB_PATH."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = Store().init()
        return _STORE

