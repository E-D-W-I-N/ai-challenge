"""Bounded compatible HTTP operations with explicit failures and no retries."""
import json

import httpx
from shared_models import generation_payload, reports_reasoning
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

    def answer(self, messages, tools):
        s = self.settings
        if not s['chat_model']: raise ModelError('Explicit chat model is required')
        controls = dict(model=s['chat_model'], messages=messages, temperature=s['temperature'], tools=tools, tool_choice='auto')
        if s['max_tokens'] is not None:
            controls['max_tokens'] = s['max_tokens']
        payload = generation_payload(controls, reasoning_enabled=s['reasoning_enabled'])
        body = self.request('POST', '/chat/completions', payload)
        if not s['reasoning_enabled'] and reports_reasoning(body):
            raise ModelError('Server reported reasoning while disabled; response rejected')
        try:
            choice = body['choices'][0]
            message = choice['message']
            finish = choice['finish_reason']
            if not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant': raise ValueError()
            content, calls = message.get('content'), message.get('tool_calls')
            if content is not None and not isinstance(content, str): raise ValueError()
            if not calls and (not isinstance(content, str) or not content.strip()): raise ValueError()
            if calls is not None and (not isinstance(calls, list) or len(calls) > 12): raise ValueError()
            if finish != ('tool_calls' if calls else 'stop'):
                raise ModelError('Incomplete or unsupported generation finish reason; response rejected')
            return dict(role='assistant', content=content, **({'tool_calls': calls} if calls else {}))
        except (KeyError, TypeError, IndexError, ValueError):
            raise ModelError('Invalid generation response') from None
