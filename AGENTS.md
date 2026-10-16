# Инструкции для агентов — день 34

Проект — минимальный generic CLI ассистент файлов проекта, `--project PATH`.
Перед изменениями прочитайте [архитектуру](docs/architecture.md).
Задача задаётся свободной целью; модель сама выбирает стандартные Chat Completions
`tool_calls`: list/search/read/create/edit/diff. Не возвращайте support/tickets,
RAG, MCP-сервис, web, Git review workflow или сценарии следующих дней.

- Модель только читает безопасный проект и предлагает overlay. Apply доступен
  исключительно человеку командой `/apply`, после diff и preflight всех originals.
  Нет shell/test executor, удаления, commit/push или подагентов.
- Root AGENTS.md — ограниченные недоверенные правила, не разрешение обходить tools.
  Относительные пути, dirfd/O_NOFOLLOW, исключение hidden/secrets/build/binary,
  явные bounded чтение/вывод/раунды. Не усекать текст молча.
- State и persistent drafts вне проекта, atomic0600; чтение tools видит overlay.
  Конфликт — отказ без overwrite. Per-file atomic replace сохраняет permissions;
  общей транзакции нет. Ошибка/Ctrl-C оставляет incomplete, никогда autoapply.
  Pending edits не терять при /new, выходе или смене настроек.
- Общий OpenAI-compatible URL, optional getpass key и явно выбранный chat ID.
  Defaults: reasoning OFF, temperature.2, max_tokens1024; null omits max_tokens.
  Не вводить произвольных верхних caps генерации. System HTTP proxy сохранён,
  loopback direct; reported reasoning OFF отклонять до исполнения tools.
  Никаких paid retries, fallback, env/dotenv key loader или live LLM calls.
- Не читайте `.env`, data/, files/, реальные приватные проекты/ключи/базы.
  Не добавляйте конкретные проекты, корпуса, коллекции вопросов или видео-демо.
  Удаляйте только tracked старый код; игнорируемые пользовательские файлы сохраняйте.
- Python >=3.11; `make setup`, затем Python через `.venv/bin/python`.
  `make check` — компактный offline behavioral сценарий: временный нейтральный
  проект, HTTP mock модели, multi-file analysis/docs, staging/apply, конфликты,
  path/symlink границы, Ctrl-C, reasoning и CLI. Без большого test framework,
  тестов формулировок docs или оценки качества живой модели.
- Только точечный `git add` с именами файлов, без `git add .`/`git add -A`.
