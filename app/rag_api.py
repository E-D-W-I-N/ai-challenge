"""Local durable RAG workflow; paths remain operator configuration only."""
import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from rag import workflow
from rag.artifacts import clear
from rag.documents import ingest
from rag.embeddings import EmbeddingConfig
from rag.semantic import SemanticConfig
from rag.preparation import PreparationConfig
from rag.index import Index, Operation, stage_chunks, stage_embeddings, save_index
from .request_security import trusted_rag_request

router = APIRouter(prefix="/api/rag", tags=["rag"])


def editable_config(value, *, embedding=False):
    """Canonical selector DTO only; published/cache identities stay untouched."""
    from shared_models import legacy_provider, provider
    result = copy.deepcopy(value)
    selected = result.get("provider")
    if selected is None:
        selected = "compatible" if embedding else legacy_provider(result.get("auth_mode", "openrouter"))
    result["provider"] = provider(selected)
    result.pop("auth_mode", None)
    return result


@router.get("/status")
def status():
    index = Index()
    result = index.status()
    try:
        stages = copy.deepcopy(workflow.stages(index.root))
        for stage, field in (("corpus", "preparation_config"), ("chunks", "semantic_config")):
            if isinstance(stages.get(stage), dict) and isinstance(stages[stage].get(field), dict):
                stages[stage][field] = editable_config(stages[stage][field])
        result["stages"] = stages
    except (OSError, ValueError, KeyError) as error:
        result["stage_error"] = str(error)
    defaults = (result.get("stages", {}).get("embeddings") or result.get("index") or {}).get("embedding_config", EmbeddingConfig().__dict__)
    try:
        result["embedding_defaults"] = editable_config(defaults, embedding=True)
    except ValueError as error:
        result["stage_error"] = str(error)
        result["embedding_defaults"] = None
    result["manifest_available"] = (index.root / "inputs.json").is_file()
    return result


class StageRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=100)
    use_manifest: bool = False
    preparation_strategy: Literal["programmatic", "llm"] = "programmatic"
    preparation_reasoning_enabled: bool = Field(default=False, strict=True)
    preparation_model: str = PreparationConfig.model
    preparation_provider: Literal["openrouter", "compatible"] = "openrouter"
    preparation_timeout_seconds: float = Field(default=PreparationConfig.timeout_seconds, ge=1, le=3600, strict=True, allow_inf_nan=False)
    strategy: str = "fixed"
    size: int = Field(default=1200, ge=64, le=100000, strict=True)
    overlap: int = Field(default=180, ge=0, strict=True)
    provider: Literal["openrouter", "compatible"] = "compatible"
    reasoning_enabled: bool = Field(default=False, strict=True)
    model: str = EmbeddingConfig.model
    dimensions: int | None = Field(default=None, gt=0, strict=True)
    revision: str = "1"
    semantic_reasoning_enabled: bool = Field(default=False, strict=True)
    semantic_model: str = SemanticConfig.model
    semantic_provider: Literal["openrouter", "compatible"] = "openrouter"
    batch_size: int = Field(default=16, ge=1, le=256, strict=True)

    class Config:
        extra = "forbid"


