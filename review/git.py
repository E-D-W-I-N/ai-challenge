"""Read immutable Git objects. Never checkout or execute target project content."""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
import selectors
import subprocess
import time

SHA = re.compile(r'[0-9a-f]{40}\Z')
DENIED = {'data', 'files', 'node_modules', 'vendor', 'build', 'dist', 'venv', '__pycache__', 'target', 'coverage', 'secrets', 'credentials'}
DOC = {'.md', '.markdown', '.rst', '.txt', '.adoc'}
CODE = {'.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.kt', '.go', '.rs', '.c', '.h', '.cpp', '.hpp', '.cs', '.rb', '.php', '.sh', '.bash', '.sql', '.graphql', '.gql', '.proto', '.json', '.yaml', '.yml', '.toml', '.html', '.css', '.vue', '.svelte', '.swift', '.scala', '.ex', '.exs', '.clj', '.xml'}
MAX_FILE = 128 * 1024
MAX_CORPUS = 4 * 1024 * 1024
MAX_DIFF = 256 * 1024


def revision(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Revision must be an exact lowercase 40-character commit SHA')
    return value


def git(repo, *args, limit=2 * 1024 * 1024):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0', 'GIT_PAGER': 'cat'}
    command = ['git', '--no-pager', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-c', 'protocol.ext.allow=never', '-C', str(repo), *args]
    try:
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            output = bytearray()
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() >= deadline:
                        process.kill()
                        raise ValueError('Git read timed out')
                    if not selector.select(timeout=min(1, max(0, deadline-time.monotonic()))):
                        continue
                    part = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    if not part:
                        break
                    output.extend(part)
                    if len(output) > limit:
                        process.kill()
                        raise ValueError('Git output exceeds review bound')
            if process.wait(timeout=max(0.1, deadline-time.monotonic())):
                raise ValueError('Cannot read requested Git objects')
            return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Cannot read requested Git objects') from None


def kind(path):
    p = PurePosixPath(path)
    if (p.is_absolute() or '..' in p.parts or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(x.startswith('.') or x.lower() in DENIED for x in p.parts)):
        return None
    name = p.name.lower()
    if (any(x in name for x in ('secret', 'credential', 'token', 'private', 'password', 'lock'))
            or p.suffix.lower() in {'.pem', '.key', '.crt', '.p12', '.pfx', '.env'}):
        return None
    if p.suffix.lower() in DOC or name.startswith('readme') or ('docs' in p.parts and p.suffix.lower() in CODE):
        return 'docs'
    if p.suffix.lower() in CODE or name in {'dockerfile', 'makefile'}:
        return 'code'
    return None


def tree(repo, commit):
    result = {}
    for entry in git(repo, 'ls-tree', '-r', '-z', '-l', commit).split(b'\0'):
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1)
        mode, typ, oid, size = meta.split()
        try:
            path = name.decode('utf-8')
        except UnicodeError:
            raise ValueError('Non-UTF8 Git paths cannot be reviewed') from None
        result[path] = (mode.decode(), typ.decode(), oid.decode(), int(size) if size != b'-' else 0)
        if len(result) > 10000:
            raise ValueError('Repository exceeds 10000 tree entries')
    return result


def snapshot(repo, base, head):
    base, head = revision(base), revision(head)
    for commit in (base, head):
        if git(repo, 'rev-parse', '--verify', commit + '^{commit}').decode().strip() != commit:
            raise ValueError('Requested revision is not an exact commit')
    merge_bases = git(repo, 'merge-base', '--all', base, head).decode().splitlines()
    if len(merge_bases) != 1:
        raise ValueError('Review requires one merge base')
    before, after = tree(repo, base), tree(repo, head)
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    if not changed:
        raise ValueError('No changed files between exact base and head')
    if len(changed) > 100:
        raise ValueError('Review exceeds 100 changed paths')
    excluded = []
    texts = {}
    total = 0
    for commit, entries in ((base, before), (head, after)):
        for path, (mode, typ, oid, size) in sorted(entries.items()):
            if commit == head and path not in changed:
                continue
            if mode not in {'100644', '100755'} or typ != 'blob' or not kind(path):
                if path in changed and path not in excluded:
                    excluded.append(path)
                continue
            if size > MAX_FILE:
                raise ValueError('Eligible file exceeds 128 KiB review bound')
            raw = git(repo, 'cat-file', 'blob', oid, limit=MAX_FILE)
            try:
                text = raw.decode('utf-8')
            except UnicodeError:
                if path in changed and path not in excluded:
                    excluded.append(path)
                continue
            if '\0' in text or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                if path in changed and path not in excluded:
                    excluded.append(path)
                continue
            total += len(raw)
            if total > MAX_CORPUS:
                raise ValueError('Eligible snapshot exceeds 4 MiB review bound')
            texts[(commit, path)] = text
    diffs = []
    size = 0
    eligible = []
    for path in changed:
        if path in excluded:
            continue
        if not any((c, path) in texts for c in (base, head)):
            continue
        patch = git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames', '--unified=3',
                    base, head, '--', ':(literal)' + path, limit=MAX_DIFF).decode('utf-8')
        size += len(patch.encode())
        if size > MAX_DIFF:
            raise ValueError('Combined diff exceeds 256 KiB review bound')
        diffs.append(dict(path=path, patch=patch))
        eligible.append(path)
    if not eligible:
        raise ValueError('No eligible text changes; review cannot analyze excluded content')
    return dict(base=base, head=head, merge_base=merge_bases[0], changed_files=changed,
                reviewed_files=eligible, excluded_files=excluded, diffs=diffs, texts=texts)
