"""Private atomic settings, independent of knowledge and ticket JSON."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import tempfile

from shared_models import Connection
from shared_models.client import Models


def state_directory(root, explicit=None):
    root = Path(root).resolve(strict=True)
    base = Path(explicit).expanduser() if explicit else Path.home() / '.local/state/support-assistant' / hashlib.sha256(str(root).encode()).hexdigest()[:24]
    base = base.resolve()
    if base.is_relative_to(root):
        raise ValueError('State directory must be outside knowledge directory')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    return base


def load(path):
    if not path.exists():
        return dict(connection=Connection(), chat_model='', embedding_model='', temperature=0.2, max_tokens=1024, reasoning_enabled=False)
    try:
        if path.stat().st_size > 16384: raise ValueError()
        data = json.loads(path.read_text())
        config = data.pop('connection')
        if not isinstance(config, dict) or set(config) != {'base_url', 'api_key'}:
            raise ValueError()
        if not isinstance(config['api_key'], str) or len(config['api_key']) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in config['api_key']):
            raise ValueError()
        connection = Connection(**config)
        if set(data) != {'chat_model', 'embedding_model', 'temperature', 'max_tokens', 'reasoning_enabled'}: raise ValueError()
        if any(not isinstance(data[k], str) for k in ('chat_model', 'embedding_model')): raise ValueError()
        if type(data['reasoning_enabled']) is not bool or type(data['max_tokens']) is not int or not 1 <= data['max_tokens'] <= 32768: raise ValueError()
        if type(data['temperature']) not in (int, float) or not 0 <= data['temperature'] <= 2: raise ValueError()
        return dict(data, connection=connection)
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('Invalid private settings') from None


def save(path, settings):
    data = dict(settings, connection=dict(base_url=settings['connection'].base_url, api_key=settings['connection'].api_key))
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def choose_models(settings):
    catalog = Models(settings).request('GET', '/models').get('data')
    if not isinstance(catalog, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) for x in catalog):
        raise ValueError('Invalid model catalog')
    ids = list(dict.fromkeys(x['id'] for x in catalog))
    for i, name in enumerate(ids, 1): print(f'{i}: {name}')
    if not ids: raise ValueError('Empty model catalog')
    selected = dict(settings)
    for key in ('chat_model', 'embedding_model'):
        try:
            number = int(input(f'{key} number: '))
        except ValueError:
            raise ValueError('Invalid model choice') from None
        if not 1 <= number <= len(ids): raise ValueError('Invalid model choice')
        selected[key] = ids[number-1]
    return selected


def configure(settings):
    print('Connection:', settings['connection'].public())
    print({k: v for k,v in settings.items() if k != 'connection'})
    url = input('Model base URL (blank preserves): ').strip() or settings['connection'].base_url
    key = getpass.getpass('API key (blank preserves; - removes): ')
    if len(key) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in key):
        raise ValueError('Invalid API key format')
    updated = dict(settings, connection=Connection(url, settings['connection'].api_key if not key else '' if key == '-' else key))
    updated = choose_models(updated)
    reasoning = input('Enable reasoning? [y/N]: ').strip().lower()
    if reasoning not in ('', 'n', 'y'): raise ValueError('Expected y or n')
    updated['reasoning_enabled'] = reasoning == 'y'
    for key, cast in [('temperature', float), ('max_tokens', int)]:
        value = input(f'{key} [{updated[key]}]: ').strip()
        if value:
            try: updated[key] = cast(value)
            except ValueError: raise ValueError('Invalid generation parameter') from None
    if not 0 <= updated['temperature'] <= 2 or not 1 <= updated['max_tokens'] <= 32768:
        raise ValueError('temperature must be 0..2; max_tokens 1..32768')
    return updated
