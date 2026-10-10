"""Calculate a CPU-only boot ceiling, then exec the local Ollama service.

This is the service launcher, not a benchmark or a model downloader.
Run after the two explicit model manifests have been created by the operator.
"""
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from shared_models.admission import GIB, cpu_capacity, memory

ROOT = Path(__file__).resolve().parents[1]
MODELS = Path("/var/lib/aichat/models")
CONTEXT = 4096
GENERATION = "private-qwen:latest"
EMBEDDING = "embeddinggemma:latest"


def manifest(name):
    model, tag = name.split(":")
    path = MODELS / "manifests" / "registry.ollama.ai" / "library" / model / tag
    raw = path.read_bytes()
    value = json.loads(raw)
    config = json.loads((MODELS / "blobs" / value["config"]["digest"].replace(":", "-")).read_text())
    weights = sum(layer["size"] for layer in value["layers"] if layer["mediaType"] == "application/vnd.ollama.image.model")
    if weights <= 0:
        raise ValueError("Downloaded model has no GGUF weight layer")
    return {"digest": hashlib.sha256(raw).hexdigest()}, weights, config


def main():
    generation, gen_bytes, config = manifest(GENERATION)
    if config.get("model_family") != "qwen3" or config.get("file_type") != "Q4_K_M":
        raise ValueError("Capacity estimate requires the documented Qwen3 Q4_K_M model")
    embedding, embed_bytes, config = manifest(EMBEDDING)
    if config.get("model_family") != "gemma3":
        raise ValueError("Capacity estimate requires the documented embeddinggemma model")
    total, available = memory()
    reserve = max(GIB, total // 5)
    # Both weight sets at 1.5x disk size, 512 MiB for the application/runtime,
    # 256 MiB embedding workspace. Generation reserves 128 KiB per context
    # token plus 64 MiB per slot. These are conservative allowances, not a
    # universal memory predictor or measured KV sizes.
    fixed = (gen_bytes + embed_bytes) * 3 // 2 + GIB // 2 + GIB // 4
    slot_bytes = CONTEXT * 128 * 1024 + GIB // 16
    ceiling = min(cpu_capacity(), (min(total, available) - reserve - fixed) // slot_bytes)
    if ceiling < 1:
        raise ValueError("Not enough RAM/cgroup budget for this deployment profile")
    generation.update(slots=ceiling, context_length=CONTEXT)
    embedding.update(slots=1, context_length=2048)
    profile = {"boot": uuid.uuid4().hex, "base_url": "http://127.0.0.1:11434/v1",
               "ceiling": ceiling, "reserve_bytes": reserve, "lease_bytes": GIB // 16,
               "context_limit": CONTEXT, "output_limit": 512,
               "models": {GENERATION: generation, EMBEDDING: embedding}}
    path = ROOT / "data" / "inference-capacity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".capacity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(profile, output)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    environment = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_MODELS=str(MODELS),
                       OLLAMA_CONTEXT_LENGTH=str(CONTEXT), OLLAMA_NUM_PARALLEL=str(ceiling),
                       OLLAMA_MAX_LOADED_MODELS="2", OLLAMA_MAX_QUEUE="0", OLLAMA_KEEP_ALIVE="-1",
                       OLLAMA_NO_CLOUD="1")
    binary = shutil.which("ollama")
    if binary is None:
        raise ValueError("Install Ollama before starting this service")
    os.execve(binary, [binary, "serve"], environment)


if __name__ == "__main__":
    main()
