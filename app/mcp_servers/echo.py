"""Эхо-сервер: один инструмент `ping`, возвращает «pong» и эхо текста.

Сервер-стенд: по нему видно, что соединение установлено, а список
инструментов доехал целиком — с описанием и схемой параметров.
"""

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo")


@server.tool(
    description="Проверка связи: отвечает «pong» и повторяет присланный текст"
)
def ping(text: str = "") -> str:
    return "pong" + (f" {text}" if text else "")


if __name__ == "__main__":
    server.run()  # stdio: менеджер говорит с процессом по stdin/stdout
