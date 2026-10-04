"""Поиск ключа OpenRouter и настроек. Ключ никогда не логируется и не отдаётся в API."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DOTENV_LOADED = False


def _load_dotenv() -> None:
    """Читает .env в корне репозитория. Уже заданные переменные окружения
    сильнее файла: так стенд подставляет свои, не трогая .env."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"OPENROUTER_API_KEY", "RAG_EMBEDDING_API_KEY"} and key not in os.environ:
            os.environ[key] = value




def api_key() -> str | None:
    _load_dotenv()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    return key or None


def has_key() -> bool:
    return api_key() is not None


def attribution_headers() -> dict[str, str]:
    return {}
