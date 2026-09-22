"""Хранилище чатов и сообщений: SQLite из стандартной библиотеки.

Десять таблиц; `memory` и `profile` — без `session_id`: оба слоя общие
на всю базу и переживают любой чат. За чем следить:

* **`session_id` в первичном ключе сообщений**: без него два чата читали бы
  одни и те же строки;
* **история пишется целиком и одной транзакцией**, `seq` от нуля без дыр;
* **id выдаёт база, а не процесс**: счётчик в памяти выдал бы консоли
  и серверу на одном файле общий `ag_00004`;
* **`redact()` на всех колонках сразу**: транзакция отдаёт обёртку, а не
  соединение, — забыть про новую колонку нельзя;
* **схема накатывается одним `CREATE TABLE IF NOT EXISTS`**, миграций нет:
  новая таблица заводится сама, колонка в живую таблицу — нет.
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

-- Сводки начала разговора. Не строкой в `messages`: `save_history` начинается
-- с `DELETE`, и сводка стиралась бы после каждого обмена. Строк несколько,
-- по одной на сворачивание: у каждого свои метрики, и без них счёт экономии
-- от сжатия стал бы враньём. Каскада нет, FK не объявлены — чистить руками
-- на всех трёх путях: `delete_session`, `clear`, `forget()`.
CREATE TABLE IF NOT EXISTS summaries (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    upto        INTEGER NOT NULL,
    content     TEXT NOT NULL,
    metrics     TEXT,
    at          REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS summaries_by_session ON summaries(session_id);

-- Рабочая память чата: записи о состоянии задачи, их вписывает человек.
--
-- * `seq` — **AUTOINCREMENT**: запись правится по одной, и перенумерация
--   сдвинула бы номера соседей — вторая вкладка удалила бы не ту;
-- * `session_id` колонкой, а не частью ключа: ключ занят сквозным номером,
--   но область записи по-прежнему чат, и вместе с ним она умирает;
-- * `kind` колонкой, а не префиксом в тексте: по нему подписана врезка.
--
-- Индекс по `session_id` нужен, в отличие от `memory`: та читается целиком,
-- а эта всегда одним чатом. Чистить руками на всех трёх путях.
CREATE TABLE IF NOT EXISTS working_memory (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS working_by_session ON working_memory(session_id);

-- Происхождение чата: чей он потомок и сколько первых сообщений унёс.
-- Ветка — обычный чат со своей строкой в `sessions`; схему `messages`
-- ветвление не трогает вовсе.
--
-- Своя таблица, а не поле в `config`: происхождение не настройка. И не
-- колонка: таблица накатится на живую базу сама. `session_id` первичным
-- ключом — у разговора ровно одно происхождение.
--
-- **Удаление родителя ветку не удаляет**: она самостоятельный чат, и строка
-- чистится только со своим — `delete_session` и `clear`.
CREATE TABLE IF NOT EXISTS branches (
    session_id  TEXT PRIMARY KEY,
    parent_id   TEXT NOT NULL,
    forked_at   INTEGER NOT NULL,
    at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS branches_by_parent ON branches(parent_id);

-- Состояние задачи: **план и есть состояние**. Этап, текущий шаг и ожидаемое
-- действие **вычисляются** (`app/plan.py`), а не хранятся — ярлык рядом
-- с работой расходился бы с работой молча.
--
-- Таблиц две, а не одна с JSON: флажки живут и у пустого списка, и «план
-- утверждён» обязано пережить возврат шага в `pending`. Поля «с какого этапа
-- приостановлено» нет намеренно: сняли флажок — этап восстановился сам.
--
-- `seq` у шагов — номер от нуля без дыр, а не AUTOINCREMENT: список приезжает
-- целиком и переписывается снимком, значит дисциплина та же, что у истории
-- и сводок. Чистить руками на всех трёх путях: план — содержимое разговора.
CREATE TABLE IF NOT EXISTS task_state (
    session_id TEXT PRIMARY KEY,
    approved   INTEGER NOT NULL DEFAULT 0,
    finished   INTEGER NOT NULL DEFAULT 0,
    paused     INTEGER NOT NULL DEFAULT 0,
    at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_steps (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS task_steps_by_session ON task_steps(session_id);

-- Долговременная память: слой общий на всю базу, без `session_id`. Переживает
-- и удаление чата, и `forget()`; чистит его ровно один путь — `clear()`.
-- Наполняет её **только человек**: он явно выбирает, что переживёт задачу.
--
-- `seq` — AUTOINCREMENT, довод тот же, что у `working_memory`. Индекса нет:
-- таблица читается только целиком.
CREATE TABLE IF NOT EXISTS memory (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    content TEXT NOT NULL,
    at      REAL NOT NULL
);

-- Профиль пользователя: как с ним разговаривать. Без `session_id` по тому же
-- доводу, что у `memory`: профиль один на всю базу. Строка на поле, а не одна
-- на три колонки: «поля нет» тогда читается как «строки нет», и отличать
-- пустую строку от NULL не приходится вовсе.
--
-- Наполняет её **только человек**, ручкой `/api/profile`: профиль это
-- распоряжение, а распоряжений из разговора агент не выводит.
CREATE TABLE IF NOT EXISTS profile (
    field   TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    at      REAL NOT NULL
);
"""

