"""Independent echo MCP service, with no application imports."""
import argparse
from mcp.server.fastmcp import FastMCP


def main():
    parser = argparse.ArgumentParser(description="Echo MCP service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8016)
    parser.add_argument("--stdio", action="store_true", help="Explicit legacy fixture mode")
    args = parser.parse_args()
    server = FastMCP("echo", host=args.host, port=args.port)

    @server.tool(description="Проверка связи: отвечает «pong» и повторяет присланный текст")
    def ping(text: str = "") -> str:
        return "pong" + (f" {text}" if text else "")

    server.run(transport="stdio" if args.stdio else "streamable-http")


if __name__ == "__main__":
    main()
