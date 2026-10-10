"""Provider contracts shared by app and standalone RAG; no app or dotenv imports."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from dataclasses import dataclass, field
from contextvars import ContextVar
from urllib.parse import urlparse

DEFAULT_COMPATIBLE_BASE_URL = "http://127.0.0.1:8005/v1"
DEFAULT_GENERATIVE_MODEL = ""
DEFAULT_EMBEDDING_MODEL = ""
_bound_connection = ContextVar("model_connection", default=None)


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Model URL must be HTTP(S) without credentials, query or fragment")
    url = urlparse(value)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError("Model URL must be HTTP(S) without credentials, query or fragment")
    url.port
    return value.rstrip("/")


@dataclass(frozen=True)
class Connection:
    base_url: str = DEFAULT_COMPATIBLE_BASE_URL
    api_key: str = field(default="", repr=False)
    revision: int = 0

    def __post_init__(self):
        object.__setattr__(self, "base_url", validate_url(self.base_url))
        object.__setattr__(self, "api_key", self.api_key.strip())

    def public(self):
        return {"base_url": self.base_url, "has_api_key": bool(self.api_key), "revision": self.revision}


def load_connection(path=None):
    if path is None or not Path(path).exists():
        return Connection()
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        secret = value.get("api_key", "")
        revision = value.get("revision", 0)
        if not isinstance(secret, str) or len(secret) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            raise ValueError()
        if type(revision) is not int or revision < 0:
            raise ValueError()
        return Connection(validate_url(value["base_url"]), secret.strip(), revision)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Invalid model connection file") from None


def connection():
    return _bound_connection.get() or Connection()


def endpoint():
    return connection().base_url


def key():
    return connection().api_key


@contextlib.contextmanager
def bind_connection(value):
    token = _bound_connection.set(value)
    try:
        yield
    finally:
        _bound_connection.reset(token)


def generation_payload(payload: dict, *, reasoning_enabled: bool = False) -> dict:
    """Apply common reasoning controls after explicit request fields."""
    if type(reasoning_enabled) is not bool:
        raise ValueError("reasoning_enabled must be boolean")
    result = dict(payload)
    for name in ("reasoning", "reasoning_effort", "include_reasoning", "enable_thinking", "thinking", "thinking_budget"):
        result.pop(name, None)
    if not reasoning_enabled:
        for message in result.get("messages") or []:
            if isinstance(message, dict) and any(isinstance(message.get(name), dict) and field in message[name]
                    for name, field in (("configuration_update", "reasoning"), ("output_config", "effort"))):
                raise ValueError("Per-message reasoning controls require reasoning_enabled")
    for name, controls in (("chat_template_kwargs", ("enable_thinking", "reasoning_effort", "thinking_budget")), ("output_config", ("effort",))):
        if isinstance(result.get(name), dict):
            cleaned = {key: item for key, item in result[name].items() if key not in controls}
            if cleaned:
                result[name] = cleaned
            else:
                result.pop(name)
    result["reasoning_effort"] = "medium" if reasoning_enabled else "none"
    if result.get("stream"):
        result["stream_options"] = {**(result.get("stream_options") or {}), "include_usage": True}
    return result


def reports_reasoning(body: dict) -> bool:
    """Observable provider evidence only; absence cannot prove internal behavior."""
    if not isinstance(body, dict):
        return False
    usage = body.get("usage")
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    details = details if isinstance(details, dict) else {}
    tokens = details.get("reasoning_tokens")
    if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens > 0:
        return True
    choices = body.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        frame = choice.get("delta") or choice.get("message") or {}
        if not isinstance(frame, dict):
            continue
        if any(isinstance(frame.get(name), str) and frame[name].strip()
               for name in ("reasoning", "reasoning_content")) or frame.get("reasoning_details"):
            return True
    return False
