"""Interactive console support assistant; MCP is launched separately."""
import argparse
import asyncio
from pathlib import Path

from shared_models import validate_url
from shared_models.client import Models
from support.assistant import Assistant
from support.context import fetch_context
from support.settings import configure, load, save, state_directory
from rag.index import build, status

HELP = '/settings /models /index /status /new /help /exit; otherwise enter a support question'


async def run(args):
    root = args.knowledge.resolve(strict=True)
    state = state_directory(root, args.state)
    settings_path, index = state / 'settings.json', state / 'index.sqlite'
    settings = load(settings_path)
    url = validate_url(args.mcp_url)
    # Check trusted launch binding before accepting questions; every answer refreshes it.
    await fetch_context(url, args.ticket)
    assistant = Assistant(root, index, args.ticket, url, Models(settings))
    print(f'Ticket: {args.ticket}; knowledge: {root}; private state: {state}')
    print(HELP)
    while True:
        try:
            question = input('support> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        try:
            if question == '/exit': return
            if question == '/help': print(HELP)
            elif question == '/new': assistant.history.clear(); print('Conversation cleared')
            elif question == '/settings':
                settings = configure(settings)
                save(settings_path, settings)
                assistant.models = Models(settings)
                assistant.history.clear()
                print('Settings saved; conversation cleared')
            elif question == '/models':
                from support.settings import choose_models
                settings = choose_models(settings)
                save(settings_path, settings)
                assistant.models = Models(settings)
                assistant.history.clear()
                print('Models saved; conversation cleared')
            elif question == '/index':
                count, embedded = build(root, index, Models(settings), settings['embedding_model'])
                print(f'Published {count} chunks; {embedded} newly embedded')
            elif question == '/status':
                notice, rows = status(root, index, settings['embedding_model'])
                print(f'{notice}; {len(rows)} chunks; selected ticket {args.ticket}')
                print('Model connection:', settings['connection'].public())
            elif question.startswith('/'): print('Unknown command; ' + HELP)
            elif question:
                answer, hits, notice = await assistant.ask(question)
                print(notice)
                print(answer)
                for hit in hits:
                    print(f"[{hit['source']}] {hit['path']}:{hit['start']}-{hit['end']} sha256={hit['hash']}")
        except ValueError as exc:
            # All application/model ValueErrors use fixed diagnostics, never raw bodies.
            print(f'Operation failed: {exc}. No answer saved.')
        except OSError:
            print('Operation failed: check private paths and file permissions. No answer saved.')


def main():
    parser = argparse.ArgumentParser(description='Docs RAG and selected-ticket MCP support assistant')
    parser.add_argument('--knowledge', type=Path, required=True)
    parser.add_argument('--ticket', required=True)
    parser.add_argument('--mcp-url', default='http://127.0.0.1:8010/mcp')
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, OSError):
        parser.exit(1, 'Startup failed: check knowledge/state paths, private settings and selected-ticket MCP service.\n')


if __name__ == '__main__': main()
