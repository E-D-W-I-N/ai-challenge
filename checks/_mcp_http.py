"""Independent slow HTTP MCP fixture for connection/call races."""
import argparse
import asyncio
from mcp.server.fastmcp import FastMCP

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    server = FastMCP("fixture", port=args.port)

    @server.tool()
    async def ping(text: str = "", delay: float = 0) -> str:
        await asyncio.sleep(delay)
        return "pong " + text

    @server.tool(meta={"host_only": True, "hostbinding": "fixture"})
    def host_probe() -> str:
        return "host only"

    server.run(transport="streamable-http")
