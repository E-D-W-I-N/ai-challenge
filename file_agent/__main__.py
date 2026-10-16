"""One project, one human console, explicitly applied persistent drafts."""
import argparse
from pathlib import Path

from file_agent.agent import Agent
from file_agent.settings import choose_models, configure, load, save, state_directory
from file_agent.workspace import Workspace
from shared_models.client import Models

HELP = '/settings /models /status /diff /apply /discard /new /help /exit; otherwise enter a goal'


def run(args):
    # Validate the raw project path before resolving so symlink root is rejected.
    if args.project.is_symlink(): raise ValueError('Symlink root is forbidden')
    root = args.project.resolve(strict=True)
    state = state_directory(root, args.state)
    settings_path = state/'settings.json'
    settings = load(settings_path)
    workspace = Workspace(root, state)
    agent = Agent(workspace, Models(settings))
    print(f'Project: {root}; private state: {state}')
    print(HELP)
    if workspace.edits: print('Restored pending draft; inspect /diff. Incomplete:', workspace.incomplete)
    while True:
        try: command = input('files> ').strip()
        except (EOFError, KeyboardInterrupt):
            print('Exit; pending draft retained outside project.'); return
        try:
            if command == '/exit':
                print('Exit; pending draft retained outside project.'); return
            if command == '/help': print(HELP)
            elif command == '/diff': print(workspace.diff() or 'No pending changes')
            elif command == '/apply':
                print(workspace.diff() or 'No pending changes')
                print('Applied:', workspace.apply())
            elif command == '/discard': workspace.discard(); agent.history.clear(); print('Draft discarded')
            elif command == '/new':
                if workspace.edits: raise ValueError('Pending draft: /apply or /discard before /new')
                agent.history.clear(); print('Conversation cleared')
            elif command in ('/settings', '/models'):
                settings = configure(settings) if command == '/settings' else choose_models(settings)
                save(settings_path, settings); agent.models = Models(settings); agent.history.clear()
                print('Settings saved; conversation cleared; draft retained')
            elif command == '/status':
                print('Connection:', settings['connection'].public())
                print({k:v for k,v in settings.items() if k != 'connection'})
                print(f'Pending files: {len(workspace.edits)}; incomplete: {workspace.incomplete}')
            elif command.startswith('/'): print('Unknown command; '+HELP)
            elif command: print(agent.ask(command))
        except KeyboardInterrupt: print('Interrupted; draft incomplete and retained. Nothing auto-applied.')
        except ValueError as exc: print(f'Operation failed: {exc}')
        except OSError: print('Operation failed: check paths and permissions; inspect retained draft.')


def main():
    parser = argparse.ArgumentParser(description='Project file assistant with staged changes and human apply')
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--state', type=Path)
    args = parser.parse_args()
    try: run(args)
    except (ValueError, OSError): parser.exit(1, 'Startup failed: check project/state paths and private settings.\n')


if __name__ == '__main__': main()
