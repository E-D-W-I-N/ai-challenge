# CLAUDE.md

Чат-клиент поверх OpenRouter: FastAPI, ванильный JS без сборки, SQLite из
стандартной библиотеки. Зависимостей три — `fastapi`, `uvicorn`, `httpx`; в
клиент не тянется из сети ничего: ни шрифтов, ни библиотек, ни иконок.

## Среда

Системный `python3` — это 3.9: без зависимостей он падает сразу на
`ModuleNotFoundError: fastapi`, а с ними часть проверок разваливается на
отсутствии текущего event loop. Везде только `/opt/homebrew/bin/python3.11`.

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000   # http://127.0.0.1:8000
```

## Безопасность

Репозиторий публичный, а в `.env` (`cp .env.example .env`, файл в `.gitignore`)
лежит `OPENROUTER_API_KEY`. Ключ, попавший в коммит, отозвать уже нельзя:
**никогда `git add -A` и `git add .`** — только точечно перечисляя файлы.
Живых вызовов к модели не делать: в проверках `stream_completion` подменяется
заглушкой (`checks/_stub.py`), ключ ей не нужен.

## Проверки

```bash
.venv/bin/python checks/run_checks.py     # 22 проверки, включая три ниже и клиентскую
.venv/bin/python checks/spawn_100.py      # сто агентов в одном процессе
.venv/bin/python checks/restart.py        # два процесса подряд на одном файле базы
.venv/bin/python checks/two_processes.py  # одновременные писатели: id не пересекаются
node checks/browser_check.js              # клиент под node, 98 утверждений
```

## Что нельзя сломать

* `provider.require_parameters` — в теле каждого вызова (`build_payload`, `app/llm.py`);
* плагин `context-compression` выключен там же и тоже на каждом вызове: иначе на окнах 8k OpenRouter молча выбрасывает середину разговора;
* `usage: {"include": true}` запрошен — токены и цену называет провайдер, своих оценок нет;
* `BEGIN IMMEDIATE` в начале транзакции (`app/store.py`); занятая база — 503, а не 500;
* `redact()` чистит любой строковый параметр любого запроса: транзакция отдаёт обёртку, а не соединение;
* PK `(session_id, seq)`, история пишется целиком одной транзакцией, `seq` от нуля и без дыр;
* в модель уезжает вся история (`Agent.build_prompt`) — окна памяти нет;
* сумма по чату считается на сервере (`Agent.usage_summary`), клиент только показывает `usage_total`.

## Про проверки

Бьют по поведению, а не по исходнику. Греп по клиенту — не проверка клиента:
`checks/browser_check.js` исполняет настоящий `app/static/app.js` под node на
стенде `checks/dom.js` (минимальный DOM и сервер с настоящими кадрами SSE).
Мета-проверок не заводить: ни таблиц запрещённых слов, ни самопроверок стенда,
ни проверок про проверки. Проверок меньше, чем продукта: 2.9k строк против 5.0k.
