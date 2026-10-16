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
        self.connection = settings['connection']
        self.usage = []

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
            usage = value.get('usage')
            allowed = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens'}
            self.usage.append({'endpoint': endpoint, 'tokens': {k: v for k, v in (usage.items() if isinstance(usage, dict) else []) if k in allowed and type(v) is int and v >= 0}})
            return value
        except ModelError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError):
            raise ModelError('Model transport or response error; check connection, proxy and model') from None

    def embed(self, texts, model):
        if not model: raise ModelError('Explicit embedding model is required')
        body = self.request('POST', '/embeddings', {'model': model, 'input': texts})
        try:
            rows = sorted(body['data'], key=lambda row: row['index'])
            if [row['index'] for row in rows] != list(range(len(texts))): raise ValueError()
            vectors = [row['embedding'] for row in rows]
            dims = len(vectors[0])
            if not dims or any(len(v)!=dims or any(type(x) not in (int,float) or not math.isfinite(x) for x in v) or not any(v) for v in vectors): raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            raise ModelError('Invalid embedding vectors') from None

    def answer(self, messages):
        s = self.settings
        if not s['chat_model']: raise ModelError('Explicit chat model is required')
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
