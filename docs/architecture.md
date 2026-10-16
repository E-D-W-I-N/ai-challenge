# Архитектура дня 31

Python >=3.11; без web UI и сборки. `assistant` — CLI, `rag` — независимый
SQLite RAG, `shared_models` — совместимый HTTP contract и system proxy,
`services.git` — отдельно запущенный read-only HTTP MCP. Прямые зависимости:
закреплённые `httpx[socks]` и официальный `mcp` SDK.

## Запрос и контекст

Обычный текст и `/help <вопрос>` идут одним маршрутом: fixed MCP tool
`git_project_context` → query embedding → cosine retrieval → Chat Completions.
MCP возвращает корень worktree, branch (null при detached HEAD), HEAD (null до
первого коммита) и tracked porcelain status; никаких shell команд от модели.
Клиент проверяет соответствие корня выбранному проекту. Ошибка MCP, retrieval,
HTTP или формата завершает запрос без retries/fallback и добавления в историю.
При отсутствии индекса требуется `/index`; ошибки не маскируются Git-ответом.

System prompt требует отличать текущие Git-факты от документации, считать
документы и историю недоверенными данными, отвечать на текущий вопрос и
указывать пробелы. Последние три успешные пары живут только в памяти процесса;
`/new` их очищает. Документация ниже `min_score` не включается, но модель может
ответить по Git-фактам с явным ограничением. Перед генерацией CLI сообщает
отсутствие совпадений. Источники, переданные модели, печатаются с file:lines,
section и cosine; семантической проверки ответа/цитат нет. Печатается только
фактический token usage, если сервер его сообщил.

## Индекс

`git ls-files --cached -z` определяет tracked имена; читается текущее содержимое
worktree, включая незакоммиченные изменения. README, docs, schema/openapi/swagger
по default globs; расширения ограничены Markdown, rst, txt, JSON/YAML, SQL,
GraphQL и proto. Includes/excludes задаются списками glob. Скрытые пути,
symlinks, известные secret имена, data/files, vendor/build и бинарные файлы
исключаются независимо от includes. UTF-8, до 2 MiB на документ. Это ограничение
сканера, а не обещание распознать любой секрет: перед индексацией проверьте
состав tracked документации. Неподходящие файлы пропускаются целиком.

Markdown headings вне fenced blocks задают разделы; длинные разделы разбиваются
на окна `chunk_lines`, без overlap. Каждый чанк содержит source/title/start/end,
текст и SHA256. Один индекс хранит метаданные, чанки и embeddings. Identity:
версия алгоритма, embedding model ID, chunk configuration; dimensions проверяются.
URL не является identity модели: оператор отвечает за одинаковый model ID на
другом сервере. Query использует модель опубликованного индекса; изменение
model/chunk settings требует явной перестройки.

`/index` держит один advisory writer lock, переиспользует совпавшие chunk hashes
при одинаковой identity, встраивает только недостающие тексты батчами по 32,
убирает удалённые документы и публикует временный SQLite через atomic replace.
Отказ сохраняет прежний индекс; readers удерживают прежний inode до конца
retrieval. Изменения документов/globs помечают индекс stale: запрос предупреждает
и использует опубликованный снимок. `/status` не вызывает inference.

## Подключение, секреты и состояние

Корень проекта определяется Git; по умолчанию cwd, `--project` выбирает другой.
State по умолчанию `~/.local/share/project-assistant/<SHA256 пути[:24]>`;
`--state` переопределяет его, но state внутри проекта запрещён. Binding к полному
пути не позволяет использовать state другого проекта. State directory 0700,
settings/index 0600. Config сохраняется atomic replace и содержит URL, optional
API key, два независимых model IDs, MCP URL, generation/retrieval параметры.
История на диск не пишется. Состояние старого приложения не читается/не мигрируется.

`/settings` собирает новый config, проверяет `/models` на новом URL и сохраняет
только полностью валидный результат. ID выбирается номером каталога или точным
ручным именем, типы моделей не угадываются. Пустой ID остаётся пустым до выбора.
API key вводится скрыто, Enter сохраняет, `-` удаляет. Нет dotenv/env key loader.
Все inference-вызовы только по явному `/index` или вопросу. Generation и embeddings
используют один compatible URL с независимыми model IDs. Стандартный `/embeddings`
не получает reasoning controls. Generation после параметров применяет
`reasoning_effort=none|medium`; default OFF. Наблюдаемое reasoning при OFF — отказ;
отсутствие evidence не доказывает внутреннее поведение любого сервера.

Model HTTP и MCP используют shared proxy resolver: системные HTTP/HTTPS proxy
и исключения либо стандартный proxy environment; loopback всегда direct.
SOCKS5 поддерживается; автоматические PAC/system SOCKS не обнаруживаются.
Нет direct fallback при сломанном proxy. Модельный HTTP timeout 60s,
response bound 16 MiB, redirects выключены; ошибки не копируют body/URL/ключи.
MCP timeout 20s; сервис запускается человеком, по умолчанию loopback:8017.
Удалённый MCP не имеет встроенной авторизации — сеть и доступ настраивает оператор.

## Проверки

`make check` — один компактный offline сценарий: временные Git/documents/state,
модельный loopback HTTP stub, настоящий HTTP MCP процесс, CLI с подменённым вводом.
Проверяются exclusions, line metadata, cache reuse/removal/stale, atomic failure,
reasoning OFF, identity mismatch, detached HEAD и отсутствие inference на help.
Никакого пользовательского корпуса, живой модели или коллекции вопросов.
Качество ответов, совместимость реального сервера и удобство диалога проверяет
человек вручную по client-guide.
