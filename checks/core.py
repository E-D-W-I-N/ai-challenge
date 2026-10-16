"""One temporary support fixture, HTTP model mock and real HTTP MCP services."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from shared_models import Connection, generation_payload
from shared_models.client import Models, ModelError
from shared_models.transport import client_options
from support.assistant import Assistant
from support.context import fetch_context, selected_context
from support.settings import load, save, state_directory
from rag.index import build, read_index, scan, status


def fails(call, error=ValueError):
    try: call()
    except error: return
    raise AssertionError('Expected explicit failure')


@contextmanager
def model_server():
    calls = []
    mode = {'error': False, 'reasoning': False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({'data': [{'id':'generation'}, {'id':'embedding'}]}).encode())
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body, self.headers.get('Authorization')))
            if mode['error']:
                self.send_response(503); self.end_headers(); self.wfile.write(b'SECRET-UPSTREAM'); return
            if self.path == '/v1/embeddings':
                # Query uses selected ticket context; neutral question alone has no category.
                def vector(text): return [1, 0] if 'alpha' in text.lower() else [0, 1]
                value = {'data': [{'index':i, 'embedding':vector(t)} for i,t in enumerate(body['input'])]}
            else:
                value = {'choices':[{'message':{'content':'Use the documented steps [1]. Ticket context is reported separately.'}}], 'usage':{'total_tokens':12}}
                if mode['reasoning']: value['choices'][0]['message']['reasoning_content'] = 'reported'
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(value).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1', calls, mode
    finally: server.shutdown(); server.server_close(); worker.join()


@contextmanager
def mcp_server(data, ticket):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    process = subprocess.Popen([sys.executable, '-m', 'support.context', '--data', str(data), '--ticket', ticket, '--port', str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if process.poll() is not None: raise AssertionError('MCP process exited')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1): break
            except OSError: time.sleep(.05)
        else: raise AssertionError('MCP process did not start')
        yield f'http://127.0.0.1:{port}/mcp'
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()


async def scenario(directory, base_url, calls, mode):
    root = directory / 'knowledge'; root.mkdir()
    (root/'alpha.md').write_text('# Alpha\nAlpha documentation.\u2028Still first source line.\n')
    (root/'beta.md').write_text('# Beta\nBeta documentation.\n')
    (root/'secret.md').write_text('Never embed')
    (root/'binary.md').write_bytes(b'\x00binary')
    (root/'not-doc.py').write_text('Never embed code')
    (root/'build').mkdir(); (root/'build'/'generated.md').write_text('Never embed')
    (root/'link.md').symlink_to(root/'alpha.md')
    corpus = scan(root)
    assert {c['path'] for c in corpus} == {'alpha.md', 'beta.md'}
    assert corpus[0]['end'] == 2 and '\u2028' in corpus[0]['text']
    state = state_directory(root, directory / 'state?#')
    settings_path = state/'settings.json'; index = state/'index.sqlite'
    settings = dict(connection=Connection(base_url, 'PRIVATE-KEY'), chat_model='generation', embedding_model='embedding', temperature=.2, max_tokens=1024, reasoning_enabled=False)
    assert 'PRIVATE-KEY' not in repr(settings['connection'])
    save(settings_path, settings)
    assert settings_path.stat().st_mode & 0o777 == 0o600
    assert load(settings_path) == settings
    models = Models(settings)
    assert models.request('GET', '/models')['data'][0]['id'] == 'generation'
    assert build(root, index, models, 'embedding') == (2, 2)
    assert index.stat().st_mode & 0o777 == 0o600
    before = len(calls)
    assert build(root, index, models, 'embedding') == (2, 0) and len(calls) == before
    (root/'beta.md').write_text('# Beta\nBeta updated documentation.\n')
    assert status(root,index,'embedding')[0].startswith('STALE')
    assert build(root,index,models,'embedding') == (2,1)
    fails(lambda: status(root,index,'wrong'))
    fails(lambda: read_index(state/'missing'))
    fails(lambda: state_directory(root, root/'state'))
    data = directory/'support.json'
    def ticket(name): return dict(id=name, user_id=name+'-owner', title=name, description=name+' request', status='open', messages=[dict(author='user',text=name+' details')])
    document = dict(users=[dict(id=x+'-owner',context=x+' user context') for x in ['alpha','beta']], tickets=[ticket(x) for x in ['alpha','beta']])
    data.write_text(json.dumps(document))
    fails(lambda: selected_context(data,'unknown'))
    with mcp_server(data,'alpha') as alpha_url, mcp_server(data,'beta') as beta_url:
        alpha = Assistant(root,index,'alpha',alpha_url,models)
        beta = Assistant(root,index,'beta',beta_url,models)
        _, ahits, _ = await alpha.ask('What are the next steps?')
        _, bhits, _ = await beta.ask('What are the next steps?')
        assert ahits[0]['path'] == 'alpha.md' and bhits[0]['path'] == 'beta.md'
        prompts = [json.loads(b['messages'][1]['content']) for p,b,_ in calls if p.endswith('/chat/completions')]
        assert prompts[0]['context']['ticket']['id'] == 'alpha'
        assert 'beta-owner' not in json.dumps(prompts[0]) and 'alpha-owner' not in json.dumps(prompts[1])
        assert all(b['reasoning_effort'] == 'none' for p,b,_ in calls if p.endswith('/chat/completions'))
        assert all(auth == 'Bearer PRIVATE-KEY' and 'PRIVATE-KEY' not in json.dumps(b) for _,b,auth in calls)
        try: await fetch_context(alpha_url,'beta')
        except ValueError: pass
        else: raise AssertionError('Mismatched ticket accepted')
        document['tickets'][0]['status'] = 'pending'; data.write_text(json.dumps(document))
        assert (await fetch_context(alpha_url,'alpha'))['ticket']['status'] == 'pending'
        history = list(alpha.history)
        data.write_text('{malformed')
        before = len(calls)
        try: await alpha.ask('Continue')
        except ValueError: pass
        else: raise AssertionError('Malformed context accepted')
        assert len(calls) == before and alpha.history == history
        document['tickets'][0]['user_id']='unknown'; data.write_text(json.dumps(document))
        fails(lambda: selected_context(data,'alpha'))
        document['tickets'][0]['user_id']='alpha-owner'; data.write_text(json.dumps(document))
        mode['reasoning'] = True
        try: await alpha.ask('Continue')
        except ModelError: pass
        else: raise AssertionError('Reasoning accepted')
        assert alpha.history == history
        mode['reasoning'] = False
        mode['error'] = True; before = len(calls)
        try: models.embed(['test'], 'embedding')
        except ModelError as exc: assert 'SECRET-UPSTREAM' not in str(exc) and 'PRIVATE-KEY' not in str(exc)
        else: raise AssertionError('HTTP error accepted')
        assert len(calls) == before+1
        mode['error'] = False
        alpha.history.clear(); assert not alpha.history
        (root/'alpha.md').unlink(); (root/'beta.md').unlink()
        assert build(root,index,models,'embedding') == (0,0)
        before = len([1 for p,_,_ in calls if p.endswith('/embeddings')])
        _, hits, _ = await alpha.ask('Any instructions?')
        assert not hits and len([1 for p,_,_ in calls if p.endswith('/embeddings')]) == before
        settings['embedding_model']=''
        save(settings_path, settings)
        # Real CLI startup and /new /status /exit; stdin contains no model request.
        cli = subprocess.run([sys.executable,'-m','support','--knowledge',str(root),'--state',str(state),'--ticket','alpha','--mcp-url',alpha_url], input='/new\n/status\n/exit\n', text=True, capture_output=True, timeout=20)
        assert cli.returncode == 0 and 'Conversation cleared' in cli.stdout
        assert 'PRIVATE-KEY' not in cli.stdout + cli.stderr
    fails(lambda: scan(directory/'missing'), OSError)
    assert client_options('http://127.0.0.1:8010') == {'trust_env':False}
    with patch('shared_models.transport.proxy_bypass', return_value=False), patch('shared_models.transport.getproxies', return_value={'https':'http://proxy.invalid:8080'}):
        assert 'proxy' in client_options('https://model.invalid/v1')
    assert generation_payload({'reasoning_effort':'high'})['reasoning_effort'] == 'none'


def main():
    with tempfile.TemporaryDirectory() as directory, model_server() as (url,calls,mode):
        asyncio.run(scenario(Path(directory), url,calls,mode))
    print('OK: offline support, RAG cache, isolation, actual HTTP MCP and model transport')


if __name__ == '__main__': main()