_ID_RE = re.compile(r"^ag_(\d+)$")

BUSY_TIMEOUT_MS = 5000
"""Сколько ждать освобождения базы. Дольше нельзя — ожидание блокировки
замораживает цикл событий."""

_CLAIM_ATTEMPTS = 50
"""Сколько раз пробуем занять id. В норме хватает двух; полсотни — запас."""


def db_path() -> Path | str:
    """Через переменную окружения — иначе проверки писали бы в рабочий файл."""
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


def _branch_row(parent_id, forked_at) -> dict | None:
    """Одна форма на оба чтения — по строке и списком: разъедься они, пометка
    ветки в списке и в панели назвала бы разные числа."""
    if parent_id is None:
        return None
    return {"parent_id": parent_id, "forked_at": forked_at}


def _memory_row(row: sqlite3.Row) -> dict:
    """Одна форма на все чтения: разъедься они, вкладка показывала бы одно,
    а в модель уезжало бы другое."""
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "content": row["content"],
        "at": row["at"],
    }


def _working_row(row: sqlite3.Row) -> dict:
    """Одна форма на оба чтения — довод тот же, что у `_memory_row`."""
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "content": row["content"],
        "at": row["at"],
    }


MIN_SECRET_LENGTH = 16
"""Короче — не ключ: редакция работает подстрокой, и с ключом в один символ
изрезала бы `ag_00001` в `ag_***0000***`. Ключ OpenRouter — 73 символа."""


def redact(value):
    """Вырезает ключ OpenRouter из всего, что уезжает в базу: `extra_body`
    приходит от клиента, да и в реплику ключ можно вставить, перепутав окно."""
    key = api_key()
    if not key or len(key) < MIN_SECRET_LENGTH:
        return value
    if isinstance(value, str):
        return value.replace(key, "***")
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class StoreBusyError(RuntimeError):
    """База занята другим процессом дольше `busy_timeout`. Свой класс: наверху
    из него делают 503 — ситуация штатная, а не поломка."""


