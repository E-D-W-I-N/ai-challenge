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
from .artifacts import available, revive
from .semantic import SemanticConfig, semantic_chunks
from .documents import digest, load_corpus, now, write_json
from .embeddings import EmbeddingConfig, Embeddings, normalize

VERSION = 1


def storage_root() -> Path:
    # Intentionally never import app.config / dotenv.
    return Path(os.environ.get("RAG_DIR", "data/rag")).resolve()


def read_json(path, default=None):
    if not available(path.parent, path.name):
        return default
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
                      "state": "running", "stage": {"chunks": "chunks", "embeddings": "embeddings", "save": "save"}.get(self.kind, "documents"), "started_at": now(), "updated_at": now(),
                      "duration_seconds": 0, "documents": 0, "chunks": 0, "computed": 0, "cached": 0}
        try:
            self.update()
        except BaseException:
            self.lock.close()
            raise
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


def stage_chunks(root, strategy="fixed", size=SIZE, overlap=OVERLAP, *, operation=None, semantic_config=None, client=None):
    if operation is None:
        with Operation(root, "chunks") as progress:
            return stage_chunks(root, strategy, size, overlap, operation=progress, semantic_config=semantic_config, client=client)
    root = Path(root)
    corpus = load_corpus(root)
    operation.update(stage="chunks", documents=len(corpus["documents"]), strategy=strategy, size=size, overlap=overlap)
    semantic_config = semantic_config or SemanticConfig()
    report = None
    if strategy == "semantic":
        # A tombstoned cache may survive interrupted physical cleanup.
        if not available(root, "semantic-cache"):
            import shutil
            shutil.rmtree(root / "semantic-cache", ignore_errors=True)
            revive(root, "semantic-cache")
        chunks, report = semantic_chunks(corpus["documents"], semantic_config, size, overlap, root=root, client=client, operation=operation)
    else:
        chunks = chunk_documents(corpus["documents"], strategy, size, overlap)
    value = {"version": 1, "corpus_fingerprint": corpus["fingerprint"], "strategy": strategy,
             "size": size, "overlap": overlap, "chunks": chunks}
    if strategy == "semantic":
        value["semantic_config"] = asdict(semantic_config)
    value["fingerprint"] = digest(json.dumps(value, sort_keys=True, separators=(",", ":")))
    if report is not None:
        value["report"] = report
    write_json(root / "chunks.json", value)
    revive(root, "chunks.json")
    operation.update(chunks=len(chunks), state="complete", **({"semantic_report": report} if report is not None else {}))
    return {key: item for key, item in value.items() if key != "chunks"} | {"chunks": len(chunks)}


def load_chunks(root):
    corpus = load_corpus(root)
    value = read_json(Path(root) / "chunks.json")
    if not value or value["corpus_fingerprint"] != corpus["fingerprint"]:
        raise ValueError("Create chunks for the current corpus first")
    expected = digest(json.dumps({k: v for k, v in value.items() if k not in {"fingerprint", "report"}}, sort_keys=True, separators=(",", ":")))
    if value["fingerprint"] != expected or not value["chunks"]:
        raise ValueError("Invalid staged chunks")
    return corpus, value


