"""Compact offline checks: temporary project, stub HTTP, real read-only MCP."""
import asyncio
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from assistant.settings import defaults, project_root, state_root, save, load
from assistant.models import Models, ModelError
from assistant.mcp import context
from assistant.cli import main, ask
from rag.index import build, status, retrieve
from services.git.__main__ import project_context


class Stub(BaseHTTPRequestHandler):
    calls=[]
    fail=False
    reasoning=False
    def log_message(self,*args): pass
    def do_GET(self): self.respond({'data':[{'id':'generate'},{'id':'embed'}]})
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.calls.append((self.path,body))
        if self.fail:
            self.send_response(401); self.end_headers(); self.wfile.write(b'never-print-this-secret'); return
        if self.path.endswith('/embeddings'):
            self.respond({'data':[{'index':i,'embedding':[1.0,0.1]} for i,_ in enumerate(body['input'])]})
        else:
            self.respond({'choices':[{'message':{'content':'Documentation says: entry point [1]'}}],
                'usage':{'prompt_tokens':8,'completion_tokens':4,'total_tokens':12,
                'completion_tokens_details':{'reasoning_tokens':int(self.reasoning)}}})
    def respond(self,body):
        raw=json.dumps(body).encode(); self.send_response(200); self.end_headers(); self.wfile.write(raw)


def git(repo,*args):
    return subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True,text=True).stdout.strip()


@contextmanager
def mcp_service(repo):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    proc=subprocess.Popen([os.sys.executable,'-m','services.git','--repo',str(repo),'--port',str(port)],
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                with socket.create_connection(('127.0.0.1',port),timeout=.1): break
            except OSError: time.sleep(.05)
        else: raise AssertionError('MCP service did not start')
        yield f'http://127.0.0.1:{port}/mcp'
    finally:
        proc.terminate(); proc.wait(timeout=10)


def check():
    with tempfile.TemporaryDirectory() as temp:
        parent=Path(temp).resolve(); repo=parent/'project'; repo.mkdir()
        git(repo,'init','-q'); git(repo,'config','user.email','offline@example.invalid'); git(repo,'config','user.name','Offline')
        (repo/'docs').mkdir(); (repo/'README.md').write_text('# Project\nEntry point lives in src.\n')
        (repo/'docs/api.md').write_text('# API\nA documented endpoint.\n')
        (repo/'docs/secret.md').write_text('do not read')
        (repo/'docs/link.md').symlink_to(parent/'outside.md'); (parent/'outside.md').write_text('not documentation')
        (repo/'docs/binary.md').write_bytes(b'\x00binary')
        (repo/'untracked.md').write_text('not tracked')
        git(repo,'add','README.md','docs'); git(repo,'commit','-qm','fixture')
        assert project_root(repo/'docs')==repo.resolve()
        try: state_root(repo,repo/'cache')
        except ValueError: pass
        else: raise AssertionError('state inside project accepted')
        state=state_root(repo,parent/'state?copy#fixture'); config=defaults()
        server=ThreadingHTTPServer(('127.0.0.1',0),Stub)
        worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        try:
            config['connection'].update(base_url=f'http://127.0.0.1:{server.server_port}/v1',api_key='never-print-this-secret')
            config.update(chat_model='generate',embedding_model='embed')
            save(state,config); assert load(state)==config
            assert (state/'settings.json').stat().st_mode & 0o777 == 0o600
            models=Models(config); assert models.catalog()==['generate','embed']; assert not Stub.calls
            first=build(repo,state,config,models); assert first==dict(documents=2,chunks=2,embedded=2,reused=0),first
            count=len(Stub.calls); assert build(repo,state,config,models)['embedded']==0; assert len(Stub.calls)==count
            (repo/'docs/api.md').write_text('# API\nUpdated endpoint.\n')
            assert status(repo,state,config)['stale']
            assert build(repo,state,config,models)['embedded']==1
            (repo/'docs/api.md').unlink(); assert build(repo,state,config,models)['chunks']==1
            hits,stale=retrieve(repo,state,config,models,'entry point'); assert len(hits)==1 and not stale
            snapshot=(state/'index.sqlite').read_bytes()
            (repo/'README.md').write_text('# Project\nChanged documentation.\n')
            Stub.fail=True
            try: build(repo,state,config,models)
            except ModelError as error: assert 'never-print' not in str(error)
            else: raise AssertionError('failed embedding accepted')
            assert snapshot==(state/'index.sqlite').read_bytes()
            Stub.fail=False
            with mcp_service(repo) as url:
                config['mcp_url']=url
                facts=asyncio.run(context(url,repo)); assert facts['head']==git(repo,'rev-parse','HEAD')
                assert facts['branch']==git(repo,'branch','--show-current'); assert ' M README.md' in facts['status'].splitlines()
                try: asyncio.run(context(url,parent))
                except ValueError: pass
                else: raise AssertionError('wrong repository accepted')
                history=[]
                with patch('sys.stdout',new_callable=io.StringIO) as out:
                    ask(repo,state,config,history,'Where is the entry point?')
                assert len(history)==2 and 'README.md:1-2' in out.getvalue() and 'stale' in out.getvalue()
                assert Stub.calls[-1][1]['reasoning_effort']=='none'
                Stub.reasoning=True
                try: models.answer([{'role':'user','content':'question'}])
                except ModelError: pass
                else: raise AssertionError('reasoning OFF violation accepted')
                Stub.reasoning=False
                git(repo,'rm','-f','README.md')
                before_empty=len(Stub.calls)
                assert build(repo,state,config,models)==dict(documents=0,chunks=0,embedded=0,reused=0)
                assert retrieve(repo,state,config,models,'current branch')==([],False)
                assert len(Stub.calls)==before_empty
                with patch('sys.stdout',new_callable=io.StringIO):
                    ask(repo,state,config,history,'Which branch is open?')
                assert [path for path,_ in Stub.calls[before_empty:]]==['/v1/chat/completions']
                prompt=Stub.calls[-1][1]['messages'][-1]['content']
                assert '"documentation": []' in prompt and '"branch":' in prompt
            before=len(Stub.calls)
            with patch('builtins.input',side_effect=['/help','/new','/exit']), patch('sys.stdout',new_callable=io.StringIO):
                assert main(['--project',str(repo),'--state',str(state)])==0
            assert len(Stub.calls)==before
            git(repo,'checkout','--detach','-q'); assert project_context(repo)['branch'] is None
        finally:
            server.shutdown(); server.server_close(); worker.join()
    print('Offline checks passed: safe scan, incremental/atomic index, HTTP contracts, MCP identity and CLI')


if __name__=='__main__': check()
