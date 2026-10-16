"""Small behavioral checks: temporary Git revisions + mocked HTTP, no live inference."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

import httpx

from review.__main__ import main, settings
from review.ci import event_identity, fetch
from review.git import revision, snapshot
from review.runner import publish, run, validate
from shared_models import Connection
from shared_models.client import Models, ModelError


def command(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


def fails(call, message):
    try:
        call()
    except (ValueError, ModelError):
        return
    raise AssertionError(message)


class Stub:
    def __init__(self):
        self.calls = []
        self.reasoning = False
        self.bad_source = False
        self.http_failure = False

    def handle(self, request):
        value = json.loads(request.content)
        self.calls.append((request.url.path, value))
        assert request.headers['Authorization'] == 'Bearer fixture-secret'
        if self.http_failure:
            return httpx.Response(401, text='fixture-secret')
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data': [{'index': i, 'embedding': [1, 1 if 'docs' in text else 2]}
                                                      for i, text in enumerate(value['input'])], 'usage': {'total_tokens': len(value['input'])}})
        assert value['reasoning_effort'] == 'none'
        prompt = json.loads(value['messages'][1]['content'])
        source = next(s for s in prompt['changed_head'] if s['path'] == 'app.py')
        findings = {'bugs': [{'path': 'app.py', 'line': 1, 'source_ids': ['invented' if self.bad_source else source['id']],
                             'text': 'Potential behavior change; inspect the return value.'}],
                    'architecture': [], 'recommendations': []}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(findings)}}],
                                        'usage': {'total_tokens': 12, 'completion_tokens_details': {'reasoning_tokens': int(self.reasoning)}}})

    def client(self, client_type, **options):
        options.pop('proxy', None)
        return client_type(**options, transport=httpx.MockTransport(self.handle))


def main_checks():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        repo = root / 'target'
        repo.mkdir()
        command(repo, 'init', '-q')
        command(repo, 'config', 'user.name', 'Offline Fixture')
        command(repo, 'config', 'user.email', 'fixture@example.invalid')
        (repo / 'docs').mkdir()
        (repo / 'docs' / 'architecture.md').write_text('# Base docs\nArchitecture docs contract.\n')
        (repo / 'app.py').write_text('def value():\n    return 1\n')
        (repo / 'secret.json').write_text('SECRET-CONTENT-NEVER-SEND')
        (repo / 'binary.py').write_bytes(b'\0binary')
        (repo / 'node_modules').mkdir()
        (repo / 'node_modules' / 'vendor.js').write_text('VENDOR-NEVER-SEND')
        (repo / 'alias.py').symlink_to('app.py')
        command(repo, 'add', 'docs/architecture.md', 'app.py', 'secret.json', 'binary.py', 'node_modules/vendor.js', 'alias.py')
        command(repo, 'commit', '-qm', 'base')
        base = command(repo, 'rev-parse', 'HEAD')
        (repo / 'app.py').write_text('def value():\n    return 2\n')
        (repo / 'docs' / 'architecture.md').write_text('# Head docs\nHEAD-DOCS-NOT-BASE-RAG\n')
        command(repo, 'add', 'app.py', 'docs/architecture.md')
        command(repo, 'commit', '-qm', 'head')
        head = command(repo, 'rev-parse', 'HEAD')
        # Neither worktree content, target config, hooks nor external diff may be used.
        (repo / 'app.py').write_text('WORKTREE-CONTENT-NEVER-SEND')
        (repo / 'docs' / 'architecture.md').write_text('WORKTREE-DOCS-NEVER-SEND')
        marker = root / 'executed'
        hook = root / 'hook'
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
        hook.chmod(0o700)
        command(repo, 'config', 'diff.external', str(hook))
        command(repo, 'config', 'core.hooksPath', str(root))
        snap = snapshot(repo, base, head)
        assert snap['merge_base'] == base
        assert 'return 2' in snap['texts'][(head, 'app.py')]
        assert 'Base docs' in snap['texts'][(base, 'docs/architecture.md')]
        assert all(p not in {'alias.py', 'secret.json', 'binary.py', 'node_modules/vendor.js'} for _, p in snap['texts'])
        fails(lambda: revision('HEAD'), 'symbolic ref accepted')
        fails(lambda: revision('--upload-pack=bad'), 'option accepted as revision')
        stub = Stub()
        config = settings(Connection('https://models.example.invalid/v1', 'fixture-secret'), 'chat', 'embed')
        with patch('shared_models.client.create_client', stub.client):
            report = run(repo, base, head, Models(config))
            assert report['base_corpus']['docs'] and report['base_corpus']['code']
            assert report['usage'] and all(row['tokens'] for row in report['usage'])
            assert all(s['revision'] == base for s in report['sources'] if s['kind'] != 'changed_head')
            assert all(s['revision'] == head for s in report['sources'] if s['kind'] == 'changed_head')
            assert all({s['id']: s for s in report['sources']}[r['id']]['kind'] in {'docs', 'code'}
                       for selection in report['retrieval'] for r in selection['sources'])
            serialized = json.dumps(stub.calls)
            for forbidden in ('WORKTREE', 'SECRET-CONTENT', 'VENDOR-NEVER', 'fixture-secret'):
                assert forbidden not in serialized
            publish(report, root / 'output')
            assert 'fixture-secret' not in (root / 'output' / 'review.json').read_text()
            assert 'Potential bugs' in (root / 'output' / 'review.md').read_text()
            fails(lambda: publish(report, root / 'output'), 'existing output overwritten')
            validate({'bugs': [], 'architecture': [], 'recommendations': []}, report['sources'])
            bad = json.loads(json.dumps(report['findings']))
            bad['bugs'][0]['line'] = 999999
            fails(lambda: validate(bad, report['sources']), 'unchecked line accepted')
            stub.bad_source = True
            fails(lambda: run(repo, base, head, Models(config)), 'invented source accepted')
            stub.bad_source = False
            stub.reasoning = True
            fails(lambda: run(repo, base, head, Models(config)), 'reasoning OFF violation accepted')
            stub.reasoning = False
            stub.http_failure = True
            before = len(stub.calls)
            fails(lambda: run(repo, base, head, Models(config)), 'HTTP failure accepted')
            assert len(stub.calls) == before + 1, 'paid retry happened'
        assert not marker.exists(), 'target executable ran'
        event = {'number': 3, 'action': 'synchronize', 'repository': {'full_name': 'owner/project'},
                 'pull_request': {'number': 3, 'base': {'repo': {'full_name': 'owner/project'}, 'sha': base}, 'head': {'sha': head}}}
        assert event_identity(event, 'owner/project') == ('owner/project', 3, base, head)
        event['number'] = '3; bad'
        fails(lambda: event_identity(event, 'owner/project'), 'untrusted PR number accepted')
        # Fetch credentials never appear in argv; mismatch rejects a moved PR head.
        calls = []
        def fake_git(directory, *args):
            calls.append(args)
            if args[:1] == ('rev-parse',):
                return ((base if args[1].endswith('base') else 'f'*40) + '\n').encode()
            return b''
        with patch('review.ci.git', fake_git):
            fails(lambda: fetch('owner/project', 3, base, head, 'fixture-secret', root), 'moved PR head accepted')
        assert 'fixture-secret' not in repr(calls)
        assert 'GIT_CONFIG_VALUE_0' not in os.environ
        connection = root / 'connection.json'
        connection.write_text(json.dumps({'base_url': 'https://models.example.invalid/v1', 'api_key': 'fixture-secret'}))
        argv = ['review', '--repo', str(repo), '--base', base, '--head', head,
                '--connection-file', str(connection), '--chat-model', 'chat', '--embedding-model', 'embed', '--output', str(root/'cli-output')]
        stub.http_failure = False
        with patch('sys.argv', argv), patch('shared_models.client.create_client', stub.client), contextlib.redirect_stdout(io.StringIO()):
            assert main() == 0
        # Bounds reject a snapshot instead of silently truncating eligible source code.
        (repo / 'large.py').write_text('x' * (128 * 1024 + 1))
        command(repo, 'add', 'large.py')
        command(repo, 'commit', '-qm', 'oversized')
        large_head = command(repo, 'rev-parse', 'HEAD')
        fails(lambda: snapshot(repo, base, large_head), 'oversized eligible source silently omitted')
    print('Offline Git, RAG, HTTP, validation, CLI and CI boundary checks passed.')


if __name__ == '__main__':
    main_checks()