@router.post("/operations/{kind}", status_code=202, dependencies=[Depends(trusted_rag_request)])
def start(kind: str, body: StageRequest):
    if kind not in {"ingest", "chunks", "embeddings", "save"}:
        raise HTTPException(404, "Unknown RAG stage")
    if body.strategy not in {"fixed", "structural", "semantic"} or body.overlap >= body.size:
        raise HTTPException(422, "Invalid chunk strategy/overlap")
    index = Index()
    try:
        from shared_models import endpoint
        from .model_settings import settings
        from .registry import REGISTRY
        from .config import api_key
        compatible_url = settings(REGISTRY.store)["compatible_base_url"]
        api_key()
        preparation_config = PreparationConfig(endpoint(body.preparation_provider, compatible_url), body.preparation_model, body.preparation_timeout_seconds, provider=body.preparation_provider, reasoning_enabled=body.preparation_reasoning_enabled)
        semantic_config = SemanticConfig(endpoint(body.semantic_provider, compatible_url), body.semantic_model, provider=body.semantic_provider, reasoning_enabled=body.semantic_reasoning_enabled)
        if body.strategy == "semantic" and body.size > 12000:
            raise ValueError("Semantic chunk size must not exceed 12000 characters")
        config = EmbeddingConfig(endpoint(body.provider, compatible_url), body.model, body.dimensions, body.revision, provider=body.provider, reasoning_enabled=body.reasoning_enabled)
        inputs = [{"url": url} for url in body.urls]
        if kind == "ingest":
            from urllib.parse import urlparse
            if any(urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc or urlparse(url).username or urlparse(url).password for url in body.urls):
                raise ValueError("Provide explicit HTTP(S) URLs without credentials")
            if body.use_manifest:
                manifest = index.root / "inputs.json"
                if not manifest.is_file():
                    raise ValueError("Operator manifest data/rag/inputs.json is not available")
                entries = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
                    raise ValueError("Invalid operator manifest")
                inputs.extend({**e, **({"path": str((manifest.parent / e["path"]).resolve())} if "path" in e else {})} for e in entries)
            if not inputs:
                raise ValueError("Provide URLs or choose the operator manifest")
        operation = Operation(index.root, kind)
        operation.__enter__()  # Reserve the cross-process writer before acknowledging.
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(409, str(error)) from None

    def run():
        try:
            if kind == "ingest":
                report = ingest(inputs, index.root, operation=operation, preparation_strategy=body.preparation_strategy, preparation_config=preparation_config)
                operation.update(documents=report["documents"], words=report["words"], state="complete")
            elif kind == "chunks":
                stage_chunks(index.root, body.strategy, body.size, body.overlap, operation=operation, semantic_config=semantic_config)
            elif kind == "embeddings":
                stage_embeddings(index.root, config, body.batch_size, operation=operation)
            else:
                save_index(index.root, operation=operation)
        except Exception as error:
            operation.__exit__(type(error), error, error.__traceback__)
        else:
            operation.__exit__(None, None, None)

    try:
        threading.Thread(target=run, name="rag-operation", daemon=True).start()
    except Exception as error:
        operation.__exit__(type(error), error, error.__traceback__)
        raise HTTPException(503, "Could not start RAG worker") from None
    return {"operation_id": operation.value["operation_id"], "state": "running"}


def read(call):
    try:
        return call(Index())
    except KeyError:
        raise HTTPException(404, "Chunk not found") from None
    except (sqlite3.Error, OSError, ValueError) as error:
        raise HTTPException(409, f"RAG data unavailable: {error}") from None


@router.get("/documents")
def documents(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), working: bool = False):
    return {"items": read(lambda index: workflow.documents(index.root, offset, limit) if working else index.documents(offset, limit)), "offset": offset, "limit": limit}


@router.get("/documents/{document_id}/chunks")
def chunks(document_id: str, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), working: bool = False):
    return {"items": read(lambda index: workflow.chunks(index.root, document_id, offset, limit) if working else index.chunks(document_id, offset, limit)), "offset": offset, "limit": limit}


@router.get("/chunks/{chunk_id}")
def chunk(chunk_id: str, vector: bool = False, working: bool = False):
    return read(lambda index: workflow.chunk(index.root, chunk_id, vector) if working else index.chunk(chunk_id, vector))


@router.delete("/stages/{kind}", dependencies=[Depends(trusted_rag_request)])
def delete_stage(kind: str):
    if kind not in {"chunks", "embeddings", "index"}:
        raise HTTPException(404, "Unknown RAG stage")
    try:
        index = Index()
        with Operation(index.root, "delete_" + kind) as operation:
            return clear(index.root, kind, operation)
    except (OSError, ValueError) as error:
        raise HTTPException(409, str(error)) from None


@router.get("/documents/{document_id}")
def document(document_id: str, offset: int = Query(0, ge=0), limit: int = Query(10000, ge=1, le=10000)):
    return read(lambda index: workflow.document(index.root, document_id, offset, limit))
