#!/usr/bin/env python3
"""Fail before serving when Panella's selected local embedding path cannot work."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


MARKER = "PANELLA: refusing pure-Python hash-embedding fallback"
DEFAULT_MODEL = "all-MiniLM-L6-v2"


def note(message: str) -> None:
    print(f"embedding preflight: {message}", file=sys.stderr, flush=True)


def fail(message: str) -> None:
    note(f"ERROR: {message}")
    raise SystemExit(1)


def sqlite_vec_source() -> Path:
    spec = importlib.util.find_spec("mcp_memory_service.storage.sqlite_vec")
    if spec is None or spec.origin is None:
        fail("could not locate sqlite_vec.py for guard depth check")
    return Path(spec.origin)


def truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def encode_sentence_transformer(model: str) -> tuple[int, str]:
    from sentence_transformers import SentenceTransformer

    offline = (
        os.environ.get("HF_HUB_OFFLINE", "1") != "0"
        or os.environ.get("TRANSFORMERS_OFFLINE", "1") != "0"
    )
    if offline and model == DEFAULT_MODEL:
        # The baked artifact gate: the DEFAULT model must load from the exact snapshot this
        # image was built with (manifest-verified at build) — a path-level check on purpose.
        hf_home = Path(os.environ.get("HF_HOME", "/opt/hf-cache"))
        repo = f"models--sentence-transformers--{model.replace('/', '--')}"
        snapshots = hf_home / "hub" / repo / "snapshots"
        candidates = sorted(item for item in snapshots.glob("*") if item.is_dir()) if snapshots.is_dir() else []
        if not candidates:
            fail(
                "baked model missing — was the image built with BAKE_EMBEDDING_MODEL=0? "
                "See docs/SELF_HOST.md (bring-your-own-model contract)"
            )
        if len(candidates) != 1:
            fail(f"expected exactly one cached snapshot for {model}, found {len(candidates)}")
        # str(), not Path: sentence-transformers does `"/" in model_name_or_path`,
        # which TypeErrors on PosixPath.
        target = str(candidates[0])
        mode = "offline"
    elif offline:
        # Custom model, offline: do NOT guess cache paths (an org-prefixed model like
        # BAAI/bge-small-en-v1.5 lives at models--BAAI--…, not models--sentence-transformers--…).
        # Load by name and let huggingface_hub resolve its own cache under the ambient
        # HF_HUB_OFFLINE=1 — preflight then fails exactly iff serving would fail.
        target = model
        mode = "offline"
    else:
        target = model
        mode = "online"
    try:
        vector = SentenceTransformer(target, device="cpu").encode("Panella embedding preflight")
    except Exception as exc:
        if mode == "offline" and model == DEFAULT_MODEL:
            fail(f"baked model snapshot failed to load ({exc}); the image build is broken")
        if mode == "offline":
            fail(
                f"custom model {model!r} is not loadable from the offline cache ({exc}); "
                "enable explicit egress with HF_HOME=/home/panella/.cache/huggingface, "
                "HF_HUB_OFFLINE=0, TRANSFORMERS_OFFLINE=0"
            )
        fail(f"model {model!r} failed to load/download in online mode ({exc})")
    dimension = len(vector)
    if model == DEFAULT_MODEL and dimension != 384:
        fail(f"default model returned dimension {dimension}, expected 384")
    if model != DEFAULT_MODEL and dimension <= 0:
        fail(f"custom model returned invalid dimension {dimension}")
    return dimension, mode


def check_onnx() -> tuple[int, str]:
    # Exact coupling: call the same factory the serving process calls
    # (sqlite_vec.py routes ONNX init through get_onnx_embedding_model, which
    # returns None on missing deps and downloads/uses the same cache itself).
    try:
        from mcp_memory_service.embeddings import get_onnx_embedding_model
    except Exception as exc:
        fail(f"MCP_MEMORY_USE_ONNX set but the upstream ONNX module is unavailable ({exc})")
    model = get_onnx_embedding_model(DEFAULT_MODEL)
    if model is None:
        fail(
            "MCP_MEMORY_USE_ONNX set but upstream returned no ONNX model "
            "(onnxruntime/tokenizers missing, or the model cache at "
            "$HOME/.cache/mcp_memory/onnx_models/ is absent and not downloadable); "
            "populate the cache or unset MCP_MEMORY_USE_ONNX"
        )
    try:
        vector = model.encode(["x"])[0]
    except Exception as exc:
        fail(f"MCP_MEMORY_USE_ONNX encode failed ({exc})")
    if len(vector) <= 0:
        fail("MCP_MEMORY_USE_ONNX returned an empty vector")
    return len(vector), "onnx"


def main() -> None:
    source = sqlite_vec_source()
    if MARKER.encode() not in source.read_bytes():
        fail("guard absent — image build is broken")

    if os.environ.get("PANELLA_REQUIRE_REAL_EMBEDDINGS", "1") == "0":
        note("WARNING: HASH FALLBACK BREAKGLASS ENABLED; PANELLA_REQUIRE_REAL_EMBEDDINGS=0 permits degraded hash embeddings")
    if os.environ.get("PANELLA_SKIP_EMBEDDING_PREFLIGHT", "0") == "1":
        note("WARNING: EMBEDDING PREFLIGHT SKIPPED")
        note("WARNING: model checks are skipped; guard remains active and failures now surface at serving start")
        return

    backend = os.environ.get("MCP_MEMORY_STORAGE_BACKEND", "sqlite_vec")
    external = os.environ.get("MCP_EXTERNAL_EMBEDDING_URL")
    if backend == "cloudflare":
        note("backend=cloudflare: embeddings use Workers AI; no local model check is needed")
        return
    if backend == "sqlite_vec" and external:
        note("backend=sqlite_vec with MCP_EXTERNAL_EMBEDDING_URL: delegating to upstream external fail-loud (#551)")
        return
    if backend not in {"sqlite_vec", "hybrid"}:
        note(f"backend={backend}: unknown backend; conservatively running local embedding checks")

    if truthy(os.environ.get("MCP_MEMORY_USE_ONNX", "")):
        dimension, mode = check_onnx()
        note(f"success backend={backend} mode={mode} model={DEFAULT_MODEL} dim={dimension}")
        return
    model = os.environ.get("MCP_EMBEDDING_MODEL", DEFAULT_MODEL)
    dimension, mode = encode_sentence_transformer(model)
    note(f"success backend={backend} mode={mode} model={model} dim={dimension}")


if __name__ == "__main__":
    main()
