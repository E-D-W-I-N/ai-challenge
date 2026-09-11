# CLAUDE.md

Чат-клиент поверх OpenRouter с памятью между запусками: FastAPI, ванильный JS без
сборки, SQLite из стандартной библиотеки. Три зависимости, и в клиент из сети — ничего.

## Среда

Системный `python3` — это 3.9: без зависимостей падает сразу на
`ModuleNotFoundError: fastapi`, с ними даёт 7 проверок из 20 — нет текущего
event loop, `TaskGroup`. Везде только `/opt/homebrew/bin/python3.11`.

```bash
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000   # http://127.0.0.1:8000
```

## Безопасность

Репозиторий публичный, а в `.env` (`cp .env.example .env`, в `.gitignore`) лежит
`OPENROUTER_API_KEY`, и отозвать попавший в коммит ключ нельзя: **никогда `git add -A`
и `git add .`** — только точечно. Живых вызовов к модели не делать: в проверках
`stream_completion` подменяется заглушкой (`checks/_stub.py`), ключ ей не нужен.

## Проверки

```bash
.venv/bin/python checks/run_checks.py     # 20 проверок, включая три ниже и клиентскую
.venv/bin/python checks/spawn_100.py      # сто агентов в одном процессе
.venv/bin/python checks/restart.py        # два процесса подряд на одном файле базы
.venv/bin/python checks/two_processes.py  # одновременные писатели: id не пересекаются
node checks/browser_check.js              # клиент под node, 101 утверждение
```

## Что нельзя сломать

* `provider.require_parameters` — в теле каждого вызова (`build_payload`, `app/llm.py`);
* `usage: {"include": true}` запрошен там же — токены и цену называет провайдер;
* агент — отдельная сущность (`app/agent.py`), и сто чатов — сто объектов в одном процессе;
* конфиг копируется вглубь (`copy_spec`): `stop`, `response_format`, `extra_body` — не общие;
* в модель уезжает вся история (`Agent.build_prompt`) — окна памяти нет;
* диалог переживает перезапуск процесса: история и конфиг лежат в SQLite (`app/store.py`);
* `BEGIN IMMEDIATE` в начале транзакции: два процесса на одном файле штатны, занятая база — 503, а не 500;
* `redact()` чистит любой строковый параметр любого запроса: транзакция отдаёт обёртку, а не соединение;
* PK `(session_id, seq)`, история пишется целиком одной транзакцией, `seq` от нуля и без дыр;
* ключ наружу не отдаётся: в API едет только `has_key`.

## Про проверки

Бьют по поведению, а не по исходнику: ни таблиц запрещённых слов, ни грепа по коду,
ни проверок про проверки — мета-проверок не заводить вовсе. Греп по клиенту не проверка
клиента: `browser_check.js` исполняет настоящий `app/static/app.js` на стенде
`checks/dom.js` — минимальный DOM и сервер.