def stage_embeddings(root, config=None, batch_size=16, *, client=None, operation=None):
    if not 1 <= batch_size <= 256:
        raise ValueError("Batch size must be between 1 and 256")
    if operation is None:
        with Operation(root, "embeddings") as progress:
            return stage_embeddings(root, config, batch_size, client=client, operation=progress)
    root, config = Path(root), config or EmbeddingConfig()
    corpus, staged = load_chunks(root)
    chunks = staged["chunks"]
    operation.update(stage="embeddings", documents=len(corpus["documents"]), chunks=len(chunks), config=asdict(config))
    vectors, missing, dimension = {}, {}, config.dimensions
    fingerprint = config.fingerprint()
    if not available(root, "embeddings-cache.sqlite"):
        (root / "embeddings-cache.sqlite").unlink(missing_ok=True)
        revive(root, "embeddings-cache.sqlite")
    with closing(sqlite3.connect(root / "embeddings-cache.sqlite")) as cache:
        cache.execute("CREATE TABLE IF NOT EXISTS embeddings (fingerprint TEXT, hash TEXT, vector TEXT, PRIMARY KEY(fingerprint,hash))")
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
        operation.update(cached=cached, dimension=dimension, pending=len(missing))
        items = list(missing.items())
        embedder = Embeddings(config, client)
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            for (key, _), vector in zip(batch, embedder.embed([text for _, text in batch])):
                vector = normalize(vector, dimension)
                dimension = len(vector)
                vectors[key] = vector
                cache.execute("INSERT OR REPLACE INTO embeddings VALUES (?,?,?)", (fingerprint, key, json.dumps(vector)))
                computed += 1
            cache.commit()
            operation.update(computed=computed, dimension=dimension, pending=len(items) - computed)
    value = {"version": 1, "chunks_fingerprint": staged["fingerprint"], "embedding_config": asdict(config),
             "embedding_fingerprint": fingerprint, "dimension": dimension, "vectors": vectors,
             "computed": computed, "cached": cached}
    write_json(root / "vectors.json", value)
    revive(root, "vectors.json")
    operation.update(state="complete")
    return {key: item for key, item in value.items() if key != "vectors"}


def load_vectors(root, staged):
    value = read_json(Path(root) / "vectors.json")
    if not value or value["chunks_fingerprint"] != staged["fingerprint"]:
        raise ValueError("Create embeddings for the current chunks first")
    config = EmbeddingConfig(**value["embedding_config"])
    if config.fingerprint() != value["embedding_fingerprint"]:
        raise ValueError("Invalid staged embedding configuration")
    value["vectors"] = {c["content_hash"]: normalize(value["vectors"][c["content_hash"]], value["dimension"]) for c in staged["chunks"]}
    return value


def save_index(root, *, operation=None):
    if operation is None:
        with Operation(root, "save") as progress:
            return save_index(root, operation=progress)
    root = Path(root)
    corpus, staged = load_chunks(root)
    embedded = load_vectors(root, staged)
    documents, chunks = corpus["documents"], staged["chunks"]
    vectors, dimension = embedded["vectors"], embedded["dimension"]
    config = EmbeddingConfig(**embedded["embedding_config"])
    fd, temporary = tempfile.mkstemp(prefix="index-", suffix=".sqlite", dir=root)
    os.close(fd)
    try:
        metadata = {"version": VERSION, "index_id": uuid.uuid4().hex, "operation_id": operation.value["operation_id"], "built_at": now(), "strategy": staged["strategy"],
                    "chunk_size": staged["size"], "overlap": staged["overlap"], "corpus_fingerprint": corpus["fingerprint"],
                    "embedding_fingerprint": embedded["embedding_fingerprint"], "embedding_config": asdict(config), "dimension": dimension,
                    "documents": len(documents), "chunks": len(chunks), "words": sum(d["words"] for d in documents),
                    "computed": embedded["computed"], "cached": embedded["cached"]}
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
        revive(root, "index.sqlite")
        # replace is the success boundary. Telemetry cannot undo publication.
        operation.value.update(state="ready", index_id=metadata["index_id"])
        try:
            operation.update(state="ready", index_id=metadata["index_id"])
        except OSError:
            # The committed operation_id lets readers reconcile stale progress.
            metadata["warning"] = "Index published; final progress could not be saved"
        return metadata
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_index(root: Path, config=None, strategy="structural", batch_size=16, *, client=None, operation=None, size=SIZE, overlap=OVERLAP, semantic_config=None):
    if operation is None:
        with Operation(root, "index") as progress:
            return build_index(root, config, strategy, batch_size, client=client, operation=progress, size=size, overlap=overlap, semantic_config=semantic_config)
    stage_chunks(root, strategy, size, overlap, operation=operation, semantic_config=semantic_config, client=client)
    operation.update(state="running")
    stage_embeddings(root, config, batch_size, client=client, operation=operation)
    operation.update(state="running")
    return save_index(root, operation=operation)


class Index:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else storage_root()

    def connect(self):
        if not available(self.root, "index.sqlite"):
            raise ValueError("Index was deleted; save a new index")
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
            if available(self.root, "index.sqlite") and (self.root / "index.sqlite").exists():
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
