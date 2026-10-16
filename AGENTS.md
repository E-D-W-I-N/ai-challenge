# Инструкции для агентов — день 32

Проект — headless автоматическое advisory PR review для произвольного Git repo.
Перед изменениями прочитайте соответствующие разделы [архитектуры](docs/architecture.md).
Старые CLI chat, MCP и state удалены; пользовательские данные не читаются/мигрируются.

- Только exact full base/head commit SHA; diff base → head, merge base фиксируется.
  Target читается Git blobs, без checkout, execution, hooks, сборки, зависимостей,
  target config. External diff/textconv OFF. Непроверенные refs/shell недопустимы.
- RAG — независимый in-memory пакет: документация И код base, отдельные квоты
  per changed path. Changed head передаётся отдельно. Bounds дают явный отказ,
  не тихую обрезку. Symlink/submodule/secret/build/vendor/binary исключаются.
- Модель не выполняет tools. Один final JSON bugs/architecture/recommendations;
  пустые списки допустимы. IDs/path/line валидируются по фактически переданным
  source chunks. Семантика и качество не проверяются; retry/fallback запрещены.
- `shared_models` сохраняет system proxy, loopback direct, reasoning default OFF
  и отказ при observed reasoning. Нет автоматического выбора модели и пробных
  inference. Local connection file явный, вне target; `.env` не загружается.
  Только CI явно читает переданные Variables/Secret и служебные GitHub env.
- Workflow reusable, caller закрепляет trusted reviewer SHA. Только contents:read
  и pull-requests:read; PR code никогда не checkout/execute. Head/base повторно
  проверяются перед publish. Отчёт advisory, без GitHub comments/записей/merge gate.
- Не читайте `.env`, data/, files/, пользовательские базы/корпус/ключи. Секреты,
  target-specific проекты/корпуса/вопросы и review artifacts не коммитьте.
  Удаляйте только tracked старый код; игнорируемые пользовательские файлы сохраняйте.
- Python >=3.11; `make setup`, затем команды через `.venv/bin/python`.
  `make check` — компактные реальные временные Git commits + HTTP mocks,
  без live LLM/GitHub workflow. Не добавляйте huge frameworks, fixtures с
  пользовательским кодом, тесты формулировок docs или качества модели.
- Только точечный `git add` с именами файлов; без `git add .`/`git add -A`.
