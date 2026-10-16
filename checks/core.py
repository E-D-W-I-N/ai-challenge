"""Neutral temporary project and actual compatible HTTP mock; no live model calls."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

from file_agent.agent import Agent
from file_agent.settings import load, save, state_directory
from file_agent.workspace import Workspace
from shared_models import Connection
from shared_models.client import Models, ModelError
from shared_models.transport import client_options


def fails(call, error=ValueError):
    try: call()
    except error: return
    raise AssertionError('Expected explicit failure')


def tool(name, **args):
    return {'id':'call-'+name, 'type':'function', 'function':{'name':name, 'arguments':json.dumps(args)}}


@contextmanager
def model_server():
    calls, replies = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"data":[{"id":"chat"}]}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((body, self.headers.get('Authorization')))
            reply = replies.pop(0)
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(reply).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1', calls, replies
    finally: server.shutdown(); server.server_close(); worker.join()


def frame(content=None, calls=None, reasoning=False, finish=None):
    message = {'role':'assistant', 'content':content}
    if calls: message['tool_calls'] = calls
    if reasoning: message['reasoning_content'] = 'reported'
    return {'choices':[{'message':message, 'finish_reason':finish or ('tool_calls' if calls else 'stop')}], 'usage':{'total_tokens':12}}


def scenario(base, url, calls, replies):
    root = base/'project'; root.mkdir()
    (root/'api.py').write_text('def render(): return "ok"\n')
    (root/'main.py').write_text('from api import render\nrender()\n')
    (root/'README.md').write_text('# Neutral project\n')
    (root/'api.py').chmod(0o640)
    (root/'.env').write_text('SECRET-FIXTURE')
    outside = base/'outside'; outside.mkdir(); (outside/'private.txt').write_text('OUTSIDE')
    (root/'linked').symlink_to(outside, target_is_directory=True)
    (root/'binary.bin').write_bytes(b'\0binary')
    state = state_directory(root, base/'state')
    settings = dict(connection=Connection(url, 'PRIVATE-KEY'), chat_model='chat', temperature=.2, max_tokens=1024, reasoning_enabled=False)
    save(state/'settings.json', settings)
    assert load(state/'settings.json') == settings and (state/'settings.json').stat().st_mode & 0o777 == 0o600
    assert 'PRIVATE-KEY' not in repr(settings['connection'])
    uncapped = dict(settings, temperature=3.5, max_tokens=None)
    save(state/'settings.json', uncapped); assert load(state/'settings.json') == uncapped
    models = Models(uncapped)
    assert models.request('GET', '/models')['data'][0]['id'] == 'chat'
    ws = Workspace(root, state); trace = []
    agent = Agent(ws, models, trace.append)
    replies.extend([frame(calls=[tool('list'), tool('search',query='render')]),
                    frame(calls=[tool('read',path='api.py')]), frame('API used in api.py:1 and main.py:1-2.')])
    assert 'main.py' in agent.ask('Find uses of the public API and report them.')
    search_results = calls[-1][0]['messages']
    assert any(m['role']=='tool' and 'main.py' in m['content'] for m in search_results)
    assert not ws.edits
    replies.extend([frame(calls=[tool('read',path='main.py')]),
                    frame(calls=[tool('edit',path='README.md',text='# Neutral project\nCall render() from api.\n'), tool('create',path='docs/API.md',text='# API\nrender() returns ok.\n')]),
                    frame(calls=[tool('diff')]), frame('Documentation staged; inspect /diff and use /apply.')])
    before = (root/'README.md').read_text()
    agent.ask('Update the documentation using the implementation and caller.')
    assert (root/'README.md').read_text() == before and not (root/'docs').exists()
    assert 'Call render' in ws.read('README.md') and 'docs/API.md' in ws.files()
    assert 'render' in ws.diff() and ws.draft_path.stat().st_mode & 0o777 == 0o600
    restored = Workspace(root, state); assert restored.edits == ws.edits
    # All-file conflict preflight: neither first edit nor colliding new file overwritten.
    (root/'docs').mkdir(); (root/'docs/API.md').write_text('external')
    fails(restored.apply)
    assert (root/'README.md').read_text() == before and (root/'docs/API.md').read_text() == 'external'
    (root/'docs/API.md').unlink()
    assert len(restored.apply()) == 2
    assert 'Call render' in (root/'README.md').read_text()
    for path in ('../outside/private.txt', '/etc/passwd', 'linked/private.txt', '.env', '.git/config', 'build/a', 'x/../../a'):
        fails(lambda: ws.read(path), (ValueError, OSError))
        fails(lambda: ws.stage(path, 'new', True), (ValueError, OSError))
    fails(lambda: Workspace(root/'linked', state))
    fails(lambda: ws.read('binary.bin'))
    assert all('linked/' not in x and '.env' not in x for x in ws.files())
    # Staging sees staged text and preserves permissions; conflict on changed originals.
    ws = Workspace(root,state); ws.stage('api.py','def render(): return "new"\n',False)
    assert ws.search('"new"')[0]['path'] == 'api.py'
    (root/'api.py').write_text('external change\n'); fails(ws.apply)
    (root/'api.py').write_text('def render(): return "ok"\n'); ws.apply()
    assert (root/'api.py').stat().st_mode & 0o777 == 0o640
    # OFF response rejected before ANY advertised mutation tool executes.
    agent = Agent(ws,models,trace.append)
    replies.append(frame(calls=[tool('create',path='bad.md',text='bad')],reasoning=True))
    before_calls = len(calls); fails(lambda: agent.ask('Inspect the project'),ModelError)
    assert len(calls) == before_calls+1 and not ws.edits and not (root/'bad.md').exists()
    assert ws.incomplete
    # Truncated/filtered/unknown or inconsistent frames cannot complete a pending draft
    # or execute even syntactically valid mutation tools. Only one HTTP call per refusal.
    ws.stage('README.md', '# Pending documentation\n', False)
    pending = dict(ws.edits)
    for finish, tool_calls in [('length', None), ('length', [tool('create',path='bad.md',text='bad')]),
                               ('content_filter', None), ('unknown', None),
                               ('stop', [tool('create',path='bad.md',text='bad')]), ('tool_calls', None)]:
        replies.append(frame('Incomplete but nonempty answer', calls=tool_calls, finish=finish))
        before_calls, before_trace = len(calls), len(trace)
        fails(lambda: agent.ask('Continue documentation'), ModelError)
        restored = Workspace(root, state)
        assert len(calls) == before_calls+1 and len(trace) == before_trace
        assert restored.edits == pending and restored.incomplete and not (root/'bad.md').exists()
        fails(restored.apply)
    ws.discard()
    # Ctrl-C leaves a persisted incomplete draft; no automatic apply.
    with patch.object(models,'answer', side_effect=[{'role':'assistant','content':None,'tool_calls':[tool('create',path='draft.md',text='pending')]}, KeyboardInterrupt]):
        fails(lambda: agent.ask('Prepare a note'),KeyboardInterrupt)
    assert Workspace(root,state).incomplete and not (root/'draft.md').exists()
    fails(ws.apply); ws.discard()
    assert all(body['reasoning_effort']=='none' and 'max_tokens' not in body and body['temperature']==3.5 and auth=='Bearer PRIVATE-KEY' for body,auth in calls)
    assert all('SECRET-FIXTURE' not in json.dumps(body) and 'OUTSIDE' not in json.dumps(body) and 'PRIVATE-KEY' not in json.dumps(body) for body,_ in calls)
    cli = subprocess.run([sys.executable,'-m','file_agent','--project',str(root),'--state',str(state)],input='/status\n/new\n/diff\n/exit\n',text=True,capture_output=True,timeout=10)
    assert cli.returncode == 0 and 'Conversation cleared' in cli.stdout and 'PRIVATE-KEY' not in cli.stdout+cli.stderr
    fails(lambda: state_directory(root, root/'state'))
    assert client_options('http://localhost:8005') == {'trust_env':False}
    with patch('shared_models.transport.proxy_bypass',return_value=False), patch('shared_models.transport.getproxies',return_value={'https':'http://proxy.invalid:8080'}):
        assert 'proxy' in client_options('https://model.invalid/v1')
    assert trace and any('tool → search' in item for item in trace)


def main():
    with tempfile.TemporaryDirectory() as directory, model_server() as (url,calls,replies):
        scenario(Path(directory),url,calls,replies)
    print('OK: offline HTTP tool rounds, multi-file analysis/docs, overlay, explicit apply, conflicts, paths, interruption, reasoning and CLI')


if __name__ == '__main__': main()
