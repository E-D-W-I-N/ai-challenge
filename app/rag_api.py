"""Local durable RAG workflow; paths remain operator configuration only."""
import json
import os
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


@router.get("/status")
def status():
    index = Index()
    result = index.status()
    try:
        result["stages"] = workflow.stages(index.root)
    except (OSError, ValueError, KeyError) as error:
        result["stage_error"] = str(error)
    result["embedding_defaults"] = (result.get("stages", {}).get("embeddings") or result.get("index") or {}).get("embedding_config", EmbeddingConfig().__dict__)
    result["manifest_available"] = bool(os.environ.get("RAG_MANIFEST"))
    return result


@router.get("/models", dependencies=[Depends(trusted_rag_request)])
async def model_catalogue(auth_mode: Literal["openrouter", "omlx"], base_url: str = Query(max_length=2048)):
    from .rag_models import models
    return await models(auth_mode, base_url)


class StageRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=100)
    use_manifest: bool = False
    preparation_strategy: Literal["programmatic", "llm"] = "programmatic"
    preparation_base_url: str = PreparationConfig.base_url
    preparation_model: str = PreparationConfig.model
    preparation_auth_mode: Literal["openrouter", "omlx"] = PreparationConfig.auth_mode
    strategy: str = "fixed"
    size: int = Field(default=1200, ge=64, le=100000, strict=True)
    overlap: int = Field(default=180, ge=0, strict=True)
    base_url: str = EmbeddingConfig.base_url
    model: str = EmbeddingConfig.model
    dimensions: int | None = Field(default=None, gt=0, strict=True)
    revision: str = "1"
    semantic_base_url: str = SemanticConfig.base_url
    semantic_model: str = SemanticConfig.model
    semantic_auth_mode: Literal["openrouter", "omlx"] = SemanticConfig.auth_mode
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
        preparation_config = PreparationConfig(body.preparation_base_url, body.preparation_model, auth_mode=body.preparation_auth_mode)
        semantic_config = SemanticConfig(body.semantic_base_url, body.semantic_model, auth_mode=body.semantic_auth_mode)
        if body.strategy == "semantic" and body.size > 12000:
            raise ValueError("Semantic chunk size must not exceed 12000 characters")
        config = EmbeddingConfig(body.base_url, body.model, body.dimensions, body.revision)
        inputs = [{"url": url} for url in body.urls]
        if kind == "ingest":
            from urllib.parse import urlparse
            if any(urlparse(url).scheme not in {"http", "https"} or not urlparse(url).netloc or urlparse(url).username or urlparse(url).password for url in body.urls):
                raise ValueError("Provide explicit HTTP(S) URLs without credentials")
            if body.use_manifest:
                configured = os.environ.get("RAG_MANIFEST")
                if not configured:
                    raise ValueError("Operator manifest is not configured")
                manifest = Path(configured).resolve()
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
