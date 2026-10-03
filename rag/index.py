"""Atomic SQLite index and embedding cache, bounded inspection and exact cosine."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from .chunks import SIZE, OVERLAP, chunk_documents
from .documents import digest, load_corpus, now, write_json
from .embeddings import EmbeddingConfig, Embeddings, normalize

VERSION = 1


def storage_root() -> Path:
    # Intentionally never import app.config / dotenv.
    return Path(os.environ.get("RAG_DIR", "data/rag")).resolve()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default



class Operation:
    """One writer, recoverable OS lock, durable status shared with UI."""
    def __init__(self, root, kind):
        self.root, self.kind = Path(root), kind

    def __enter__(self):
        import fcntl
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / "operation.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise ValueError("Another RAG operation is running") from None
        self.started = time.monotonic()
        self.value = {"operation_id": uuid.uuid4().hex, "kind": self.kind, "pid": os.getpid(),
                      "state": "running", "stage": "documents", "started_at": now(), "updated_at": now(),
                      "duration_seconds": 0, "documents": 0, "chunks": 0, "computed": 0, "cached": 0}
        self.update()
        return self

    def update(self, **values):
        self.value.update(values)
        self.value.update(updated_at=now(), duration_seconds=round(time.monotonic() - self.started, 3))
        write_json(self.root / "progress.json", self.value)

    def __exit__(self, exc_type, error, traceback):
        try:
            if error:
                self.update(state="error", error=str(error))
            elif self.value["state"] == "running":
                self.update(state="ready")
        finally:
            self.lock.close()


def build_index(root: Path, config=None, strategy="structural", batch_size=16, *, client=None, operation=None):
    if not 1 <= batch_size <= 256:
        raise ValueError("Batch size must be between 1 and 256")
    if operation is None:
        with Operation(root, "index") as progress:
            return build_index(root, config, strategy, batch_size, client=client, operation=progress)
    root = Path(root)
    config = config or EmbeddingConfig()
    corpus = load_corpus(root)
    documents = corpus["documents"]
    operation.update(stage="documents", documents=len(documents), words=sum(d["words"] for d in documents),
                     corpus_fingerprint=corpus["fingerprint"], config=asdict(config), strategy=strategy)
    chunks = chunk_documents(documents, strategy)
    operation.update(stage="chunks", chunks=len(chunks))
    cache = sqlite3.connect(root / "embeddings-cache.sqlite")
    cache.execute("CREATE TABLE IF NOT EXISTS embeddings (fingerprint TEXT, hash TEXT, vector TEXT, PRIMARY KEY(fingerprint,hash))")
    fingerprint = config.fingerprint()
    vectors, missing, dimension = {}, {}, config.dimensions
    fd, temporary = tempfile.mkstemp(prefix="index-", suffix=".sqlite", dir=root)
    os.close(fd)
    try:
        cached = computed = 0
        for chunk in chunks:
            key = chunk["content_hash"]
            row = cache.execute("SELECT vector FROM embeddings WHERE fingerprint=? AND hash=?", (fingerprint, key)).fetchone()
            if row:
                vector = normalize(json.loads(row[0]), dimension)
                dimension = len(vector)
                vectors[key] = vector
                cached += 1
            elif key not in missing:
                missing[key] = chunk["text"]
        operation.update(stage="embeddings", cached=cached, dimension=dimension, pending=len(missing))
        items = list(missing.items())
        embedder = Embeddings(config, client)
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            batch_vectors = embedder.embed([text for _, text in batch])
            for (key, _), vector in zip(batch, batch_vectors):
                vector = normalize(vector, dimension)
                dimension = len(vector)
                vectors[key] = vector
                cache.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)", (fingerprint, key, json.dumps(vector)))
                computed += 1
            cache.commit()
            operation.update(computed=computed, dimension=dimension, pending=len(items) - computed)
        metadata = {"version": VERSION, "index_id": uuid.uuid4().hex, "operation_id": operation.value["operation_id"], "built_at": now(), "strategy": strategy,
                    "chunk_size": SIZE, "overlap": OVERLAP, "corpus_fingerprint": corpus["fingerprint"],
                    "embedding_fingerprint": fingerprint, "embedding_config": asdict(config), "dimension": dimension,
                    "documents": len(documents), "chunks": len(chunks), "words": sum(d["words"] for d in documents),
                    "computed": computed, "cached": cached}
        operation.update(stage="save")
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
                             "CREATE TABLE documents (id TEXT PRIMARY KEY, source TEXT, title TEXT, content_hash TEXT, fetched_at TEXT, words INTEGER, text TEXT, blocks TEXT);"
                             "CREATE TABLE chunks (id TEXT PRIMARY KEY, document_id TEXT, source TEXT, title TEXT, section TEXT, start INTEGER, end INTEGER, content_hash TEXT, strategy TEXT, text TEXT, vector TEXT);"
                             "CREATE INDEX chunk_document ON chunks(document_id,start);")
            db.executemany("INSERT INTO metadata VALUES (?,?)", [(key, json.dumps(value)) for key, value in metadata.items()])
            db.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)", [(d["document_id"], d["source"], d["title"], d["content_hash"], d["fetched_at"], d["words"], d["text"], json.dumps(d["blocks"], ensure_ascii=False)) for d in documents])
            db.executemany("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(c["chunk_id"], c["document_id"], c["source"], c["title"], c["section"], c["start"], c["end"], c["content_hash"], c["strategy"], c["text"], json.dumps(vectors[c["content_hash"]])) for c in chunks])
            db.commit()
        with open(temporary, "rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, root / "index.sqlite")
        # replace is the success boundary. Telemetry cannot undo publication.
        operation.value.update(state="ready", index_id=metadata["index_id"])
        try:
            operation.update(state="ready", index_id=metadata["index_id"])
        except OSError:
            # The committed operation_id lets readers reconcile stale progress.
            metadata["warning"] = "Index published; final progress could not be saved"
        return metadata
    finally:
        cache.close()
        Path(temporary).unlink(missing_ok=True)


class Index:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else storage_root()

    def connect(self):
        path = self.root / "index.sqlite"
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        return db

    def metadata(self, db=None):
        if db is None:
            with closing(self.connect()) as connection:
                return self.metadata(connection)
        result = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT * FROM metadata")}
        if result.get("version") != VERSION:
            raise ValueError("Unsupported index version")
        required = {"index_id", "corpus_fingerprint", "embedding_fingerprint", "embedding_config", "dimension", "documents", "chunks", "words", "strategy"}
        if not required <= result.keys() or any(type(result[k]) is not int or result[k] <= 0 for k in ("dimension", "documents", "chunks", "words")):
            raise ValueError("Incomplete index metadata")
        return result

    def status(self):
        result = {"state": "missing", "index": None, "operation": None, "ingestion": None}
        try:
            operation = read_json(self.root / "progress.json")
            if operation and operation["state"] == "running":
                # Lock, not heartbeat age: a slow legitimate HTTP batch is still running.
                import fcntl
                try:
                    with (self.root / "operation.lock").open("r") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    operation = {**operation, "state": "interrupted"}
                except FileNotFoundError:
                    operation = {**operation, "state": "interrupted"}
                except BlockingIOError:
                    pass
            result["operation"] = operation
            result["ingestion"] = read_json(self.root / "ingest-report.json")
            if (self.root / "index.sqlite").exists():
                with closing(self.connect()) as db:
                    metadata = self.metadata(db)
                    metadata["size_bytes"] = db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]
                    metadata["rows"] = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("documents", "chunks")}
                if any(metadata["rows"][table] != metadata[table] for table in ("documents", "chunks")):
                    raise ValueError("Index row counts differ from committed metadata")
                if operation and operation.get("operation_id") == metadata.get("operation_id") and operation["state"] != "ready":
                    operation = {**operation, "state": "ready", "stage": "save", "index_id": metadata["index_id"],
                                 "warning": "Индекс опубликован; завершающее состояние операции не сохранено"}
                    result["operation"] = operation
                metadata["state"] = "ready"
                result.update(state="ready", index=metadata)
                corpus = read_json(self.root / "corpus.json")
                if corpus and corpus.get("fingerprint") != metadata["corpus_fingerprint"]:
                    result["state"] = metadata["state"] = "stale"
            if operation and operation["state"] in {"running", "error", "interrupted"}:
                # Prior index remains separately visible/usable; latest operation is explicit.
                result["state"] = operation["state"]
        except (OSError, ValueError, sqlite3.Error, KeyError) as error:
            result.update(state="error", error=str(error))
        return result

    def documents(self, offset=0, limit=25):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT id AS document_id,source,title,content_hash,fetched_at,words,length(text) AS characters FROM documents ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset))]

    def chunks(self, document_id, offset=0, limit=25):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT id AS chunk_id,document_id,source,title,section,start,end,content_hash,strategy,length(text) AS characters FROM chunks WHERE document_id=? ORDER BY start LIMIT ? OFFSET ?", (document_id, limit, offset))]

    def chunk(self, chunk_id, vector=False):
        with closing(self.connect()) as db:
            columns = "*" if vector else "id,document_id,source,title,section,start,end,content_hash,strategy,text"
            row = db.execute(f"SELECT {columns} FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if not row:
                raise KeyError(chunk_id)
            result = dict(row)
            result["chunk_id"] = result.pop("id")
            if vector:
                result["vector"] = json.loads(result["vector"])
            return result

    def search(self, query, top_k=5, *, config=None, client=None):
        """One read connection pins a complete index across concurrent rebuilds."""
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        with closing(self.connect()) as db:
            metadata = self.metadata(db)
            config = config or EmbeddingConfig(**metadata["embedding_config"])
            if config.fingerprint() != metadata["embedding_fingerprint"]:
                raise ValueError("Query/index embedding configuration mismatch")
            corpus = read_json(self.root / "corpus.json")
            if corpus and corpus["fingerprint"] != metadata["corpus_fingerprint"]:
                raise ValueError("Index is stale; rebuild before retrieval")
            vector = normalize(Embeddings(config, client).embed([query])[0], metadata["dimension"])
            hits = []
            for row in db.execute("SELECT * FROM chunks"):
                stored = normalize(json.loads(row["vector"]), metadata["dimension"])
                hit = dict(row)
                hit.pop("vector")
                hit["chunk_id"] = hit.pop("id")
                hit["score"] = sum(a * b for a, b in zip(vector, stored))
                hits.append(hit)
            return sorted(hits, key=lambda h: (-h["score"], h["chunk_id"]))[:top_k]
