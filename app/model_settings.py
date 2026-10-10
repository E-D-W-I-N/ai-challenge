"""One private connection file; public settings contain no credential."""
import json
import os
import tempfile
import threading
from pathlib import Path
from shared_models import Connection, load_connection, validate_url
from .config import ROOT

DEFAULT_CONNECTION_PATH = ROOT / "data" / "model-connection.json"
_lock = threading.Lock()


def connection_path(store=None):
    return Path(store.path).parent / "model-connection.json" if store is not None and str(store.path) != ":memory:" else DEFAULT_CONNECTION_PATH


def current_connection(store=None):
    if store is None:
        from .store import _STORE
        store = _STORE
    return load_connection(connection_path(store))


def settings(store=None):
    return current_connection(store).public()


def save_settings(payload, store=None):
    if not isinstance(payload, dict) or not payload or set(payload) - {"base_url", "api_key"}:
        raise ValueError("Only base_url and api_key are accepted")
    with _lock:
        old = current_connection(store)
        url = validate_url(payload.get("base_url", old.base_url))
        secret = payload.get("api_key", old.api_key)
        if not isinstance(secret, str) or len(secret) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in secret):
            raise ValueError("API key must be text without control characters")
        secret = secret.strip()
        saved = Connection(url, secret, old.revision + int((url, secret) != (old.base_url, old.api_key)))
        path = connection_path(store)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".model-connection-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"base_url": saved.base_url, "api_key": secret, "revision": saved.revision}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return saved.public()
