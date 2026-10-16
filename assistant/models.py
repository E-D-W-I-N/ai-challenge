"""Bounded compatible HTTP operations with explicit failures and no retries."""
import json
import math

import httpx
from shared_models import Connection, generation_payload, reports_reasoning
from shared_models.transport import client_options, create_client


class ModelError(ValueError):
    pass


class Models:
    def __init__(self, settings):
        self.settings = settings
        self.connection = Connection(**settings['connection'])

    def request(self, method, endpoint, payload=None):
        try:
            options = client_options(self.connection.base_url)
            headers = {'Authorization': f'Bearer {self.connection.api_key}'} if self.connection.api_key else {}
            with create_client(httpx.Client, **options, timeout=60, follow_redirects=False) as client:
                with client.stream(method, self.connection.base_url + endpoint, json=payload, headers=headers) as response:
                    if response.status_code >= 300:
                        raise ModelError(f'Model HTTP {response.status_code}; check connection and parameters')
                    raw = bytearray()
                    for part in response.iter_bytes():
                        raw.extend(part)
                        if len(raw) > 16 * 1024 * 1024: raise ModelError('Model response exceeds limit')
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError()
            return value
        except ModelError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            raise ModelError('Model transport or response error; check connection, proxy and model') from None

    def catalog(self):
        body = self.request('GET', '/models')
        try:
            rows = body['data']
            if not isinstance(rows, list): raise ValueError()
            ids = [row['id'] for row in rows]
            if any(not isinstance(x, str) or not x or len(x)>256 or any(ord(c)<32 for c in x) for x in ids): raise ValueError()
            return ids
        except (KeyError, TypeError, ValueError):
            raise ModelError('Invalid /models catalog') from None

    def embed(self, texts, model):
        if not model: raise ModelError('Choose an embedding model in /settings')
        body = self.request('POST', '/embeddings', {'model': model, 'input': texts})
        try:
            rows = sorted(body['data'], key=lambda row: row['index'])
            if [row['index'] for row in rows] != list(range(len(texts))): raise ValueError()
            vectors = [row['embedding'] for row in rows]
            dims = len(vectors[0])
            if not dims or any(len(v)!=dims or any(type(x) not in (int,float) or not math.isfinite(x) for x in v) or not any(v) for v in vectors): raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError, IndexError):
            raise ModelError('Invalid embedding vectors') from None

    def answer(self, messages):
        s = self.settings
        if not s['chat_model']: raise ModelError('Choose a generation model in /settings')
        payload = generation_payload(dict(model=s['chat_model'], messages=messages,
            temperature=s['temperature'], max_tokens=s['max_tokens']), reasoning_enabled=s['reasoning_enabled'])
        body = self.request('POST', '/chat/completions', payload)
        if not s['reasoning_enabled'] and reports_reasoning(body):
            raise ModelError('Server reported reasoning while disabled; response rejected')
        try:
            answer = body['choices'][0]['message']['content']
            if not isinstance(answer, str) or not answer.strip(): raise ValueError()
            return answer, body.get('usage')
        except (KeyError, TypeError, IndexError, ValueError):
            raise ModelError('Invalid generation response') from None
