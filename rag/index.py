"""Tracked documentation, structural chunks and atomic incremental SQLite index."""
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import fcntl
from contextlib import contextmanager

DENIED = {'.git', 'data', 'files', 'node_modules', 'vendor', 'build', 'dist', '.venv', 'venv', '__pycache__', '.cache', 'secrets', 'credentials'}
SUFFIXES = {'.md', '.markdown', '.rst', '.txt', '.json', '.yaml', '.yml', '.sql', '.graphql', '.gql', '.proto'}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def documents(project, settings):
    try:
        raw = subprocess.run(['git', 'ls-files', '-z', '--cached'], cwd=project,
            capture_output=True, check=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Cannot enumerate tracked project documents') from None
    docs = []
    for name in sorted(set(os.fsdecode(p) for p in raw.split(b'\0') if p)):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts: continue
        if any(part.lower() in DENIED or part.startswith('.') for part in relative.parts): continue
        if any(word in relative.name.lower() for word in ('secret', 'credential', 'token', 'private', 'password')): continue
        if relative.suffix.lower() not in SUFFIXES and not relative.name.upper().startswith('README'): continue
        if not any(fnmatch.fnmatchcase(name, p) for p in settings['includes']): continue
        if any(fnmatch.fnmatchcase(name, p) for p in settings['excludes']): continue
        path = project / relative
        if any(p.is_symlink() for p in [path, *path.parents] if p != project.parent): continue
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024: continue
            text = path.read_text(encoding='utf-8')
        except (FileNotFoundError, UnicodeError):
            continue
        except OSError:
            raise ValueError('Cannot read a selected project document') from None
        if '\0' in text: continue
        docs.append(dict(source=name, text=text, hash=digest(text)))
    return docs


def chunks(doc, window):
    lines = doc['text'].splitlines()
    title = doc['source']
    start = 0
    fence = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            marker = stripped[:3]
            fence = None if fence == marker else marker if fence is None else fence
        heading = fence is None and line.startswith('#') and line.lstrip('#').startswith(' ')
        if heading or i - start >= window:
            if i > start:
                text = '\n'.join(lines[start:i])
                if text.strip(): yield dict(source=doc['source'], title=title, start=start+1, end=i, text=text, hash=digest(text))
            start = i
            if heading: title = line.lstrip('#').strip()
    if start < len(lines):
        text = '\n'.join(lines[start:])
        if text.strip(): yield dict(source=doc['source'], title=title, start=start+1, end=len(lines), text=text, hash=digest(text))


def identity(settings):
    return dict(version=1, model=settings['embedding_model'], chunk_lines=settings['chunk_lines'])


def manifest(docs, settings):
    return digest(json.dumps(dict(files=[(d['source'], d['hash']) for d in docs],
        includes=settings['includes'], excludes=settings['excludes'], identity=identity(settings)), sort_keys=True))


@contextmanager
def reading(state):
    path = state / 'index.sqlite'
    if not path.exists(): raise ValueError('No index. Choose models in /settings, then run /index')
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        yield db, json.loads(db.execute('select value from metadata').fetchone()[0])
    finally:
        db.close()


def status(project, state, settings):
    docs = documents(project, settings)
    with reading(state) as (db, meta):
        return dict(documents=len(docs), chunks=db.execute('select count(*) from chunks').fetchone()[0],
                    stale=manifest(docs, settings) != meta['manifest'], model=meta['identity']['model'])


def build(project, state, settings, models):
    lock = open(state / 'index.lock', 'a')
    os.chmod(state / 'index.lock', 0o600)
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('Another /index operation is running') from None
        docs = documents(project, settings)
        if not docs: raise ValueError('No eligible tracked documents; check /settings include/exclude patterns')
        rows = [c for d in docs for c in chunks(d, settings['chunk_lines'])]
        if not rows: raise ValueError('Selected documents are empty')
        cached = {}
        if (state / 'index.sqlite').exists():
            with reading(state) as (db, meta):
                if meta['identity'] == identity(settings):
                    cached = dict(db.execute('select hash, vector from chunks'))
        missing = list(dict.fromkeys(c['hash'] for c in rows if c['hash'] not in cached))
        by_hash = {c['hash']: c['text'] for c in rows}
        for offset in range(0, len(missing), 32):
            batch = missing[offset:offset+32]
            vectors = models.embed([by_hash[h] for h in batch], settings['embedding_model'])
            cached.update(zip(batch, map(json.dumps, vectors)))
        dims = {len(json.loads(cached[c['hash']])) for c in rows}
        if len(dims)!=1: raise ValueError('Embedding dimensions changed; select a distinct model ID and rebuild')
        meta = dict(identity=identity(settings), manifest=manifest(docs, settings), dimensions=dims.pop())
        fd, temp = tempfile.mkstemp(prefix='.index-', dir=state)
        os.close(fd)
        try:
            with sqlite3.connect(temp) as db:
                db.executescript('create table metadata(value text); create table chunks(source text,title text,start integer,end integer,text text,hash text,vector text);')
                db.execute('insert into metadata values (?)', (json.dumps(meta),))
                db.executemany('insert into chunks values (?,?,?,?,?,?,?)',
                    [(c['source'],c['title'],c['start'],c['end'],c['text'],c['hash'],cached[c['hash']]) for c in rows])
            with open(temp, 'rb') as file: os.fsync(file.fileno())
            os.replace(temp, state / 'index.sqlite')
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return dict(documents=len(docs), chunks=len(rows), embedded=len(missing), reused=len(rows)-sum(c['hash'] in missing for c in rows))
    finally:
        lock.close()


def retrieve(project, state, settings, models, question):
    docs = documents(project, settings)
    with reading(state) as (db, meta):
        if meta['identity'] != identity(settings):
            raise ValueError('Index model/chunk settings changed. Run /index')
        stale = meta['manifest'] != manifest(docs, settings)
        query = models.embed([question], meta['identity']['model'])[0]
        if len(query)!=meta['dimensions']: raise ValueError('Query embedding dimensions differ; rebuild index')
        norm = math.sqrt(sum(x*x for x in query))
        hits = []
        for source,title,start,end,text,vector in db.execute('select source,title,start,end,text,vector from chunks'):
            vector = json.loads(vector)
            score = sum(a*b for a,b in zip(query,vector)) / (norm * math.sqrt(sum(x*x for x in vector)))
            if score >= settings['min_score']:
                hits.append(dict(source=source,title=title,start=start,end=end,text=text,score=score))
        hits.sort(key=lambda h:(-h['score'], h['source'], h['start']))
        return hits[:settings['top_k']], stale
