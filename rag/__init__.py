"""Local document indexing. Independent of chat configuration and MCP."""
from .chunks import chunk_documents
from .index import Index, build_index
from .documents import ingest, load_corpus

__all__ = ["Index", "build_index", "chunk_documents", "ingest", "load_corpus"]
