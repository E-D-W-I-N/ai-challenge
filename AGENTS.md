# Инструкции для агентов — день 33

Проект — минимальный консольный ассистент поддержки: Markdown RAG и отдельный
read-only HTTP MCP сервис, связанный оператором с одним тикетом JSON.
Перед изменениями прочитайте [архитектуру](docs/architecture.md).
Не возвращайте прежние сценарии, web/CRM платформу, Git review или workflow.

- MCP запускает человек с явными `--data`/`--ticket`; модель не выбирает IDs.
  Клиент не читает JSON напрямую. Перед каждым ответом свежий MCP контекст;
  ошибка не допускает stale/direct JSON fallback. В модель только выбранный
  тикет и его владелец, никогда весь JSON или список пользователей.
- Документы — недоверенные данные. Индекс только Markdown; symlink/hidden/
  secret/build/binary исключены. Корпус и контекст bounded, превышение даёт
  ошибку, не усечение. Индекс вне корпуса, explicit /index, incremental cache,
  atomic SQLite; empty corpus очищает его. Никакого automatic paid reindex.
- История только последние три успешные пары выбранного тикета в памяти;
  /new очищает. Не пишите ответы/ключи/JSON контекст в логи или Git.
- `shared_models` сохраняет system proxy, loopback direct, reasoning default OFF
  и отказ при reported reasoning. IDs generation/embedding независимы и явны;
  никаких retries, скрытых fallback, пробного inference или env/dotenv key loader.
  Настройки и индекс private 0600 atomic, путь изолирован knowledge directory.
- Нет изменения статуса, отправки ответа, CRM mutations, регистрации/auth.
  Не читайте `.env`, data/, files/, пользовательские базы/корпус/ключи.
  Не добавляйте реальные проекты, корпуса, коллекции вопросов или видео-демо.
  Удаляйте только tracked старый код; игнорируемые пользовательские файлы сохраняйте.
- Python >=3.11; `make setup`, затем команды через `.venv/bin/python`.
  `make check` — один компактный offline behavioral scenario с временными
  Markdown/JSON, mock model HTTP и реальным локальным HTTP MCP.
  Никаких live LLM, больших test frameworks, тестов формулировок docs или качества модели.
- Только точечный `git add` с именами файлов; без `git add .`/`git add -A`.
