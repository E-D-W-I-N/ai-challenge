# Независимый echo MCP-сервис

Сервис содержит только MCP SDK и `ping(text="") → "pong [text]"`.
Он не импортирует приложение, Agent, Store или LLM, не открывает базу чатов,
не использует ключ модели. Запускайте его вручную в отдельном терминале.

Из корня репозитория (Python >=3.11):

```bash
python3.11 -m venv /private/tmp/echo-mcp-venv
/private/tmp/echo-mcp-venv/bin/python -m pip install -r services/echo/requirements.txt
/private/tmp/echo-mcp-venv/bin/python -m services.echo --port 8016
```

Стандартный Streamable HTTP endpoint: `http://127.0.0.1:8016/mcp`.
В приложении: «Настройки → Инструменты», произвольное имя, этот URL,
«Сохранить URL», «Подключить». Порт меняется через `--port`; host — `--host`
(по умолчанию localhost). Ctrl+C останавливает сервис. Приложение закрывает
только свою MCP-сессию; disconnect/shutdown не останавливают этот процесс.

Для явной совместимости с локальными stdio-фикстурами:

```bash
/private/tmp/echo-mcp-venv/bin/python -m services.echo --stdio
```

Legacy module `app.mcp_servers.echo` — тонкий stdio entry point, не реализация
сервиса. Штатный запуск приложения не запускает ни один из этих процессов.
