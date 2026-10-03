"""Durable stage inspection and bounded previews before index publication."""
from .documents import load_corpus
from .index import load_chunks, load_vectors


def stages(root):
    result = {"corpus": None, "chunks": None, "embeddings": None}
    try:
        corpus = load_corpus(root)
    except FileNotFoundError:
        return result
    result["corpus"] = {"fingerprint": corpus["fingerprint"], "documents": len(corpus["documents"]),
                        "words": sum(d["words"] for d in corpus["documents"]),
                        "urls": [d["source"] for d in corpus["documents"] if d["source"].startswith(("http://", "https://"))]}
    try:
        _, chunks = load_chunks(root)
    except (FileNotFoundError, ValueError):
        return result
    result["chunks"] = {key: value for key, value in chunks.items() if key != "chunks"} | {"chunks": len(chunks["chunks"])}
    try:
        embeddings = load_vectors(root, chunks)
    except (FileNotFoundError, ValueError):
        return result
    result["embeddings"] = {key: value for key, value in embeddings.items() if key != "vectors"}
    return result


def documents(root, offset, limit):
    return [{k: v for k, v in d.items() if k not in {"text", "blocks"}} | {"characters": len(d["text"])}
            for d in load_corpus(root)["documents"][offset:offset + limit]]


def chunks(root, document_id, offset, limit):
    _, value = load_chunks(root)
    rows = [c for c in value["chunks"] if c["document_id"] == document_id]
    return [{k: v for k, v in c.items() if k != "text"} | {"characters": len(c["text"])} for c in rows[offset:offset + limit]]


def chunk(root, chunk_id, vector=False):
    _, value = load_chunks(root)
    for item in value["chunks"]:
        if item["chunk_id"] == chunk_id:
            if vector:
                return item | {"vector": load_vectors(root, value)["vectors"][item["content_hash"]]}
            return item
    raise KeyError(chunk_id)


def document(root, document_id, offset=0, limit=10000):
    for item in load_corpus(root)["documents"]:
        if item["document_id"] == document_id:
            return {k: v for k, v in item.items() if k not in {"text", "blocks"}} | {
                "text": item["text"][offset:offset + limit], "offset": offset, "characters": len(item["text"])}
    raise KeyError(document_id)
