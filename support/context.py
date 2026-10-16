"""Read-only JSON adapter: the operator binds one ticket, never the model."""
import argparse
import httpx
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from shared_models.transport import client_options, create_client

MAX_CONTEXT = 24000


def bounded(value, limit=MAX_CONTEXT):
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode()) > limit:
        raise ValueError('Context exceeds explicit byte limit')
    return text


def selected_context(path, ticket_id):
    try:
        if Path(path).stat().st_size > 1024 * 1024:
            raise ValueError()
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if set(data) != {'users', 'tickets'}:
            raise ValueError()
        users, tickets = data['users'], data['tickets']
        if not isinstance(users, list) or not isinstance(tickets, list):
            raise ValueError()
        ids = set()
        for user in users:
            if not isinstance(user, dict) or set(user) != {'id', 'context'}:
                raise ValueError()
            if not isinstance(user['id'], str) or not user['id'] or user['id'] in ids or not isinstance(user['context'], str):
                raise ValueError()
            ids.add(user['id'])
        seen = set()
        for ticket in tickets:
            if not isinstance(ticket, dict) or set(ticket) != {'id', 'user_id', 'title', 'description', 'status', 'messages'}:
                raise ValueError()
            if any(not isinstance(ticket[k], str) for k in ('id', 'user_id', 'title', 'description', 'status')):
                raise ValueError()
            if not ticket['id'] or ticket['id'] in seen or ticket['user_id'] not in ids:
                raise ValueError()
            if not isinstance(ticket['messages'], list) or any(not isinstance(m, dict) or set(m) != {'author', 'text'} or any(not isinstance(v, str) for v in m.values()) for m in ticket['messages']):
                raise ValueError()
            seen.add(ticket['id'])
        matches = [t for t in tickets if t['id'] == ticket_id]
        if len(matches) != 1:
            raise ValueError()
        ticket = matches[0]
        result = {'ticket': ticket, 'user': next(u for u in users if u['id'] == ticket['user_id'])}
        bounded(result)
        return result
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        raise ValueError('Invalid support JSON, unknown ticket or context limit exceeded') from None


async def fetch_context(url, ticket_id):
    try:
        def http_client(headers=None, timeout=None, auth=None):
            return create_client(httpx.AsyncClient, **client_options(url), headers=headers, timeout=timeout, auth=auth, follow_redirects=False)
        async with streamablehttp_client(url, timeout=15, sse_read_timeout=15, httpx_client_factory=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                if len(tools) != 1 or tools[0].name != 'selected_context' or tools[0].inputSchema.get('properties', {}):
                    raise ValueError()
                result = await session.call_tool('selected_context', {})
                if result.isError:
                    raise ValueError()
                value = result.structuredContent
                if value is None:
                    if len(result.content) != 1 or result.content[0].type != 'text':
                        raise ValueError()
                    value = json.loads(result.content[0].text)
                if not isinstance(value, dict) or set(value) != {'ticket', 'user'}:
                    raise ValueError()
                if value['ticket']['id'] != ticket_id or value['ticket']['user_id'] != value['user']['id']:
                    raise ValueError()
                bounded(value)
                return value
    except Exception:
        raise ValueError('MCP context unavailable, invalid or bound to a different ticket') from None


def main():
    parser = argparse.ArgumentParser(description='Read-only, single-ticket MCP support context')
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--ticket', required=True)
    parser.add_argument('--port', type=int, default=8010)
    args = parser.parse_args()
    selected_context(args.data, args.ticket)
    server = FastMCP('Support context', host='127.0.0.1', port=args.port, json_response=True)

    @server.tool()
    def selected_context_tool() -> dict:
        """Fresh context for the operator-selected ticket and its owner only."""
        return selected_context(args.data, args.ticket)

    # Stable public name, with no model-selectable ticket/user parameters.
    server.remove_tool('selected_context_tool')
    server.add_tool(selected_context_tool, name='selected_context')
    server.run(transport='streamable-http')


if __name__ == '__main__':
    main()
