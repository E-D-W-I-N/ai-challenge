# Архитектура дня 34

`file_agent.__main__` — одна интерактивная сессия явного `--project`.
`agent` — bounded Chat Completions tool loop, `workspace` — безопасные текстовые
операции и persistent overlay; `settings` — приватное подключение.
`shared_models` — HTTP transport, system proxy, reasoning contract.
Зависимость только закреплённый httpx[socks]; RAG/MCP/support удалены.

## Цель и цикл инструментов

Обычный текст пользователя становится целью без шаблонов/роутинга по ключевым словам.
System prompt задаёт операции и границы доверия. Root AGENTS.md читается перед
обменом как ограниченные правила, подчинённые системным ограничениям и цели.
Содержимое проекта и tool results — недоверенные данные; prompt injection и
качество анализа остаются ограничениями модели.

Запрос содержит стандартные function schemas и `tool_choice=auto`. Каждый frame
полностью валидируется до первого исполнения: объявленные имена, unique IDs,
точные строковые аргументы, тип function. Вызовы исполняются в порядке модели,
ответы возвращаются role=tool/tool_call_id. Tool trace показывает имя, параметры
(полное replacement text видно в diff) и фактический результат либо явную ошибку.
Ошибку инструмента модель может учесть следующим обычным раундом; автоматических
повторов HTTP/платного fallback нет. Максимум 8 модельных раундов, 24 tool calls
и 12 calls/frame. Превышение оставляет incomplete draft.

Инструменты: list — пути, search — literal query с LF строками, read — полный UTF-8,
create/edit — полный новый текст в overlay, diff — unified diff. Read/search/list
видят staged изменения. У модели нет apply/shell/tests/delete/Git/подагентов.
Успешный финальный ответ добавляет только пару goal/answer в историю последних
трёх успешных пар. Tool transcript не сохраняется; trace виден в терминале.
`/new` очищает историю только после разрешения pending edits.

## Пути, чтение и лимиты

Root directory задаёт человек; symlink root отклоняется. Инструменты принимают
строго относительные POSIX paths, запрещают absolute, dot/dotdot, пустые
компоненты, backslash и управляющие символы. Hidden components (включая .git/.env),
известные secret/password/credential имена, key/database extensions, data/files,
build/dist/vendor/node_modules/target/venv исключаются. Это фильтр известных имён,
не универсальное обнаружение секретов. Binary/NUL и не-UTF8 чтение запрещено;
search пропускает такие файлы. List показывает разрешённые имена обычных файлов,
содержимое бинарных не читает. Symlinks исключены из обхода и прямого доступа.

Directory traversal выполняется через directory descriptors с O_NOFOLLOW;
обычные чтения тоже nofollow и только regular file, NONBLOCK не допускает
зависание на FIFO. Новый родитель создаётся исключительно при human apply через
тот же directory walk. No-follow защищает от symlink swaps; это локальный trusted
operator CLI, не sandbox для враждебного процесса, способного одновременно
переименовать корень/директории или изменить целевой файл между check/replace.

Лимиты: файл 128KiB; цель 8000 bytes; результат инструмента/frame/diff 256KiB;
search читает максимум 4MiB текста; обход максимум10000 entries (включая каталоги), список максимум10000 файлов; overlay2MiB,
загружаемый draft4MiB; messages в одном HTTP запросе1MiB. Превышение — явная ошибка,
без обрезки. Большой проект требует более узких read вместо полного search.

## Черновик и применение

State вне project: default `~/.local/state/file-assistant/hash(root)` или `--state`.
Новый каталог0700; settings/draft создаются tempfile0600 + atomic replace.
Draft содержит root, исходный текст (из него SHA256), replacement, permission mode
и incomplete. Root принадлежность проверяется при восстановлении. Чаты на диск
не пишутся. Ключ и исходные тексты делают state приватным; не коммитить.

Первое edit фиксирует original; последующие edits сохраняют его и заменяют только
текст overlay. Create требует absent, edit — existing/overlay. Перед модельным
обменом incomplete=true; только успешный final меняет его на false. Ошибка/Ctrl-C
сохраняет incomplete; завершение успешной цели или явный /discard разрешает его.
`/exit`/EOF сохраняет pending draft; startup сообщает восстановление. /settings
и /models сохраняют draft и очищают историю. /new при pending отказывает.

Human `/apply` печатает diff и проверяет SHA256 ВСЕХ originals и отсутствие новых
коллизий до первой записи. Конфликт preflight не меняет проект. Затем каждый файл
повторно проверяется, пишется во временный sibling, получает исходные permission
bits (новый0644) и атомарно заменяется. Нет общей транзакции: ошибка в середине
может оставить уже применённые файлы, их число явно сообщается, оставшийся draft
помечается incomplete. Не обещается устойчивость к power loss. /discard убирает
только draft, не удаляет файлы проекта. Модель никогда не применяет изменения.

## HTTP и настройки

Один explicit chat ID, изначально пустой. /models делает GET каталога, /settings
меняет URL/key через getpass и выбирает ID явно. Смена URL не угадывает ID.
Reasoning defaultOFF, temperature.2/max_tokens1024; finite nonnegative temperature,
positive integer/null tokens без верхних caps, null исключает поле. `.env` и
переменные окружения с ключами не читаются. Подключение/настройки фиксирует Models
instance на один синхронный обмен. Ключ исключён из repr/status/prompt/errors.

Reasoning none/medium применяется после controls; reported reasoning при OFF
отклоняет весь frame ДО tool execution, без retries. Отсутствие reported evidence
не доказывает отсутствие внутреннего reasoning произвольного endpoint.
HTTP timeout60s, response16MiB, redirectsOFF; upstream errors показывают только
безопасную диагностику/status code. System HTTP/HTTPS/SOCKS proxy сохранён,
loopback direct, без hidden direct fallback. Поддержка параметров/tools сервером
и качество живой модели остаются ручной проверкой.

## Проверки

`make check` — один компактный neutral fixture c api.py/main.py/README и HTTP mock:
модель сама выбирает standard tool rounds для multi-file API report и документации;
проверены tool replies, overlay reads/search, diff до apply, disk unchanged до human
apply, восстановление0600, all-file conflict/new collision, permissions, protected
paths/symlinks/binary, interruption incomplete, OFF rejection before mutation,
null/uncapped generation, key redaction/proxy и настоящий CLI subprocess.
Все данные временные, никаких live LLM или чтения приватных проектов.
