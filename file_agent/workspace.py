"""Bounded text tools, private persistent overlay and explicit conflict-checked apply."""
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from contextlib import contextmanager

FILE_LIMIT = 128 * 1024
OUTPUT_LIMIT = 256 * 1024
BLOCKED = {'data', 'files', 'build', 'dist', 'vendor', 'node_modules', '__pycache__',
           'id_rsa', 'id_ed25519', 'api_key', 'api-key', 'tokens', 'token', 'credentials', 'secrets', 'secret', 'passwords', 'target', 'venv'}
SECRET_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3'}


def bounded(value, limit=OUTPUT_LIMIT):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text.encode('utf-8')) > limit: raise ValueError('Size limit exceeded; narrow the operation')
    return text


def identity(text):
    return None if text is None else hashlib.sha256(text.encode('utf-8')).hexdigest()


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out: json.dump(value, out, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def parts(path):
    if not isinstance(path, str) or not path or '\\' in path or any(ord(c) < 32 for c in path):
        raise ValueError('Invalid relative path')
    components = path.split('/')
    if PurePosixPath(path).is_absolute() or any(x in ('', '.', '..') for x in components):
        raise ValueError('Path must stay inside the project')
    for component in components:
        low = component.lower()
        if low.startswith('.') or low in BLOCKED or any(x in low for x in ('secret', 'credential', 'password')) or Path(low).suffix in SECRET_SUFFIXES:
            raise ValueError('Protected path')
    return components


class Workspace:
    def __init__(self, root, state):
        root = Path(root)
        if root.is_symlink(): raise ValueError('Project root cannot be a symlink')
        self.root = root.resolve(strict=True)
        if not self.root.is_dir(): raise ValueError('Project must be a directory')
        self.draft_path = state / 'draft.json'
        self.edits, self.incomplete = {}, False
        if self.draft_path.exists():
            if self.draft_path.stat().st_size > 4 * 1024 * 1024: raise ValueError('Draft exceeds limit')
            try:
                draft = json.loads(self.draft_path.read_text(encoding='utf-8'))
                if draft['root'] != str(self.root): raise ValueError()
                self.edits, self.incomplete = draft['edits'], draft['incomplete']
                if type(self.incomplete) is not bool or not isinstance(self.edits, dict): raise ValueError()
                for path, edit in self.edits.items():
                    parts(path)
                    if set(edit) != {'original', 'text', 'mode'}: raise ValueError()
                    if not isinstance(edit['text'], str) or (edit['original'] is not None and not isinstance(edit['original'], str)): raise ValueError()
                    bounded(edit['text'], FILE_LIMIT)
                    if edit['original'] is not None: bounded(edit['original'], FILE_LIMIT)
                    if type(edit['mode']) is not int or not 0 <= edit['mode'] <= 0o777: raise ValueError()
            except (KeyError, TypeError, ValueError): raise ValueError('Invalid private draft') from None

    @contextmanager
    def parent(self, path, create=False):
        components = parts(path)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in components[:-1]:
                if create:
                    try: os.mkdir(name, mode=0o755, dir_fd=fd)
                    except FileExistsError: pass
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = nxt
            yield fd, components[-1]
        finally: os.close(fd)

    def disk(self, path):
        try:
            with self.parent(path) as (directory, name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode): raise ValueError('Only regular text files are allowed')
                    raw = stream.read(FILE_LIMIT + 1)
                if len(raw) > FILE_LIMIT: raise ValueError('File exceeds 128 KiB')
                if b'\0' in raw: raise ValueError('Binary file is excluded')
                try: text = raw.decode('utf-8')
                except UnicodeError: raise ValueError('Only UTF-8 text is allowed') from None
                return text, stat.S_IMODE(info.st_mode) & 0o777
        except FileNotFoundError: return None, 0o644

    def read(self, path):
        parts(path)
        if path in self.edits: return self.edits[path]['text']
        text, _ = self.disk(path)
        if text is None: raise ValueError('File does not exist')
        return text

    def files(self):
        result = set(self.edits)
        visited = 0
        def scan(directory, prefix=''):
            nonlocal visited
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 10000: raise ValueError('Project traversal exceeds 10000 entries')
                    path = prefix + entry.name
                    try: parts(path)
                    except ValueError: continue
                    if entry.is_symlink(): continue
                    if entry.is_dir(follow_symlinks=False):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                        try: scan(child, path + '/')
                        finally: os.close(child)
                    elif entry.is_file(follow_symlinks=False): result.add(path)
                    if len(result) > 10000: raise ValueError('Project exceeds 10000 files')
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: scan(fd)
        finally: os.close(fd)
        return sorted(result)

    def search(self, query):
        if not isinstance(query, str) or not query: raise ValueError('Nonempty literal query required')
        hits, total = [], 0
        for path in self.files():
            try: text = self.read(path)
            except ValueError as exc:
                if str(exc) in ('Binary file is excluded', 'Only UTF-8 text is allowed'): continue
                raise
            total += len(text.encode())
            if total > 4 * 1024 * 1024: raise ValueError('Search exceeds 4 MiB; use read on selected paths')
            hits.extend({'path': path, 'line': number, 'text': line} for number, line in enumerate(text.split('\n'), 1) if query in line)
            bounded(hits)
        return hits

    def persist(self):
        atomic_json(self.draft_path, dict(root=str(self.root), edits=self.edits, incomplete=self.incomplete))

    def stage(self, path, text, create):
        parts(path); bounded(text, FILE_LIMIT)
        if not isinstance(text, str) or '\0' in text: raise ValueError('Expected UTF-8 text')
        old = self.edits.get(path)
        original, mode = (old['original'], old['mode']) if old else self.disk(path)
        exists = old is not None or original is not None
        if create == exists: raise ValueError('Create requires absent file; edit requires existing file')
        prospective = dict(self.edits, **{path: dict(original=original, text=text, mode=mode)})
        bounded(prospective, 2 * 1024 * 1024)
        self.edits = prospective; self.persist()
        return {'staged': path, 'applied': False}

    def diff(self):
        chunks = []
        for path, edit in sorted(self.edits.items()):
            original = edit['original']
            before = 'a/'+path if original is not None else '/dev/null'
            lines = list(difflib.unified_diff((original or '').splitlines(keepends=True),
                         edit['text'].splitlines(keepends=True), fromfile=before, tofile='b/'+path))
            if original is None and not lines: lines = ['--- /dev/null\n', '+++ b/'+path+'\n', '(new empty file)\n']
            for line in lines:
                chunks.append(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n')
        return bounded(''.join(chunks))

    def discard(self):
        self.edits = {}; self.incomplete = False; self.persist()

    def apply(self):
        if self.incomplete: raise ValueError('Draft is incomplete; finish a successful goal or /discard first')
        self.diff()  # Enforce a reviewable bounded diff before any writes.
        # Preflight EVERY original before replacing ANY file.
        for path, edit in self.edits.items():
            actual, _ = self.disk(path)
            if identity(actual) != identity(edit['original']): raise ValueError('Conflict: project changed; no files applied')
        applied = []
        try:
            for path, edit in self.edits.items():
                with self.parent(path, create=True) as (directory, name):
                    # Recheck after preflight; no claim of a multi-file transaction.
                    actual, _ = self.disk(path)
                    if identity(actual) != identity(edit['original']): raise ValueError('Conflict during apply')
                    temporary = '.file-agent-' + os.urandom(12).hex()
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as out:
                            out.write(edit['text']); out.flush(); os.fchmod(out.fileno(), edit['mode'])
                        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                    finally:
                        try: os.unlink(temporary, dir_fd=directory)
                        except FileNotFoundError: pass
                applied.append(path)
        except (OSError, ValueError, KeyboardInterrupt):
            # Keep remaining draft useful after a partial filesystem failure.
            for path in applied: del self.edits[path]
            self.incomplete = True; self.persist()
            raise ValueError(f'Apply interrupted after {len(applied)} file(s); remaining draft incomplete') from None
        self.discard()
        return applied
