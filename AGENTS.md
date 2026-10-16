# Инструкции для агентов — день 31

Проект — консольный ассистент разработчика для произвольного Git worktree.
Перед изменениями прочитайте [карту](docs/client-guide.md) и относящиеся разделы
[архитектуры](docs/architecture.md). Старый веб-сервис, аккаунты, напоминания,
HTML pipeline и VPS runtime удалены; старые приватные данные не открываются.

- CLI `assistant`: `/help <вопрос>` и обычный вопрос обязательно получают свежие
  Git-факты через отдельный HTTP MCP и документацию из опубликованного RAG.
  Модель не выбирает инструменты и не исполняет произвольные команды.
- `rag` независим от `assistant`; только tracked текстовые документы текущего
  worktree, явный `/index`, структурные ограниченные чанки с путями/строками,
  incremental embeddings cache и атомарная SQLite публикация. Stale виден,
  автоматической переиндексации нет. Низкая релевантность не отменяет Git-факты.
- MCP `services.git` запускает человек с `--repo`; один fixed read-only tool
  возвращает repository identity, branch/HEAD/status. Клиент сверяет identity.
- URL+optional key и независимые generation/embedding IDs живут вне проекта
  в приватном state, конфиг атомарный 0600. Ключ вводится getpass, не печатается;
  `.env` и env keys не загружаются. `/settings` проверяет только `/models`.
- `shared_models` сохраняет system proxy, loopback direct и reasoning contract.
  Reasoning default OFF; observed reasoning при OFF отклоняется. Нет retries,
  fallback, автоматического выбора модели или скрытых inference-вызовов.
- Не читайте `.env`, data/, files/, пользовательские базы/корпус/ключи.
  Не включайте их в Git или отчёты. Удаление старого кода затрагивает только
  tracked файлы; игнорируемые файлы пользователя сохраняются.
- Python >=3.11, зависимости закреплены. `make setup`, затем Python только через
  `.venv/bin/python`. `make check` использует временный Git-проект, локальные
  HTTP/MCP fixtures; живые LLM вызовы запрещены. Проверяйте поведение компактно,
  не добавляйте большие матрицы, тесты формулировок документации и live corpus.
- Только точечный `git add` с именами файлов, без `git add .`/`git add -A`.
