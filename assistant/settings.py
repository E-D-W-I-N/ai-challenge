"""Private per-worktree settings; never stored inside the indexed project."""
import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from shared_models import Connection


def project_root(path):
    import subprocess
    try:
        result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, check=True, timeout=10)
        return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Project must be a Git working tree') from None


def state_root(project, explicit=None):
    identity = hashlib.sha256(str(project).encode()).hexdigest()[:24]
    path = Path(explicit).expanduser().resolve() if explicit else Path.home() / '.local/share/project-assistant' / identity
    if path == project or project in path.parents:
        raise ValueError('State directory must be outside the project')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def defaults():
    return dict(connection=asdict(Connection()), chat_model='', embedding_model='',
                mcp_url='http://127.0.0.1:8017/mcp', temperature=0.2, max_tokens=512,
                reasoning_enabled=False, includes=['README*', 'docs/**', '**/docs/**',
                '**/README*', '*schema*', '**/*schema*', '*openapi*', '**/*openapi*', '*swagger*', '**/*swagger*'],
                excludes=[], chunk_lines=60, top_k=5, min_score=0.2)


def validate(value):
    secret=value['connection'].get('api_key','')
    if not isinstance(secret,str) or len(secret)>4096 or any(ord(c)<32 or ord(c)==127 for c in secret):
        raise ValueError('Invalid API key')
    Connection(**value['connection'])
    from shared_models import validate_url
    validate_url(value['mcp_url'])
    if type(value['reasoning_enabled']) is not bool:
        raise ValueError('Reasoning must be true or false')
    for key, low, high in [('max_tokens', 1, 100000), ('chunk_lines', 10, 200), ('top_k', 1, 20)]:
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError(f'Invalid {key}')
    for key, low, high in [('temperature', 0, 2), ('min_score', -1, 1)]:
        if type(value[key]) not in (int, float) or not low <= value[key] <= high:
            raise ValueError(f'Invalid {key}')
    for key in ['includes', 'excludes']:
        if not isinstance(value[key], list) or any(not isinstance(x, str) or not x or len(x)>512 for x in value[key]):
            raise ValueError(f'{key} must be a JSON array of glob patterns')
    for key in ['chat_model', 'embedding_model']:
        if not isinstance(value[key], str) or len(value[key])>256 or any(ord(c)<32 for c in value[key]):
            raise ValueError(f'Invalid {key}')
    return value


def load(state):
    try:
        value = defaults()
        if (state / 'settings.json').exists():
            value.update(json.loads((state / 'settings.json').read_text()))
        return validate(value)
    except (OSError, TypeError, KeyError, ValueError, AttributeError):
        raise ValueError('Invalid private settings file') from None


def save(state, value):
    validate(value)
    fd, name = tempfile.mkstemp(dir=state, prefix='.settings-')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, state / 'settings.json')
    finally:
        if os.path.exists(name): os.unlink(name)
