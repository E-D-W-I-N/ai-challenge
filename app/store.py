"""Хранилище сессий и сообщений: SQLite из стандартной библиотеки.

До Дня 7 память агента жила в памяти процесса: реестр — обычный словарь,
и `Ctrl+C` стирал все диалоги разом. Здесь та же память переезжает в файл,
и разговор переживает перезапуск.

Две таблицы, потому что восстанавливать надо сессию целиком, а не только текст:

* `sessions` — id, родитель, ярлык и **конфиг агента одним JSON-полем**
  (модель, температура, системный промпт, `history_limit`, `extra_body`);
* `messages` — реплики, у каждой обязателен `session_id` и порядковый номер
  внутри сессии.

`session_id` в ключе таблицы сообщений — не украшение. Без него после
перезапуска все диалоги слились бы в одну ленту: два чата, поднятые из базы,
читали бы одни и те же строки. Поэтому первичный ключ здесь составной,
`(session_id, seq)`: строку сообщения физически нельзя записать, не сказав,
чья она.

Запись истории — всегда целиком и одной транзакцией: `DELETE` всех реплик
сессии плюс `INSERT` заново с номерами от нуля. Так порядковые номера не
получают дыр после отката несостоявшегося обмена или после кап-а хранимого,
и в базе никогда не оказывается вопроса без ответа — оборванная транзакция
откатывается целиком.

**Идентификатор сессии выдаёт база, а не процесс.** Стенд и CLI по умолчанию
работают с одним файлом, и это штатный сценарий: README предлагает запустить
консоль рядом с поднятым `uvicorn`. Счётчик в памяти процесса на это не годится
— два процесса выдали бы один и тот же `ag_00004`, и второй молча стёр бы
диалог первого (`save_history` начинается с `DELETE`). Поэтому id занимается
`INSERT`-ом строки сессии: конфликт по первичному ключу — это и есть арбитр,
а `claim_agent_id` на конфликте догоняет базу и берёт следующий свободный.

Всё, что уезжает в базу, проходит через `redact()`: транзакция отдаёт не голое
соединение, а обёртку, которая чистит строковые параметры любого запроса.
Забыть про новую колонку нельзя — она чистится по построению.
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
"""Куда пишется база, если AGENT_DB_PATH не задан.

Каталог в .gitignore вместе с `*.db`, `*.db-wal` и `*.db-shm`: репозиторий
публичный, а в базе лежат тексты диалогов.
"""

MEMORY = ":memory:"
"""Особый путь sqlite: база живёт в памяти и перезапуска не переживает.

Нужен только проверкам, которым база нужна на один вызов.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT,
    label       TEXT NOT NULL DEFAULT '',
    config      TEXT NOT NULL DEFAULT '{}',
    seed        TEXT NOT NULL DEFAULT '[]',
    overrides   TEXT NOT NULL DEFAULT '{}',
    seed_used   INTEGER NOT NULL DEFAULT 0,
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
    at          REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS sessions_by_parent ON sessions(parent_id);
"""

_ID_RE = re.compile(r"^ag_(\d+)$")

_CLAIM_ATTEMPTS = 50
"""Сколько раз пробуем занять id, прежде чем признать, что что-то не так.

