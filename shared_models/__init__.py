"""Provider contracts shared by app and standalone RAG; no app or dotenv imports."""
from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from urllib.parse import urlparse

PROVIDERS = ("openrouter", "compatible")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_COMPATIBLE_BASE_URL = "http://127.0.0.1:8005/v1"
DEFAULT_GENERATIVE_MODEL = "openai/gpt-6-luna"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_bound_url = ContextVar("compatible_model_url", default=DEFAULT_COMPATIBLE_BASE_URL)


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Model URL must be HTTP(S) without credentials, query or fragment")
    url = urlparse(value)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise ValueError("Model URL must be HTTP(S) without credentials, query or fragment")
    url.port
    return value.rstrip("/")


def provider(value: str) -> str:
    if value not in PROVIDERS:
        raise ValueError("Provider must be openrouter or compatible")
    return value


def legacy_provider(value: str) -> str:
    # Only archived auth_mode configurations call this migration.
    if value == "omlx":
        return "compatible"
    return provider(value)


def endpoint(value: str, compatible_base_url: str | None = None) -> str:
    return OPENROUTER_BASE_URL if provider(value) == "openrouter" else validate_url(compatible_base_url or _bound_url.get())


def key(value: str) -> str:
    return os.environ.get("OPENROUTER_API_KEY" if provider(value) == "openrouter" else "RAG_EMBEDDING_API_KEY", "").strip()


@contextlib.contextmanager
def bind_compatible_url(value: str):
    token = _bound_url.set(validate_url(value))
    try:
        yield
    finally:
        _bound_url.reset(token)


def generation_payload(payload: dict, value: str, *, reasoning_enabled: bool = False) -> dict:
    """Only OpenRouter receives routing/accounting extensions."""
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
    if provider(value) == "openrouter":
        result["provider"] = {**(result.get("provider") or {}), "require_parameters": True}
        result["reasoning"] = {"enabled": True, "exclude": False} if reasoning_enabled else {"effort": "none", "enabled": False, "exclude": False}
        result.setdefault("usage", {"include": True})
        result.setdefault("plugins", [{"id": "context-compression", "enabled": False}])
    else:
        result["reasoning_effort"] = "medium" if reasoning_enabled else "none"
        for name in ("provider", "plugins", "usage", "reasoning", "transforms", "route", "models"):
            result.pop(name, None)
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
