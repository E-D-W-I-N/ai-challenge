"""Хранилище чатов и сообщений: SQLite из стандартной библиотеки.

Девять таблиц: `sessions` (id, имя и **конфиг одним JSON-полем** — новое поле
сохраняется само, а не ждёт, пока вспомнят про колонку), `messages`, `meta`
(счётчики, общие на всю базу), `summaries` (сводки начала разговора),
`working_memory` (рабочая память чата — записи о состоянии задачи) и
`working_state` (докуда её дочитало извлечение и во что обошлось), `facts`
(снимок выписки, оставленный от Дня 10 и больше не используемый), `branches`
(чей потомок этот чат и сколько сообщений он унёс) и `memory`
(долговременная память — единственная таблица **без** `session_id`).
Четыре свойства, за которыми стоит следить:

* **`session_id` в первичном ключе сообщений**: без него два чата из базы
  читали бы одни и те же строки, и список слева слился бы в один диалог;
* **история пишется целиком и одной транзакцией** — `DELETE` плюс `INSERT`
  заново с номерами от нуля: `seq` не получает дыр, а оборванная запись
  откатывается вся;
* **id выдаёт база, а не процесс**: консоль запускают рядом с сервером, файл
  у них один, и счётчик в памяти выдал бы обоим `ag_00004` — второй стёр бы
  диалог первого (`save_history` начинается с `DELETE`);
* **`redact()` на всех колонках сразу**: транзакция отдаёт не соединение,
  а обёртку, чистящую строковые параметры любого запроса, — забыть про новую
  колонку нельзя.
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

-- Сводки начала разговора. Отдельной таблицей, а не колонкой в `sessions`
-- и не строкой в `messages`: `save_history` на каждой записи начинается
-- с `DELETE FROM messages`, и сводка стиралась бы после каждого обмена.
--
-- Строк несколько, а не одна на чат: сворачиваний за разговор столько же,
-- сколько раз перевалило за порог, и у каждого свои метрики. Одна строка
-- затирала бы метрики прошлых сжатий — а без них честного счёта стоимости
-- сжатия не будет, то есть «экономия» станет враньём.
--
-- Ключ зеркалит `messages`: (session_id, seq). Каскада нет, FK не объявлены —
-- чистить руками на всех трёх путях: удаление чата, очистка базы, `forget()`.
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

-- Снимок выписки фактов Дня 10. **Больше не используется**: рабочая память
-- переехала в `working_memory` записями с устойчивыми номерами, и ни один
-- путь кода сюда не пишет и отсюда не читает. Таблица оставлена в схеме
-- намеренно — сносит её миграция, та же, что добавляет колонку авторства;
-- содержимое терять не жаль, снимок был производным от истории и собрался бы
-- заново. Дальше — довод, по которому она когда-то завелась, и он же теперь
-- работает на её наследницу.
--
-- Факты о разговоре: цель, ограничения, предпочтения, решения, договорённости.
-- Своя таблица по тому же доводу, что и `summaries`: колонку в `sessions` не
-- накатить (миграций в коде нет, `_migrate` удалён), а строкой в `messages`
-- факт стирался бы каждым обменом — `save_history` начинается с
-- `DELETE FROM messages`.
--
-- Строки — **снимок**, а не журнал: список фактов переписывается целиком на
-- каждом извлечении, и семь строк здесь значат семь фактов, а не семь вызовов.
-- Поэтому `upto` и `metrics` у строк одного снимка одинаковые: они про снимок,
-- а не про отдельный факт, а второй формы строки в одной таблице заводить
-- незачем.
--
-- `upto` — сколько первых реплик истории уже прочитано извлечением. Он и
-- зажимал срез промпта: срезано ровно то, что выписка
-- прочитала, а не то, что хотелось бы срезать. Без него провалившееся
-- извлечение (или подъём чата из базы) дал бы срезать начало, которого
-- выписка никогда не видела, — молча и под подписью «факты вместо N
-- сообщений». Он же говорит извлечению, с какой реплики читать дальше.
--
-- `metrics` — **накопленные** числа всех вызовов на извлечение этого чата.
-- Вызов идёт на каждом обмене, снимок переписывается тогда же, и метрики
-- отдельного вызова в нём не удержались бы; а итог по чату обязан считать их
-- все — иначе экономия врёт.
--
-- Каскада нет, FK не объявлены — чистить руками на всех трёх путях: удаление
-- чата, очистка базы, `forget()`.
CREATE TABLE IF NOT EXISTS facts (
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    upto        INTEGER NOT NULL,
    metrics     TEXT,
    at          REAL NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE INDEX IF NOT EXISTS facts_by_session ON facts(session_id);

-- Рабочая память чата: состояние задачи записями — цель, ограничение,
-- решение, открытый вопрос. Пришла на смену снимку `facts`, и смена эта
-- не про удобство: снимок переписывался целиком на каждом извлечении, а
-- писать сюда будет и человек. Его правка исчезла бы на следующем обмене,
-- а править «по номеру» было бы нельзя — завтра тот же номер достался бы
-- другой записи.
--
-- Отсюда все четыре отличия от снимка:
--
-- * `seq` — **AUTOINCREMENT**, а не номер от нуля: запись здесь имеет
--   идентичность и правится по одной. Перенумерация сдвинула бы номера
--   соседей, и вторая вкладка удалила бы не ту запись; AUTOINCREMENT
--   добавляет «номер удалённой не выдаётся заново» средствами самого
--   первичного ключа. Довод тот же, по которому он стоит у `memory`
--   и у `claim_agent_id`;
-- * `session_id` колонкой, а не частью ключа: ключ занят сквозным номером,
--   а область у записи по-прежнему чат — это рабочая память **разговора**,
--   и вместе с ним она и умирает;
-- * `kind` — род записи (`WORKING_KINDS`), колонкой по тому же доводу, что
--   и у `memory`: по нему подписана строка врезки, и разбирать текст обратно
--   пришлось бы и врезке, и вкладке;
-- * `author` — кто записал: служебный вызов или человек. Это не пометка для
--   красоты, а **инвариант**: извлечение правит только свои записи. Не будь
--   колонки, первая же правка руками исчезла бы на следующем обмене.
--
-- Индекс по `session_id` нужен, в отличие от `memory`: та читается целиком,
-- а эта всегда одним чатом.
--
-- Каскада нет, FK не объявлены — чистить руками на всех трёх путях: удаление
-- чата, очистка базы, `forget()`.
CREATE TABLE IF NOT EXISTS working_memory (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    author     TEXT NOT NULL,
    at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS working_by_session ON working_memory(session_id);

-- Чем занято извлечение: докуда оно дочитало историю этого чата и во что
-- обошлись все его вызовы. Отдельной таблицей, а не колонками в
-- `working_memory`, и довод ровно тот, по которому записи перестали быть
-- снимком: это числа **про вызов**, а не про запись. У записи, набранной
-- руками, никакого `upto` нет и быть не может, а список, опустевший до нуля
-- записей, обязан помнить прочитанное — иначе следующее извлечение
-- перечитало бы разговор с начала, а зажим среза («резать не дальше
-- прочитанного») на пустом списке обнулился бы молча.
--
-- `upto` — сколько первых реплик истории уже прочитано извлечением. Он и
-- зажимает срез промпта (`Agent.working_cover`): срезано ровно то, что
-- память прочитала, а не то, что хотелось бы срезать. Он же говорит
-- извлечению, с какой реплики читать дальше.
--
-- `metrics` — **накопленные** числа всех вызовов на извлечение этого чата:
-- вызов идёт на каждом обмене, а итог по чату обязан считать их все.
--
-- Строка одна на чат, поэтому `session_id` первичным ключом. Чистится
-- вместе с записями, теми же тремя путями.
CREATE TABLE IF NOT EXISTS working_state (
    session_id TEXT PRIMARY KEY,
    upto       INTEGER NOT NULL,
    metrics    TEXT,
    at         REAL NOT NULL
);

-- Происхождение чата: чей он потомок и сколько первых сообщений унёс.
-- Ветка — обычный чат, отдельная строка в `sessions` с копией истории до
-- точки ветвления; схему `messages` ветвление не трогает вовсе, изоляция
-- сессий там уже по `session_id`, а `seq` у копии идёт от нуля и без дыр.
--
-- Своя таблица, а не поле в `config`: `config` это `asdict(spec)`, «чем один
-- чат отличается от другого», а происхождение — не настройка. Тот же довод,
-- по которому в контекст не поехали ни сводка, ни факты. И тот же, по
-- которому это таблица, а не колонка: `CREATE TABLE IF NOT EXISTS` накатится
-- на живую базу сам, а колонка бы не накатилась — миграций в коде нет.
--
-- `session_id` первичным ключом: у разговора ровно одно происхождение.
-- `forked_at` — сколько первых сообщений родителя унесено, оно же место
-- ветвления. Каскада нет, FK не объявлены, и это здесь не упущение:
-- **удаление родителя ветку не удаляет** — ветка самостоятельный чат, и
-- строка о том, от кого она отделилась, живёт дольше родителя. Чистится
-- строка только вместе со своим чатом: `delete_session` и `clear`.
CREATE TABLE IF NOT EXISTS branches (
    session_id  TEXT PRIMARY KEY,
    parent_id   TEXT NOT NULL,
    forked_at   INTEGER NOT NULL,
    at          REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS branches_by_parent ON branches(parent_id);

-- Долговременная память — третий слой модели памяти, и единственная таблица
-- **без** `session_id`. В этом вся разница: краткосрочная память (`messages`)
-- и рабочая (`summaries`, `working_memory`) привязаны к чату и умирают вместе с ним,
-- а этот слой общий на всю базу и переживает и удаление чата, и `forget()`.
-- Чистит его ровно один путь — `clear()`, служебная очистка базы.
--
-- Наполняется **только руками**: ни одного служебного вызова к модели за ней
-- не стоит. «Явно выбирать, что и куда сохраняется» понято буквально: строка
-- сюда попадает нажатием кнопки, а не догадкой сервера.
--
-- `seq` — AUTOINCREMENT, а не номера от нуля, как у истории, сводок и фактов.
-- Те переписываются целиком (`DELETE` плюс `executemany`), потому что они
-- снимок; память — список записей с идентичностью, редактируемый по одной.
-- Перенумерация после удаления третьей записи сдвинула бы номера четвёртой и
-- пятой, и второй клиент, показывающий список с прошлой минуты, удалил бы
-- не ту. AUTOINCREMENT добавляет к этому «номера не переиспользуются»
-- средствами самого первичного ключа — тот же довод, по которому арбитром id
-- чатов назначен он же (`claim_agent_id`).
--
-- `kind` — какого рода знание: профиль, решение, знание. Колонкой, а не
-- префиксом в тексте: по нему подписана строка врезки, и разбирать текст
-- обратно пришлось бы и врезке, и вкладке.
--
-- Индекса нет: таблица читается только целиком — отбирать не по чему,
-- а сортировка идёт по первичному ключу.
CREATE TABLE IF NOT EXISTS memory (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    content TEXT NOT NULL,
    at      REAL NOT NULL
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


def _branch_row(parent_id, forked_at) -> dict | None:
    """Родство в том виде, в каком его ждут список слева и панель: от кого
    и сколько сообщений унесено. Одна форма на оба чтения — по строке и
    списком: разъедься они, пометка ветки в списке и в панели назвала бы
    разные числа."""
    if parent_id is None:
        return None
    return {"parent_id": parent_id, "forked_at": forked_at}


def _memory_row(row: sqlite3.Row) -> dict:
    """Запись долговременной памяти в том виде, в каком её ждут и ручка,
    и врезка в промпт. Одна форма на все чтения: разъедься они, вкладка
    показывала бы одно, а в модель уезжало бы другое."""
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "content": row["content"],
        "at": row["at"],
    }


def _working_row(row: sqlite3.Row) -> dict:
    """Запись рабочей памяти в том виде, в каком её ждут и ручка, и врезка
    в промпт, и извлечение. Одна форма на все чтения — довод тот же, что
    у `_memory_row`: разъедься они, вкладка показывала бы одно, а в модель
    уезжало бы другое."""
    return {
        "seq": row["seq"],
        "kind": row["kind"],
        "content": row["content"],
        "author": row["author"],
        "at": row["at"],
    }


MIN_SECRET_LENGTH = 16
"""Короче этого значение ключом не считается и не вырезается: редакция работает
подстрокой и чистит **любой** строковый параметр, а с ключом в один символ
изрезала бы `ag_00001` в `ag_***0000***`. Настоящий ключ OpenRouter — 73 символа."""


def redact(value):
    """Вырезает ключ OpenRouter из всего, что уезжает в базу: в конфиг он
    не попадает по построению, но `extra_body` приходит от клиента, да и
    в реплику его можно вставить, перепутав окно. Репозиторий публичный."""
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
    """База занята другим процессом дольше `busy_timeout`. Отдельный класс,
    а не голый `sqlite3.OperationalError`: наверху из него делают 503
    с объяснением — ситуация штатная, а не поломка."""


def _busy(exc: sqlite3.OperationalError, path) -> StoreBusyError | None:
    """Переводит «database is locked» в человеческий текст. None — ошибка
    не про блокировку: «no such table» это баг схемы, и подменять его
    успокаивающим текстом нельзя."""
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
    """Соединение, которое чистит строковые параметры любого запроса. Записать
    можно только через транзакцию, а она отдаёт эту обёртку: обещание держится
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
    """Файл базы плюс несколько запросов к нему. Один на процесс: соединение
    живёт до `close()` — открывать файл на каждую реплику значит терять
    WAL-кеш и упираться в блокировки. Запросы идут под общим `RLock`: писать
    могут и цикл событий, и рабочий поток TestClient."""

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
        conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL: читатель не ждёт писателя. Побочный эффект — файлы -wal и -shm
        # рядом с базой, и .gitignore обязан ловить их тоже.
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
        """Заводит сессию или обновляет её конфиг; `created_at` не перетирается.

        Конфиг едет одним JSON-полем целиком — это `asdict(spec)`, а не список
        колонок, который надо не забыть дополнить: новое поле `AgentSpec`
        сохраняется само.
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

        `limit=None` — все до одной, и это режим по умолчанию: по этому списку
        строится список слева, а он не вправе молча что-то скрывать.
        """
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
            # Происхождение приезжает тем же запросом, а не по строке на чат:
            # список слева рисуется по нему целиком, и пометка ветки обязана
            # стоить столько же, сколько имя чата.
            data["branch"] = _branch_row(row["branch_parent_id"], row["branch_forked_at"])
            out.append(data)
        return out

    def count_sessions(self) -> int:
        with self.reading() as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def delete_session(self, session_id: str) -> bool:
        """Стирает чат вместе с репликами, сводками и рабочей памятью, все
        таблицы одной транзакцией. False — его и не было.

        Внешних ключей в схеме нет, каскада тоже: не вычистишь `summaries`,
        `working_memory`, `working_state` и `branches` руками — сводка,
        цели и происхождение удалённого разговора достанутся чату с тем же id.

        Долговременная память (`memory`) здесь не трогается намеренно: чат ей
        не владелец, а читатель, и слой переживает и удаление чата, и `forget()`.

        Своя строка в `branches` уносится, а строки **потомков** — нет:
        удаление родителя ветку не удаляет, она самостоятельный чат со своей
        историей. Пометка в ней остаётся честной и после: чат, от которого
        она отделилась, называется по id, а имени у него больше нет.
        """
        with self.tx() as conn:
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM working_memory WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM working_state WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM branches WHERE session_id = ?", (session_id,))
            return bool(cursor.rowcount)

    def clear(self) -> None:
        """Стирает базу целиком, включая `meta`, сводки, рабочую память, родство
        и долговременную память. Нужно только проверкам: оставленный счётчик
        имён отдал бы следующей номер посередине, оставленная сводка — чужое
        начало разговора, а оставленная строка родства сделала бы свежий чат
        веткой мёртвого.

        Память здесь **обязана** стираться, хотя удаление чата её не трогает:
        это не «ещё одна таблица чата», а вся база разом. `kill_all()` зовёт
        `clear()` перед каждой проверкой — забытая строка утекла бы из одной
        проверки в другую врезкой в промпт и сдвинула бы там роли.
        """
        with self.tx() as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM summaries")
            conn.execute("DELETE FROM working_memory")
            conn.execute("DELETE FROM working_state")
            conn.execute("DELETE FROM branches")
            conn.execute("DELETE FROM memory")
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM meta")

    # --- сообщения -----------------------------------------------------------

    def save_history(self, session_id: str, turns) -> None:
        """Переписывает историю чата целиком, одной транзакцией.

        Номера расставляются заново от нуля: откат обмена и снятая
        перегенерацией пара укорачивают историю, и «дописать хвост» оставил бы
        дыры в нумерации. Рассуждение не пишется — в контекст оно
        не возвращается; метрики пишутся, из них считается сводка по чату.
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

    # --- сводки начала разговора ---------------------------------------------

    def save_summaries(self, session_id: str, summaries) -> None:
        """Переписывает сводки чата целиком, одной транзакцией.

        Дисциплина ровно как у истории (`save_history`): `DELETE` плюс
        `INSERT` заново с номерами от нуля — `seq` не получает дыр, а
        оборванная запись откатывается вся. Пустой список стирает сводки
        и ничего не пишет: это и есть очистка на `forget()`.
        """
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
        """Сводки чата по порядку сворачивания. Лежат отдельно от истории,
        поэтому перезапись истории их не трогает."""
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
    #
    # Записи, а не снимок: каждая правится, удаляется и пережидает чужие правки
    # по своему номеру. Поэтому и методов четыре штуки по одной операции, а не
    # один «переписать целиком»: перезапись списка — ровно та форма, из-за
    # которой правка человека жила бы до первого извлечения.

    def list_working(self, session_id: str) -> list[dict]:
        """Рабочая память чата по порядку номеров.

        Порядок — по первичному ключу, то есть по времени появления записи:
        правка содержимого записи с места её не двигает, и список во вкладке
        не перетасовывается от каждого служебного вызова.
        """
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT seq, kind, content, author, at FROM working_memory "
                "WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [_working_row(row) for row in rows]

    def add_working(
        self, session_id: str, kind: str, content: str, author: str, at: float | None = None
    ) -> dict:
        """Добавляет запись и отдаёт её целиком — вместе с номером от базы.

        Номер приезжает через `RETURNING`, а не вторым запросом: между
        `INSERT` и `SELECT max(seq)` пролез бы второй писатель, и вызывающий
        получил бы чужой номер под своим текстом. Довод и прецедент те же,
        что у `add_memory` и `next_counter`.

        Отдаётся **записанное**, а не присланное: параметры едут через `tx()`,
        и `redact()` чистит их по дороге.
        """
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO working_memory (session_id, kind, content, author, at) "
                "VALUES (?, ?, ?, ?, ?) "
                "RETURNING seq, kind, content, author, at",
                (session_id, kind, content, author, stamp),
            ).fetchone()
        return _working_row(row)

    def update_working(
        self, session_id: str, seq: int, *, kind: str, content: str, author: str,
        at: float | None = None,
    ) -> dict | None:
        """Переписывает одну запись по номеру. `None` — записи с таким номером
        в этом чате нет.

        Номер в `WHERE` вместе с `session_id`: номера сквозные на всю базу,
        и запрос без чата правил бы запись соседнего разговора по номеру,
        который в этом никогда не выдавался.

        Значения приходят все сразу, а не «только изменённые»: у вызывающего
        запись уже на руках (он же её и показывает), и `COALESCE` в запросе
        завёл бы второе место, где решается, чем пустое поле отличается
        от неназванного.
        """
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "UPDATE working_memory SET kind = ?, content = ?, author = ?, at = ? "
                "WHERE seq = ? AND session_id = ? "
                "RETURNING seq, kind, content, author, at",
                (kind, content, author, stamp, int(seq), session_id),
            ).fetchone()
        return None if row is None else _working_row(row)

    def delete_working(self, session_id: str, seq: int) -> bool:
        """Стирает одну запись. False — записи с таким номером в этом чате не было.

        Номер после этого не достанется никому: у `seq` стоит AUTOINCREMENT.
        Разница видна на **последней** записи — обычная `INTEGER PRIMARY KEY`
        сняла бы номер с удалённой и отдала его следующей, и вторая вкладка,
        показывающая список с прошлой минуты, удалила бы по нему запись,
        которой в тот момент ещё не было.
        """
        with self.tx() as conn:
            cursor = conn.execute(
                "DELETE FROM working_memory WHERE seq = ? AND session_id = ?",
                (int(seq), session_id),
            )
            return bool(cursor.rowcount)

    def load_working_state(self, session_id: str) -> dict:
        """Докуда извлечение дочитало историю этого чата и во что обошлось.

        Строки нет — состояние пустое, но не `None`: «ещё ни разу не
        извлекали» это рабочее состояние, а не отсутствие данных, и звать
        его надо так же, как свежий чат.
        """
        with self.reading() as conn:
            row = conn.execute(
                "SELECT upto, metrics, at FROM working_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return {"upto": 0, "metrics": None, "at": None}
        return {
            "upto": row["upto"],
            "metrics": _loads(row["metrics"], None) if row["metrics"] else None,
            "at": row["at"],
        }

    def save_working_state(self, session_id: str, *, upto: int, metrics=None, at=None) -> None:
        """Записывает состояние извлечения. Строка на чат одна, поэтому
        `ON CONFLICT`: у второго вызова обновлять, а не падать первичным
        ключом."""
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO working_state (session_id, upto, metrics, at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    upto    = excluded.upto,
                    metrics = excluded.metrics,
                    at      = excluded.at
                """,
                (
                    session_id,
                    int(upto),
                    _dumps(metrics) if metrics else None,
                    time.time() if at is None else at,
                ),
            )

    def clear_working(self, session_id: str) -> None:
        """Стирает рабочую память чата целиком — и записи, и состояние
        извлечения, одной транзакцией.

        Обе таблицы разом, потому что забытый разговор не вправе оставить
        ни того, ни другого: оставленные записи рассказали бы следующему
        разговору его цели, а оставленный `upto` дал бы срезать начало,
        которого память никогда не видела.
        """
        with self.tx() as conn:
            conn.execute("DELETE FROM working_memory WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM working_state WHERE session_id = ?", (session_id,))

    # --- происхождение чата ---------------------------------------------------

    def save_branch(self, session_id: str, *, parent_id: str, forked_at: int, at=None) -> None:
        """Записывает, чей этот чат потомок и сколько первых сообщений унёс.

        Идёт через `tx()`, как любая запись: обёртка чистит строковые
        параметры, и `parent_id` попадает под `redact()` наравне с репликой.

        `ON CONFLICT` здесь ради идемпотентности записи, а не ради второго
        происхождения: id у ветки свежий, занят базой минуту назад, и
        переписывать эту строку некому — одно происхождение на чат стережёт
        первичный ключ.
        """
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
        """Происхождение чата или `None` — чат заведён сам по себе.

        Имени родителя здесь нет намеренно: имя меняют из списка слева, и
        копия рядом с родством разошлась бы с ним на первом же
        переименовании. Наружу уезжает id, а имя по нему находит тот, кто
        рисует список, — у него все чаты и так на руках.
        """
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
        """Добавляет запись в долговременную память и отдаёт её целиком —
        вместе с номером, который выдала база.

        Номер приезжает через `RETURNING`, а не вторым запросом (прецедент —
        `next_counter`): между `INSERT` и `SELECT max(seq)` пролез бы второй
        писатель, и клиент получил бы чужой номер под своим текстом.

        Отдаётся то, что **записалось**, а не то, что пришло: параметры едут
        через `tx()`, и `redact()` чистит их по дороге — значит, ответ ручки
        обязан показывать уже чистый текст, а не исходный.
        """
        stamp = time.time() if at is None else at
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO memory (kind, content, at) VALUES (?, ?, ?) "
                "RETURNING seq, kind, content, at",
                (kind, content, stamp),
            ).fetchone()
        return _memory_row(row)

    def list_memory(self) -> list[dict]:
        """Вся долговременная память по порядку добавления.

        Целиком и всегда: слой глобальный, отбирать не по чему — ни чата,
        ни владельца у записи нет, а сортировка идёт по первичному ключу.
        """
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT seq, kind, content, at FROM memory ORDER BY seq"
            ).fetchall()
        return [_memory_row(row) for row in rows]

    def delete_memory(self, seq: int) -> bool:
        """Стирает одну запись памяти. False — записи с таким номером не было.

        Номер после этого не достанется никому: у `seq` стоит AUTOINCREMENT.
        Разница видна на **последней** записи — обычная `INTEGER PRIMARY KEY`
        сняла бы номер с удалённой и отдала его следующей, и вторая вкладка,
        показывающая список с прошлой минуты, удалила бы по нему запись,
        которой в тот момент ещё не было.
        """
        with self.tx() as conn:
            cursor = conn.execute("DELETE FROM memory WHERE seq = ?", (int(seq),))
            return bool(cursor.rowcount)

    # --- meta: счётчики, общие на всю базу -----------------------------------

    def next_counter(self, key: str) -> int:
        """Следующее число счётчика из базы, никогда не повторяющееся.

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
        по `session_id` его первая строчка. Нарушение ограничения в SQLite
        откатывает только сам запрос, поэтому цикл живёт и внутри `tx()`.
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