Каждая неудача догоняет счётчик за базу, поэтому в норме хватает двух попыток:
первая ловит конфликт, вторая уже берёт свободный номер. Полсотни — это запас
на встречный поток из соседнего процесса, а не рабочий режим.
"""


def db_path() -> Path | str:
    """Путь к базе. Через переменную окружения — иначе проверки писали бы в рабочий файл."""
    raw = os.environ.get("AGENT_DB_PATH", "").strip()
    if not raw:
        return DEFAULT_DB_PATH
    return MEMORY if raw == MEMORY else Path(raw)


def _dumps(value) -> str:
    # default=str: в extra_body может приехать что угодно из day.py, и падение
    # сериализации не должно ронять обмен, который уже оплачен.
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: str, fallback):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def redact(value):
    """Вырезает ключ OpenRouter из всего, что уезжает в базу.

    Ключ в конфиг не попадает по построению — `app/llm.py` кладёт его прямо
    в заголовок запроса и нигде больше. Но `extra_body` приходит от клиента,
    и туда ключ можно вписать руками; в реплику диалога его тоже можно
    вставить, просто перепутав окно. Репозиторий публичный, файл базы —
    обычный файл: дешевле вырезать, чем потом отзывать ключ.
    """
    key = api_key()
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, "***")
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class _Writer:
    """Соединение, которое чистит строковые параметры любого запроса.

    Единственный способ что-то записать — взять транзакцию, а транзакция
    отдаёт эту обёртку. Поэтому обещание «ключ не уедет в базу» держится
    на всех колонках сразу, включая те, которых ещё нет: новую колонку
    нельзя добавить в обход `redact()`, потому что её значение приедет тем же
    параметром запроса.
    """

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @staticmethod
    def _clean(params):
        if isinstance(params, dict):
            return {key: redact(value) for key, value in params.items()}
        return tuple(redact(value) for value in params)

    def execute(self, sql: str, params=()):
        return self._conn.execute(sql, self._clean(params))

    def executemany(self, sql: str, rows):
        return self._conn.executemany(sql, (self._clean(row) for row in rows))


class Store:
    """Файл базы плюс несколько запросов к нему. Один на процесс.

    Соединение одно и живёт до `close()`: sqlite открывает файл дёшево, но
    открывать его на каждую реплику — значит терять WAL-кеш и упираться в
    блокировки на прогоне, где восемь колонок пишут одновременно. Все запросы
    идут под общим `RLock`: писать в стенде могут и цикл событий, и рабочий
    поток TestClient.
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
            # mkdir(parents=True), а не open(): на пустом каталоге sqlite падает
            # «unable to open database file», и стенд не поднимается вовсе.
            path.parent.mkdir(parents=True, exist_ok=True)
            target: str = str(path)
        else:
            target = MEMORY
        conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL: читатель не ждёт писателя. Побочный эффект — файлы -wal и -shm
        # рядом с базой, и .gitignore обязан ловить их тоже.
        if target != MEMORY:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        self._conn = conn
        self._migrate(conn)
        return self

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Догоняет схему базы, записанной прошлой версией стенда.

        `CREATE TABLE IF NOT EXISTS` новую колонку не добавит, а падать на
        чужом файле нельзя: в нём лежат сохранённые диалоги. Дописываем
        недостающее и идём дальше.
        """
        have = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "context_length" not in have:
            conn.execute("ALTER TABLE sessions ADD COLUMN context_length INTEGER")

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
    def tx(self):
        """Одна транзакция. Вложенные вызовы коммитятся один раз, самым внешним.

        Нужно спавну пачки: сто сессий — это одна транзакция, а не сто.

        Отдаёт не соединение, а `_Writer`: любой строковый параметр запроса
        проходит через `redact()`. Это и есть то самое «всё, что уезжает
        в базу, чистится» — по построению, а не по внимательности автора.
        """
        with self._lock:
            conn = self.conn
            outer = self._depth == 0
            if outer:
                # IMMEDIATE, а не голый BEGIN: писать в базу могут два процесса
                # сразу, и отложенная транзакция, начавшаяся с чтения, при
                # попытке записи получила бы SQLITE_BUSY без ретрая по
                # busy_timeout. Блокировку берём сразу — тогда второй писатель
                # честно ждёт своей очереди.
                conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield _Writer(conn)
            except BaseException:
                self._depth -= 1
                if outer:
                    conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    conn.execute("COMMIT")

    bulk = tx
    """Читаемое имя для пачки: `with store.bulk(): ...`."""

    # --- сессии --------------------------------------------------------------

    def save_session(
        self,
        session_id: str,
        *,
        parent_id: str | None,
        label: str,
        config: dict,
        seed: list[dict],
        overrides: dict,
        seed_used: bool,
        created_at: float,
        context_length: int | None = None,
    ) -> None:
        """Заводит сессию или обновляет её конфиг. `created_at` не перетирается."""
        now = time.time()
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, parent_id, label, config, seed, overrides, seed_used,
                     context_length, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    label     = excluded.label,
                    config    = excluded.config,
                    seed      = excluded.seed,
                    overrides = excluded.overrides,
                    seed_used = excluded.seed_used,
                    context_length = excluded.context_length,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    parent_id,
                    label,
                    _dumps(redact(config)),
                    _dumps(redact(seed)),
                    _dumps(redact(overrides)),
                    1 if seed_used else 0,
                    context_length,
                    created_at,
                    now,
                ),
            )

    def load_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._session_row(row) if row is not None else None

    @staticmethod
    def _session_row(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "label": row["label"],
            "config": _loads(row["config"], {}),
            "seed": _loads(row["seed"], []),
            "overrides": _loads(row["overrides"], {}),
            "seed_used": bool(row["seed_used"]),
            "context_length": row["context_length"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_sessions(self, *, limit: int = 500) -> list[dict]:
        """Сохранённые сессии, свежие сверху, с числом реплик у каждой."""
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT s.*, (
                    SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id
                ) AS history_len
                FROM sessions s
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            data = self._session_row(row)
            data["history_len"] = row["history_len"]
            out.append(data)
        return out

    def count_sessions(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def children(self, parent_id: str) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM sessions WHERE parent_id = ?", (parent_id,)
            ).fetchall()
        return [row["id"] for row in rows]

    def delete_session(self, session_id: str) -> list[str]:
        """Удаляет сессию и её потомков — из базы, а не только из памяти.

        Каскад по `parent_id` идёт по базе, а не по реестру: субагент прогона
        мог быть вытеснен из памяти раньше родителя, и в реестре его уже нет,
        а строка в базе осталась бы висеть навсегда.
        """
        removed: list[str] = []
        with self.tx() as conn:
            stack = [session_id]
            seen = set()
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                stack.extend(self.children(current))
                cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (current,))
                conn.execute("DELETE FROM messages WHERE session_id = ?", (current,))
                if cursor.rowcount:
                    removed.append(current)
        return removed

    def clear(self) -> None:
        """Стирает базу целиком. Нужно только проверкам между собой."""
        with self.tx() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")

    # --- сообщения -----------------------------------------------------------

    def save_history(self, session_id: str, turns) -> None:
        """Переписывает историю сессии целиком, одной транзакцией.

        Номера расставляются заново от нуля: откат несостоявшегося обмена и
        кап хранимого укорачивают историю, и «дописать хвост» тут не годится —
        в нумерации остались бы дыры, а по ним потом восстанавливать порядок.
        """
        rows = [
            (
                session_id,
                seq,
                turn.role,
                redact(turn.content),
                redact(turn.error),
                turn.at,
            )
            for seq, turn in enumerate(turns)
        ]
        with self.tx() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            if rows:
                conn.executemany(
                    "INSERT INTO messages (session_id, seq, role, content, error, at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
            )

    def load_messages(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT role, content, error, at FROM messages "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {"role": r["role"], "content": r["content"], "error": r["error"], "at": r["at"]}
            for r in rows
        ]

    def message_rows(self, session_id: str) -> list[tuple]:
        """(seq, role, content) как они лежат в базе — этим проверяют нумерацию."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT seq, role, content FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [(r["seq"], r["role"], r["content"]) for r in rows]

    # --- идентификаторы ------------------------------------------------------

    def claim_agent_id(self) -> str:
        """Занимает свободный id, вставляя пустую строку сессии.

        Счётчик в памяти процесса тут только подсказка: арбитр — первичный ключ.
        Если строка с таким id уже есть (её завёл другой процесс — например,
        CLI рядом с поднятым стендом), `INSERT` падает на конфликте, счётчик
        догоняет базу, и мы берём следующий свободный. Без этого второй процесс
        выдал бы занятый id и `save_history` стёр бы чужой диалог: `DELETE`
        по `session_id` — первая строчка записи истории.

        В SQLite нарушение ограничения откатывает только сам запрос, а не всю
        транзакцию, поэтому цикл безопасно живёт и внутри `bulk()`.
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
                    # Догоняем базу разом, а не по одному: между процессами
                    # разрыв счётчиков бывает и в сотни сессий.
                    reserve_ids(self.max_agent_seq())
                    continue
                return candidate
        raise RuntimeError(
            f"не удалось занять свободный id за {_CLAIM_ATTEMPTS} попыток — "
            f"похоже, база {self.path} занята кем-то ещё"
        )

    def max_agent_seq(self) -> int:
        """Наибольший номер в id вида `ag_00007` среди сохранённых сессий.

        Счётчик id живёт в процессе и после перезапуска начинается с нуля.
        Без этой поправки второй запуск выдал бы `ag_00001` заново — и новый
        агент молча унаследовал бы чужую историю из базы.
        """
        with self._lock:
            rows = self.conn.execute("SELECT id FROM sessions").fetchall()
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


def close_shared_store() -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.close()
            _STORE = None
