"""Bounded Markdown corpus and explicit incremental private SQLite index."""
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

EXCLUDED = {'data', 'files', 'node_modules', 'vendor', 'build', 'dist', 'venv', '__pycache__', 'secrets'}


def scan(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Knowledge path must be a directory')
    corpus = []
    total = entries = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d.lower() not in EXCLUDED and not (Path(directory) / d).is_symlink())
        for name in sorted(files):
            entries += 1
            if entries > 10000:
                raise ValueError('Knowledge directory exceeds 10000 files')
            path = Path(directory) / name
            if path.is_symlink() or name.startswith('.') or path.suffix.lower() != '.md' or any(s in name.lower() for s in ('secret', 'credential', 'password', 'private-key')):
                continue
            if path.stat().st_size > 128 * 1024:
                raise ValueError('Markdown file exceeds 128 KiB')
            try:
                text = path.read_text(encoding='utf-8')
            except UnicodeError:
                continue
            if '\0' in text:
                continue
            total += len(text.encode())
            if total > 4 * 1024 * 1024:
                raise ValueError('Knowledge corpus exceeds 4 MiB')
            lines = text.split('\n')
            if lines[-1] == '': lines.pop()
            for start in range(0, len(lines), 40):
                body = '\n'.join(lines[start:start+40])
                if len(body.encode()) > 12000:
                    raise ValueError('Markdown chunk exceeds 12000 bytes')
                if body.strip():
                    relative = path.relative_to(root).as_posix()
                    digest = hashlib.sha256(body.encode()).hexdigest()
                    corpus.append(dict(path=relative, start=start+1, end=min(start+40, len(lines)), text=body, hash=digest))
                    if len(corpus) > 2000:
                        raise ValueError('Knowledge corpus exceeds 2000 chunks')
    return corpus


def fingerprint(corpus):
    return hashlib.sha256(json.dumps(corpus, sort_keys=True).encode()).hexdigest()


def read_index(path):
    if not Path(path).is_file():
        raise ValueError('Index missing; run /index explicitly')
    try:
        with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
            meta = dict(db.execute('SELECT key,value FROM metadata'))
            rows = [(json.loads(c), json.loads(v)) for c, v in db.execute('SELECT chunk,vector FROM chunks ORDER BY position')]
        return meta, rows
    except (sqlite3.Error, ValueError):
        raise ValueError('Index invalid; rebuild explicitly with /index') from None


def build(root, path, models, model):
    root, path = Path(root).resolve(), Path(path)
    if path.resolve().is_relative_to(root):
        raise ValueError('Index must be outside knowledge directory')
    if not model:
        raise ValueError('Explicit embedding model is required')
    corpus = scan(root)
    cache = {}
    if path.exists():
        meta, rows = read_index(path)
        if meta['root'] == str(root) and meta['model'] == model:
            cache = {c['hash']: v for c, v in rows}
    missing = list(dict.fromkeys(c['hash'] for c in corpus if c['hash'] not in cache))
    texts = {c['hash']: c['text'] for c in corpus}
    for offset in range(0, len(missing), 32):
        keys = missing[offset:offset+32]
        cache.update(zip(keys, models.embed([texts[k] for k in keys], model)))
    dims = {len(cache[c['hash']]) for c in corpus}
    if len(dims) > 1:
        raise ValueError('Embedding dimensions changed; use a new state directory')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    os.close(fd)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.executemany('INSERT INTO metadata VALUES(?,?)', [('root', str(root)), ('model', model), ('fingerprint', fingerprint(corpus))])
            db.execute('CREATE TABLE chunks(position INTEGER PRIMARY KEY,chunk TEXT,vector TEXT)')
            db.executemany('INSERT INTO chunks VALUES(?,?,?)', [(i, json.dumps(c), json.dumps(cache[c['hash']])) for i, c in enumerate(corpus)])
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return len(corpus), len(missing)


def status(root, path, model):
    meta, rows = read_index(path)
    if meta['root'] != str(Path(root).resolve()) or meta['model'] != model:
        raise ValueError('Index belongs to another knowledge path or embedding model; run /index')
    return ('STALE: run /index explicitly' if meta['fingerprint'] != fingerprint(scan(root)) else 'Current'), rows


def retrieve(rows, query, models, model):
    vector = models.embed([query], model)[0]
    def score(other):
        if len(vector) != len(other): raise ValueError('Embedding dimensions differ')
        return sum(a*b for a,b in zip(vector, other)) / math.sqrt(sum(a*a for a in vector)*sum(b*b for b in other))
    ranked = sorted(((score(v), c) for c,v in rows), key=lambda x: (-x[0], x[1]['path'], x[1]['start']))
    return [dict(c, source=i+1, score=s) for i, (s,c) in enumerate(ranked[:3])]
