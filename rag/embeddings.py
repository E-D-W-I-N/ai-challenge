"""OpenAI-compatible external embeddings; no model installation/inference here."""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx

from .documents import digest
from shared_models import DEFAULT_COMPATIBLE_BASE_URL, DEFAULT_EMBEDDING_MODEL


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str = DEFAULT_COMPATIBLE_BASE_URL
    model: str = DEFAULT_EMBEDDING_MODEL
    dimensions: int | None = None
    # Change revision when replacing weights behind the same server/model name.
    revision: str = "1"
    # None is archived compatible identity: do not change its serialized fingerprint.
    reasoning_enabled: bool = False

    def __post_init__(self):
        if type(self.reasoning_enabled) is not bool:
            raise ValueError("reasoning_enabled must be boolean")
        from shared_models import validate_url
        validate_url(self.base_url)
        url = urlparse(self.base_url)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("Embedding endpoint must be HTTP(S) without embedded credentials")
        if not isinstance(self.model, str) or not self.revision.strip():
            raise ValueError("Embedding model/revision must be nonempty")
        if self.dimensions is not None and (type(self.dimensions) is not int or self.dimensions <= 0):
            raise ValueError("Embedding dimensions must be a positive integer")

    def fingerprint(self):
        identity = asdict(self)
        identity.pop("reasoning_enabled")
        return digest(json.dumps(identity, sort_keys=True, separators=(",", ":")))


def normalize(vector, dimension=None):
    if not isinstance(vector, list) or not vector or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
        raise ValueError("Embedding must be a finite numerical vector")
    if dimension is not None and len(vector) != dimension:
        raise ValueError(f"Embedding dimension mismatch: expected {dimension}, got {len(vector)}")
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise ValueError("Embedding must have finite nonzero norm")
    return [x / norm for x in vector]


class Embeddings:
    def __init__(self, config: EmbeddingConfig, client=None):
        self.config, self.client = config, client

    def embed(self, texts):
        if not self.config.model.strip():
            raise ValueError("Choose an embedding model before running this stage")
        if not texts:
            return []
        payload = {"model": self.config.model, "input": texts, "encoding_format": "float"}
        if self.config.dimensions is not None:
            payload["dimensions"] = self.config.dimensions
        def call(client):
            # Runtime credential only: never part of config, cache identity or state.
            from shared_models import key as model_key
            key = model_key()
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            try:
                from shared_models.admission import slot, validate_payload
                with slot((self.config.model,), "embedding"):
                    validate_payload(payload)
                    response = client.post(self.config.base_url.rstrip("/") + "/embeddings", json=payload, headers=headers)
                    response.raise_for_status()
            except httpx.HTTPStatusError as error:
                raise ValueError(f"Embedding HTTP error: status {error.response.status_code}") from None
            except (httpx.HTTPError, UnicodeError):
                # Transport errors may contain request headers; keep persisted errors safe.
                raise ValueError("Embedding HTTP request failed") from None
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Embedding response must be a JSON object")
            if body.get("model") not in {None, self.config.model}:
                raise ValueError("Embedding server returned another model")
            rows = body.get("data")
            if not isinstance(rows, list) or len(rows) != len(texts):
                raise ValueError("Embedding response count mismatch")
            by_index = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Embedding data rows must be JSON objects")
                index = row.get("index")
                if type(index) is not int or not 0 <= index < len(texts) or index in by_index:
                    raise ValueError("Embedding response indices invalid")
                by_index[index] = normalize(row.get("embedding"), self.config.dimensions)
            vectors = [by_index[i] for i in range(len(texts))]
            if len({len(v) for v in vectors}) != 1:
                raise ValueError("Embedding batch dimensions differ")
            return vectors
        if self.client is not None:
            return call(self.client)
        with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
            return call(client)
