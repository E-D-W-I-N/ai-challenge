"""Atomic deletion visibility: interrupted cleanup never resurrects old artifacts."""
import json
import shutil
from pathlib import Path

from .documents import write_json


def deleted(root):
    try:
        return set(json.loads((Path(root) / "tombstone.json").read_text(encoding="utf-8"))["files"])
    except FileNotFoundError:
        return set()


def available(root, name):
    return name not in deleted(root)


def revive(root, name):
    names = deleted(root)
    if name in names:
        names.remove(name)
        write_json(Path(root) / "tombstone.json", {"files": sorted(names)})


def clear(root, kind, operation):
    root = Path(root)
    names = {"vectors.json", "index.sqlite", "embeddings-cache.sqlite", "comparison.json", "comparisons"}
    if kind == "index":
        names = {"index.sqlite", "comparison.json", "comparisons"}
    elif kind == "chunks":
        names |= {"chunks.json", "semantic-cache"}
    elif kind != "embeddings":
        raise ValueError("Unknown clear stage")
    # Commit the visibility boundary before physical cleanup. Markers survive restart.
    write_json(root / "tombstone.json", {"files": sorted(deleted(root) | names)})
    for name in sorted(names):
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    operation.update(stage=kind, state="complete", cleared=kind)
    return {"cleared": kind}
