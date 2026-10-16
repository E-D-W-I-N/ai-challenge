"""Explicit HTTP MCP connection; one fixed tool call per question."""
import asyncio
import json
from pathlib import Path
import httpx
from shared_models.transport import client_options, create_client

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def context(url, project):
    try:
        async with asyncio.timeout(20):
            async with create_client(httpx.AsyncClient, **client_options(url), timeout=20, follow_redirects=False) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool('git_project_context', {})
                        if result.isError: raise ValueError()
                        value = result.structuredContent
                        if value is None:
                            value = json.loads(''.join(c.text for c in result.content if c.type == 'text'))
                        if not isinstance(value, dict) or Path(value['project']).resolve()!=project: raise ValueError()
                        if any(value.get(k) is not None and not isinstance(value[k], str) for k in ['branch','head','status']): raise ValueError()
                        if not {'branch','head','status'} <= value.keys(): raise ValueError()
                        return value
    except (Exception, BaseExceptionGroup):
        raise ValueError('Git MCP unavailable, invalid response or wrong repository; check service --repo and /settings URL') from None
