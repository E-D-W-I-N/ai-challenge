"""Read-only local inspector. Storage path is operator configuration only."""
import sqlite3

from fastapi import APIRouter, HTTPException, Query

from rag.index import Index

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.get("/status")
def status():
    return Index().status()


def read(call):
    try:
        return call(Index())
    except KeyError:
        raise HTTPException(404, "Chunk not found") from None
    except (sqlite3.Error, OSError, ValueError) as error:
        raise HTTPException(409, f"RAG index unavailable: {error}") from None


@router.get("/documents")
def documents(offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    return {"items": read(lambda index: index.documents(offset, limit)), "offset": offset, "limit": limit}


@router.get("/documents/{document_id}/chunks")
def chunks(document_id: str, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
    return {"items": read(lambda index: index.chunks(document_id, offset, limit)), "offset": offset, "limit": limit}


@router.get("/chunks/{chunk_id}")
def chunk(chunk_id: str, vector: bool = False):
    return read(lambda index: index.chunk(chunk_id, vector))