def _busy(exc: sqlite3.OperationalError, path) -> StoreBusyError | None:
    """None — ошибка не про блокировку: «no such table» это баг схемы,
    и подменять его успокаивающим текстом нельзя."""
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
    """Соединение, чистящее строковые параметры любого запроса. Записать можно
    только через транзакцию, а она отдаёт эту обёртку: обещание держится
    на всех колонках сразу, включая те, которых ещё нет."""

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
    """Файл базы плюс запросы к нему. Один на процесс: открывать файл
    на каждую реплику значит терять WAL-кеш. Запросы под общим `RLock`:
    писать могут и цикл событий, и поток TestClient."""

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
            # Без mkdir sqlite падает «unable to open database file».
            path.parent.mkdir(parents=True, exist_ok=True)
            target: str = str(path)
        else:
            target = MEMORY
        conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL: читатель не ждёт писателя. Файлы -wal и -shm ловит .gitignore.
        if target != MEMORY:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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
        """Одна транзакция; вложенные коммитятся один раз, самым внешним —
        спавну сотни чатов нужна одна транзакция, а не сто. Отдаёт не
        соединение, а `_Writer`: с `redact()` на любом параметре."""
        with self._lock:
            conn = self.conn
            outer = self._depth == 0
            if outer:
                # IMMEDIATE, а не голый BEGIN: отложенная транзакция
                # получила бы SQLITE_BUSY без ретрая по busy_timeout.
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
        """Заводит сессию или обновляет её конфиг; `created_at` не перетирается.
        Конфиг едет одним JSON-полем: новое поле `AgentSpec` сохраняется само.
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

    def list_sessions(self, *, limit: int | None = None) -> list[dict]:
        """Сохранённые чаты, свежие сверху, с числом реплик у каждой.
        `limit=None` по умолчанию: по этому списку строится список слева,
        а он не вправе молча что-то скрывать."""
        sql = """
            SELECT s.*, (
                SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id
            ) AS history_len,
            b.parent_id AS branch_parent_id, b.forked_at AS branch_forked_at
            FROM sessions s
            LEFT JOIN branches b ON b.session_id = s.id
            ORDER BY s.updated_at DESC
        """
        with self.reading() as conn:
            if limit is None:
                rows = conn.execute(sql).fetchall()
            else:
                rows = conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
        out = []
        for row in rows:
            data = self._session_row(row)
            data["history_len"] = row["history_len"]
            # Тем же запросом, а не по строке на чат: пометка ветки обязана
            # стоить столько же, сколько имя чата.
            data["branch"] = _branch_row(row["branch_parent_id"], row["branch_forked_at"])
            out.append(data)
        return out

    def count_sessions(self) -> int:
        with self.reading() as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def delete_session(self, session_id: str) -> bool:
        """Стирает чат со всеми его таблицами, одной транзакцией. False — его
        и не было.

        Каскада нет, FK не объявлены: не вычистишь руками — сводка, цели
        и шаги достанутся чату с тем же id. `memory` и `profile` не трогаются
        намеренно (чат им не владелец, а читатель), как и строки **потомков**
        в `branches`: удаление родителя ветку не удаляет.
        """
        with self.tx() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM working_memory WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM task_state WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM task_steps WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM branches WHERE session_id = ?", (session_id,))
            return bool(cursor.rowcount)

    def clear(self) -> None:
        """Стирает базу целиком, все десять таблиц. Нужно только проверкам.
        Память и профиль здесь **обязаны** стираться, хотя удаление чата их
        не трогает: `kill_all()` зовёт `clear()` перед каждой проверкой,
        и забытая строка утекла бы врезкой в чужой промпт, сдвинув там роли.
        """
        with self.tx() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM summaries")
            conn.execute("DELETE FROM working_memory")
            conn.execute("DELETE FROM task_state")
            conn.execute("DELETE FROM task_steps")
            conn.execute("DELETE FROM branches")
            conn.execute("DELETE FROM memory")
            conn.execute("DELETE FROM profile")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM meta")

    # --- сообщения -----------------------------------------------------------

    def save_history(self, session_id: str, turns) -> None:
        """Переписывает историю чата целиком, одной транзакцией. Номера заново
        от нуля: откат обмена и снятая перегенерацией пара укорачивают
        историю, и «дописать хвост» оставил бы дыры. Рассуждение не пишется —
        в контекст оно не возвращается."""
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

    # --- сводки начала разговора ---------------------------------------------

    def save_summaries(self, session_id: str, summaries) -> None:
        """Переписывает сводки чата целиком, одной транзакцией — дисциплина
        как у `save_history`. Пустой список стирает сводки: это и есть
        очистка на `forget()`."""
        rows = [
            (
                session_id,
                seq,
                int(item["upto"]),
                item["content"],
                _dumps(item["metrics"]) if item.get("metrics") else None,
                item.get("at") or time.time(),
            )
            for seq, item in enumerate(summaries)
        ]
        with self.tx() as conn:
            conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
            if rows:
                conn.executemany(
                    "INSERT INTO summaries (session_id, seq, upto, content, metrics, at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )

    def load_summaries(self, session_id: str) -> list[dict]:
        """По порядку сворачивания. Лежат отдельно от истории, поэтому
        перезапись истории их не трогает."""
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT seq, upto, content, metrics, at FROM summaries "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            {
                "seq": r["seq"],
                "upto": r["upto"],
                "content": r["content"],
                "metrics": _loads(r["metrics"], None) if r["metrics"] else None,
                "at": r["at"],
            }
            for r in rows
        ]

    # --- рабочая память чата --------------------------------------------------
    # Записи, а не снимок: каждая правится и удаляется по своему номеру.
    # Отсюда четыре метода по операции, а не один «переписать целиком».

    def list_working(self, session_id: str) -> list[dict]:
        """По порядку номеров, то есть по времени появления: правка записи
        с места её не двигает, и список во вкладке не перетасовывается."""
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT seq, kind, content, at FROM working_memory "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [_working_row(row) for row in rows]

    def add_working(
        self, session_id: str, kind: str, content: str, at: float | None = None
    ) -> dict:
        """Добавляет запись и отдаёт её целиком — вместе с номером от базы.
        Номер через `RETURNING`, а не вторым запросом: между `INSERT`
        и `SELECT max(seq)` пролез бы второй писатель. Отдаётся
        **записанное** — `redact()` чистит текст по дороге."""
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO working_memory (session_id, kind, content, at) "
                "VALUES (?, ?, ?, ?) "
                "RETURNING seq, kind, content, at",
                (session_id, kind, content, stamp),
            ).fetchone()
        return _working_row(row)

    def update_working(
        self, session_id: str, seq: int, *, kind: str, content: str,
        at: float | None = None,
    ) -> dict | None:
        """Переписывает одну запись по номеру. `None` — такой в этом чате нет.
        `session_id` в `WHERE` обязателен: номера сквозные на всю базу.
        Значения приходят все сразу: `COALESCE` завёл бы второе место, где
        решается, чем пустое поле отличается от неназванного."""
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "UPDATE working_memory SET kind = ?, content = ?, at = ? "
                "WHERE seq = ? AND session_id = ? "
                "RETURNING seq, kind, content, at",
                (kind, content, stamp, int(seq), session_id),
            ).fetchone()
        return None if row is None else _working_row(row)

    def delete_working(self, session_id: str, seq: int) -> bool:
        """Стирает одну запись. False — такой в этом чате не было. Номер
        не достанется никому: у `seq` стоит AUTOINCREMENT, и вторая вкладка
        со списком с прошлой минуты не удалит по нему чужую запись."""
        with self.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM working_memory WHERE seq = ? AND session_id = ?",
                (int(seq), session_id),
            )
            return bool(cursor.rowcount)

    def clear_working(self, session_id: str) -> None:
        """Забытый разговор не вправе оставить следующему свои цели: врезка
        встаёт в промпт, пока в памяти есть хоть одна запись."""
        with self.tx() as conn:
            conn.execute("DELETE FROM working_memory WHERE session_id = ?", (session_id,))

    # --- состояние задачи: план со статусами ----------------------------------
    # Список приезжает целиком, одним вызовом модели, — поэтому здесь два
    # метода, а не четыре: записать снимок и прочитать его.

    def save_plan(self, session_id: str, plan: dict | None) -> None:
        """Переписывает состояние задачи целиком, одной транзакцией —
        дисциплина как у `save_history`. `None` или пустой план без флажков —
        только `DELETE` из обеих таблиц: это очистка на `forget()` и сбросе.
        Пустой список с флажком сохранится: врать про этап план не станет."""
        steps = (plan or {}).get("steps") or []
        approved = bool((plan or {}).get("approved"))
        finished = bool((plan or {}).get("finished"))
        paused = bool((plan or {}).get("paused"))
        rows = [
            (session_id, seq, str(step.get("title") or ""), str(step.get("status") or ""))
            for seq, step in enumerate(steps)
            if isinstance(step, dict)
        ]
        with self.tx() as conn:
            conn.execute("DELETE FROM task_steps WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM task_state WHERE session_id = ?", (session_id,))
            if plan is None or not (rows or approved or finished or paused):
                return
            conn.execute(
                "INSERT INTO task_state (session_id, approved, finished, paused, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, int(approved), int(finished), int(paused), time.time()),
            )
            if rows:
                conn.executemany(
                    "INSERT INTO task_steps (session_id, seq, title, status) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )

    def load_plan(self, session_id: str) -> dict | None:
        """Состояние задачи чата — или `None`, если строки нет вовсе.
        `None`, а не пустой план: чем их считать, решает один вызывающий
        (`Agent`), а не два места по-своему."""
        with self.reading() as conn:
            row = conn.execute(
                "SELECT approved, finished, paused FROM task_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            steps = conn.execute(
                "SELECT title, status FROM task_steps WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return {
            "steps": [{"title": s["title"], "status": s["status"]} for s in steps],
            "approved": bool(row["approved"]),
            "finished": bool(row["finished"]),
            "paused": bool(row["paused"]),
        }

    # --- происхождение чата ---------------------------------------------------

    def save_branch(self, session_id: str, *, parent_id: str, forked_at: int, at=None) -> None:
        """Записывает, чей этот чат потомок и сколько первых сообщений унёс.
        `ON CONFLICT` ради идемпотентности, а не второго происхождения: одно
        происхождение на чат стережёт первичный ключ."""
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO branches (session_id, parent_id, forked_at, at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    parent_id = excluded.parent_id,
                    forked_at = excluded.forked_at,
                    at        = excluded.at
                """,
                (session_id, parent_id, int(forked_at), time.time() if at is None else at),
            )

    def load_branch(self, session_id: str) -> dict | None:
        """Происхождение чата или `None` — чат заведён сам по себе. Имени
        родителя здесь нет намеренно: копия разошлась бы с ним на первом же
        переименовании, а по id имя находит тот, кто рисует список."""
        with self.reading() as conn:
            row = conn.execute(
                "SELECT parent_id, forked_at FROM branches WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return None if row is None else _branch_row(row["parent_id"], row["forked_at"])

    def message_rows(self, session_id: str) -> list[tuple]:
        """(seq, role, content) как лежат в базе — этим проверяют нумерацию."""
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT seq, role, content FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [(r["seq"], r["role"], r["content"]) for r in rows]

    # --- долговременная память ------------------------------------------------

    def add_memory(self, kind: str, content: str, at: float | None = None) -> dict:
        """Добавляет запись и отдаёт её целиком — вместе с номером от базы.
        Доводы про `RETURNING` и про «отдаётся записанное» — как
        у `add_working`."""
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO memory (kind, content, at) VALUES (?, ?, ?) "
                "RETURNING seq, kind, content, at",
                (kind, content, stamp),
            ).fetchone()
        return _memory_row(row)

    def update_memory(
        self, seq: int, *, kind: str, content: str, at: float | None = None
    ) -> dict | None:
        """Переписывает одну запись по номеру. `None` — такой нет. Значения
        приходят все сразу — довод тот же, что у `update_working`."""
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "UPDATE memory SET kind = ?, content = ?, at = ? "
                "WHERE seq = ? RETURNING seq, kind, content, at",
                (kind, content, stamp, int(seq)),
            ).fetchone()
        return None if row is None else _memory_row(row)

    def list_memory(self) -> list[dict]:
        """Вся память по порядку добавления. Целиком и всегда: слой глобальный,
        отбирать не по чему."""
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT seq, kind, content, at FROM memory ORDER BY seq"
            ).fetchall()
        return [_memory_row(row) for row in rows]

    def delete_memory(self, seq: int) -> bool:
        """Стирает одну запись. False — такой не было. Номер не достанется
        никому — довод тот же, что у `delete_working`."""
        with self.tx() as conn:
            cursor = conn.execute("DELETE FROM memory WHERE seq = ?", (int(seq),))
            return bool(cursor.rowcount)

    # --- профиль: как отвечать этому человеку --------------------------------

    def load_profile(self) -> dict:
        """Профиль целиком: `{поле: текст}`, только заполненные поля. Снятое
        поле удаляется, а не хранится пустым, — значит «профиль пуст»
        и «профиля нет» это одно значение, и врезке не надо их различать."""
        with self.reading() as conn:
            rows = conn.execute("SELECT field, content FROM profile").fetchall()
        return {row["field"]: row["content"] for row in rows}

    def save_profile(self, values: dict, at: float | None = None) -> dict:
        """Записывает названные поля и отдаёт профиль целиком. Неназванное
        не трогается, пустая строка **снимает** поле: второе представление
        пустоты развело бы врезку с хранением. Отдаётся записанное."""
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            for field, content in values.items():
                if content:
                    conn.execute(
                        "INSERT INTO profile (field, content, at) VALUES (?, ?, ?) "
                        "ON CONFLICT(field) DO UPDATE SET content = excluded.content, "
                        "at = excluded.at",
                        (field, content, stamp),
                    )
                else:
                    conn.execute("DELETE FROM profile WHERE field = ?", (field,))
            rows = conn.execute("SELECT field, content FROM profile").fetchall()
        return {row["field"]: row["content"] for row in rows}

    # --- meta: счётчики, общие на всю базу -----------------------------------

    def next_counter(self, key: str) -> int:
        """Следующее число счётчика из базы, никогда не повторяющееся.
        В памяти процесса он не годится: после перезапуска начался бы
        с единицы, а консоль рядом с сервером вела бы свой счёт."""
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
        """Занимает свободный id, вставляя пустую строку чата. Счётчик процесса
        тут подсказка, арбитр — первичный ключ: `INSERT` упал на конфликте,
        счётчик догнал базу, берём следующий (иначе `save_history` стёр бы
        чужой диалог). Нарушение ограничения откатывает только сам запрос,
        поэтому цикл живёт и внутри `tx()`."""
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
