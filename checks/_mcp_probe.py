"""Зонд вместо MCP-сервера: отвечает на initialize ошибкой со списком
своих переменных окружения в тексте.

По тексту проверяется белый список env дочерних процессов: наследуй
менеджер `os.environ` целиком — в списке оказался бы OPENROUTER_API_KEY.
А самим отказом — что упавший на рукопожатии сервер это down, а не падение
приложения. Не молчать, а отвечать ошибкой — чтобы проверка не ждала таймаут.
"""

import json
import sys

for line in sys.stdin:
    try:
        req = json.loads(line)
    except ValueError:
        continue
    if req.get("method") == "initialize":
        import os

        json.dump(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "error": {
                    "code": -32000,
                    "message": "env: " + json.dumps(sorted(os.environ)),
                },
            },
            sys.stdout,
        )
        # Сообщения — строками: без перевода строки ответ не доедет никогда.
        sys.stdout.write("\n")
        sys.stdout.flush()
        break
