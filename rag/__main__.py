"""Operator CLI. Inputs, snapshots and reports stay outside version control."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3

import httpx
import sys
from pathlib import Path

from .chunks import STRATEGIES, chunk_documents
from .artifacts import clear
from .documents import ingest, load_corpus, write_json
from .embeddings import EmbeddingConfig
from .semantic import SemanticConfig
from .preparation import PreparationConfig
from .index import Index, Operation, build_index, stage_chunks, stage_embeddings, save_index, storage_root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=storage_root(), help="Snapshot directory (default data/rag)")
    from shared_models import DEFAULT_COMPATIBLE_BASE_URL, endpoint, validate_url
    parser.add_argument("--compatible-base-url", default=DEFAULT_COMPATIBLE_BASE_URL)
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("ingest", help="Explicit URLs/local HTML, never crawl")
    load.add_argument("--url", action="append", default=[])
    load.add_argument("--manifest", type=Path, help='JSON list: {"url":...} or {"path":...,"source":...}; relative paths resolve by manifest')
    load.add_argument("--preparation-strategy", choices=("programmatic", "llm"), default="programmatic")
    load.add_argument("--preparation-reasoning", action="store_true")
    load.add_argument("--preparation-model", default=PreparationConfig.model)
    load.add_argument("--preparation-provider", choices=("openrouter", "compatible"), default="openrouter")
    load.add_argument("--preparation-timeout", type=float, default=PreparationConfig.timeout_seconds)
    split = commands.add_parser("chunks")
    split.add_argument("--strategy", choices=(*STRATEGIES, "semantic"), default="fixed")
    split.add_argument("--size", type=int, default=1200)
    split.add_argument("--overlap", type=int, default=180)
    split.add_argument("--semantic-reasoning", action="store_true")
    split.add_argument("--semantic-model", default=SemanticConfig.model)
    split.add_argument("--semantic-provider", choices=("openrouter", "compatible"), default="openrouter")
    commands.add_parser("save")
    clear_command = commands.add_parser("clear")
    clear_command.add_argument("stage", choices=("chunks", "embeddings", "index"))
    for name in ("index", "compare", "embed"):
        command = commands.add_parser(name)
        command.add_argument("--provider", choices=("openrouter", "compatible"), default="compatible")
        command.add_argument("--reasoning", action="store_true", help="Remember this embedding preference; standard embeddings has no reasoning parameter")
        command.add_argument("--model", default=EmbeddingConfig.model)
        command.add_argument("--dimensions", type=int)
        command.add_argument("--revision", default="1", help="Change when replacing weights under same model ID")
        command.add_argument("--batch-size", type=int, default=16)
        if name == "index":
            command.add_argument("--strategy", choices=(*STRATEGIES, "semantic"), default="structural")
            command.add_argument("--size", type=int, default=1200)
            command.add_argument("--overlap", type=int, default=180)
            command.add_argument("--semantic-reasoning", action="store_true")
            command.add_argument("--semantic-model", default=SemanticConfig.model)
            command.add_argument("--semantic-provider", choices=("openrouter", "compatible"), default="openrouter")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        validate_url(args.compatible_base_url)
        if args.command == "ingest":
            inputs = [{"url": url} for url in args.url]
            if args.manifest:
                entries = json.loads(args.manifest.read_text(encoding="utf-8"))
                if not isinstance(entries, list) or any(not isinstance(e, dict) or not ({"url", "path"} & e.keys())
                        or any(not isinstance(e[k], str) or not e[k].strip() for k in ("url", "path", "source") if k in e) for e in entries):
                    raise ValueError("Manifest must be a list of URL/path objects")
                for entry in entries:
                    if "path" in entry:
                        entry = {**entry, "path": str((args.manifest.parent / entry["path"]).resolve())}
                    inputs.append(entry)
            if not inputs:
                raise ValueError("Provide --url or --manifest")
            with Operation(root, "ingest") as operation:
                result = ingest(inputs, root, operation=operation, preparation_strategy=args.preparation_strategy, preparation_config=PreparationConfig(endpoint(args.preparation_provider, args.compatible_base_url), args.preparation_model, args.preparation_timeout, provider=args.preparation_provider, reasoning_enabled=args.preparation_reasoning))
                operation.update(documents=result["documents"], words=result["words"], state="complete")
        elif args.command == "clear":
            with Operation(root, "delete_" + args.stage) as operation:
                result = clear(root, args.stage, operation)
        elif args.command == "chunks":
            result = stage_chunks(root, args.strategy, args.size, args.overlap, semantic_config=SemanticConfig(endpoint(args.semantic_provider, args.compatible_base_url), args.semantic_model, provider=args.semantic_provider, reasoning_enabled=args.semantic_reasoning))
        elif args.command == "save":
            result = save_index(root)
        elif args.command == "status":
            result = Index(root).status()
        else:
            config = EmbeddingConfig(endpoint(args.provider, args.compatible_base_url), args.model, args.dimensions, args.revision, provider=args.provider, reasoning_enabled=args.reasoning)
            if args.command == "index":
                result = build_index(root, config, args.strategy, args.batch_size, size=args.size, overlap=args.overlap, semantic_config=SemanticConfig(endpoint(args.semantic_provider, args.compatible_base_url), args.semantic_model, provider=args.semantic_provider, reasoning_enabled=args.semantic_reasoning))
            elif args.command == "embed":
                result = stage_embeddings(root, config, args.batch_size)
            else:
                # Separate artifacts: comparing never replaces the active index.
                with Operation(root, "compare") as operation:
                    corpus = load_corpus(root)
                    result = {"corpus_fingerprint": corpus["fingerprint"], "embedding_config": config.__dict__, "strategies": {}}
                    for strategy in STRATEGIES:
                        destination = root / "comparisons" / strategy
                        destination.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(root / "corpus.json", destination / "corpus.json")
                        metadata = build_index(destination, config, strategy, args.batch_size)
                        chunks = chunk_documents(corpus["documents"], strategy)
                        lengths = [len(c["text"]) for c in chunks]
                        overlap = sum(max(0, left["end"] - right["start"]) for left, right in zip(chunks, chunks[1:])
                                      if left["document_id"] == right["document_id"])
                        result["strategies"][strategy] = {**metadata, "min_characters": min(lengths), "max_characters": max(lengths),
                            "mean_characters": sum(lengths) / len(lengths), "stored_characters": sum(lengths),
                            "sectionless_chunks": sum(not c["section"] for c in chunks),
                            "duplicated_characters": overlap}
                    write_json(root / "comparison.json", result)
                    operation.update(state="complete", stage="save")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, httpx.HTTPError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
