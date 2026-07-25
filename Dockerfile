# Self-host packaging for the governed Panella store memory product (Slice-S P3a).
#
# Two independent targets, composed by docker-compose.yml:
#   store — near-stock upstream mcp-memory-service, PINNED to the version whose HTTP contract
#           the facade adapter is proven against (tests/fixtures/panella_openapi_v10.67.1.json;
#           the [sqlite] extra only adds onnxruntime; the store runs SentenceTransformer/torch
#           locally with a model baked at build time, and needs no provider API key either way).
#   app   — this repo's memory HTTP facade (panella.http.app:create_app) plus the
#           package-rendered per-distribution config artifact (/app/dist-config).
#
# `app` is last so a bare `docker build .` produces the facade image.

# --------------------------------------------------------------------------------------------
FROM python:3.14-slim AS store

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Proven-live env contract (owner's memory-service.service runs this exact version):
    MCP_MEMORY_STORAGE_BACKEND=sqlite_vec \
    MCP_MEMORY_BASE_DIR=/data \
    MCP_MEMORY_SQLITE_PATH=/data/sqlite_vec.db \
    MCP_MEMORY_BACKUPS_PATH=/data/backups \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000 \
    MCP_MDNS_ENABLED=false \
    MCP_OAUTH_ENABLED=false

# The adapter contract pin — upgrade ONLY together with a re-pinned OpenAPI fixture.
# 10.67.1: patches CVE-2026-50027 (CVSS 9.8 — /api/documents/* served with NO auth even when
# MCP_API_KEY is set: unauthenticated read/write/delete) and CVE-2026-49291 (HIGH — OAuth
# read-only clients could write/delete via MCP). Minimal version fixing both (fixed 10.67.1 /
# 10.65.3 respectively); the facade never calls /api/documents, but the store must not ship the
# vulnerable version and the release trivy gate correctly blocks it. Contract re-verified against
# the 10.67.1 OpenAPI fixture + boot-check (the /api/memories + /api/search + memories-table
# schema the adapter depends on are unchanged from 10.31.2).
# cryptography<47: the >=47.0.0 aarch64 wheels die with SIGILL (illegal instruction) inside
# the Rust extension under Apple-Silicon Docker (Virtualization.framework VM); 46.0.7 is proven
# good there (bisected 45.0.5 OK / 46.0.7 OK / 47.0.0+48.0.2+49.0.0 SIGILL), and upstream
# requires >=46.0.6. Image-layer mitigation only — drop once upstream wheels stop faulting.
# ---- CPU-only ML stack (C0-3b image slimming) ----
# pip's default resolution of mcp-memory-service pulls the CUDA torch wheel on BOTH amd64 and aarch64
# (torch 2.13 ships CUDA wheels for both) — ~2.9GB nvidia + ~0.65GB triton of dead weight the CPU-only
# deploy targets never use. Pre-install the +cpu torch build from the dedicated PyTorch CPU index —
# a single trusted index for that one command (torch AND its own dependency closure resolve only from
# there; no --extra-index-url, so no dependency-confusion surface). Then install mcp-memory-service
# from PyPI with store-constraints.txt pinning torch to that +cpu build so the CUDA closure is never
# resolved. This whole block MUST stay ahead of the guard_patch RUN below (guard_patch is the last
# step allowed to touch site-packages).
COPY docker/store/store-constraints.txt /tmp/store-constraints.txt
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.13.0+cpu
RUN pip install "mcp-memory-service[sqlite]==10.67.1" "cryptography<47" -c /tmp/store-constraints.txt
# Patch the packaging-tooling CVEs Trivy blocks on (metadata-only, not in the serving path):
# setuptools CVE-2025-47273, wheel CVE-2026-24049, jaraco.context CVE-2026-23949. Minimum-version
# bumps only; none touch the torch/CUDA closure asserted below. (transformers + cryptography CVEs
# are constrained — see .trivyignore.yaml.)
RUN pip install -U 'setuptools>=78.1.1' 'wheel>=0.46.2' 'jaraco.context>=6.1.0'
# Authoritative build-time gate (fail-fast, protects every build incl. local): the shipped torch MUST
# be the CPU build with no CUDA linkage and no CUDA/nvidia/triton packages, or the image build fails
# here — before the model bake / guard / user setup. pip check catches any constraints conflict.
RUN pip check \
 && python -c "import torch; assert torch.__version__ == '2.13.0+cpu', torch.__version__; assert torch.version.cuda is None, torch.version.cuda; print('C0-3b torch assert OK:', torch.__version__)" \
 && if pip list --format=freeze | grep -iqE '(^nvidia|^cuda|triton)'; then echo 'C0-3b FAIL: CUDA/nvidia/triton residue in image'; pip list --format=freeze | grep -iE '(^nvidia|^cuda|triton)'; exit 1; fi \
 && echo 'C0-3b no-CUDA-residue assert OK'

# ---- baked embedding model + fail-loud guards (C0-3a) ----
ARG BAKE_EMBEDDING_MODEL=1
ENV HF_HOME=/opt/hf-cache \
    PANELLA_REQUIRE_REAL_EMBEDDINGS=1
COPY docker/store/embedding_preflight.py docker/store/verify_model_manifest.py \
     docker/store/guard_patch.py docker/store/model-manifest.sha256 /usr/local/share/panella/
COPY --chmod=0755 docker/store/store-entrypoint.sh /usr/local/bin/store-entrypoint.sh
# The guard bakes a fail-loud refusal of upstream's silent hash-embedding fallback into the
# installed source (version -> anchor -> fresh-child verified); this is the LAST step allowed to
# touch site-packages. Remove once upstream ships a native fail-fast env (tracked issue).
RUN python /usr/local/share/panella/guard_patch.py --apply --verify
RUN mkdir -p /opt/hf-cache && chgrp -R 0 /opt/hf-cache && chmod -R g=rX /opt/hf-cache \
    && chmod -R a+rX /usr/local/share/panella
# BAKE=0 is expert "bring your own model" mode (contract in docs/SELF_HOST.md), never a quiet
# size toggle; the baked model lives outside /home/panella/.cache because the named volume masks
# that path on every upgraded box and would silently un-pin the model.
RUN if [ "$BAKE_EMBEDDING_MODEL" = "1" ]; then \
      python -c "from huggingface_hub import snapshot_download; snapshot_download( \
        repo_id='sentence-transformers/all-MiniLM-L6-v2', \
        revision='1110a243fdf4706b3f48f1d95db1a4f5529b4d41', \
        allow_patterns=['modules.json','sentence_bert_config.json','config_sentence_transformers.json', \
                        'config.json','model.safetensors','tokenizer.json','tokenizer_config.json', \
                        'vocab.txt','special_tokens_map.json','1_Pooling/config.json'])" \
      && python /usr/local/share/panella/verify_model_manifest.py /opt/hf-cache \
           /usr/local/share/panella/model-manifest.sha256 \
           --repo sentence-transformers/all-MiniLM-L6-v2 \
           --revision 1110a243fdf4706b3f48f1d95db1a4f5529b4d41 \
      && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -c "import glob; \
           from sentence_transformers import SentenceTransformer; \
           p = glob.glob('/opt/hf-cache/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/*')[0]; \
           v = SentenceTransformer(p, device='cpu').encode('bake smoke test'); \
           assert len(v) == 384, len(v)" \
      && chgrp -R 0 /opt/hf-cache && chmod -R g=rX /opt/hf-cache; \
    fi
# Runtime default = zero egress + zero telemetry from process start; custom-model egress is an
# explicit three-env override (docs/SELF_HOST.md).
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1

# /home/panella/.cache must exist owned by panella BEFORE the named volume mounts over it —
# a mountpoint Docker has to create itself is root-owned and the uid-10001 store could
# never persist its optional MCP_MEMORY_USE_ONNX cache, miscellaneous caches, or pre-bake
# leftovers there; the HF model itself is baked into the image at /opt/hf-cache.
RUN useradd --create-home --uid 10001 panella \
    && mkdir -p /data /home/panella/.cache \
    && chown -R panella:panella /data /home/panella/.cache \
    && chgrp -R 0 /data /home/panella/.cache \
    && chmod -R g=rwX /data /home/panella/.cache
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --retries=12 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["store-entrypoint.sh"]
CMD ["memory", "server", "--http"]

# Deterministic hash-fallback injection for CI cases B/C; never pushed or signed. This is the
# sole exception to the "guard_patch is the last site-packages mutation" rule.
FROM store AS store-hash-fallback-test
USER 0
RUN pip uninstall -y sentence-transformers
USER 10001

# --------------------------------------------------------------------------------------------
FROM python:3.14-slim AS app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Container default: listen on the bridge network; compose publishes 127.0.0.1 only.
    PANELLA_HTTP_HOST=0.0.0.0 \
    # Slice-S P3b — resolve agent profiles + wings from the package-rendered dist-config (below),
    # and serve routine /v1/memory/* through the generic serving profile (closes the P3a 403 gap).
    PANELLA_CONFIG_DIR=/app/dist-config \
    PANELLA_HTTP_PROFILE=serving \
    # Network MCP surface on /mcp: read-only by default (mcp-read). To enable write/approval, the
    # box owner flips PANELLA_MCP_PROFILE=mcp-write, provisions the 0600 approval token, and configures
    # approvers in the overlay (docs/SELF_HOST.md). /mcp is loopback-only unless PANELLA_MCP_ALLOWED_HOSTS is set.
    PANELLA_MCP_ENABLED=1 \
    PANELLA_MCP_PROFILE=mcp-read

WORKDIR /app
COPY . /app

# Editable install keeps the runtime repo-anchored (governance base config, agent profiles and
# store-probe defaults resolve relative to /app exactly like a checkout / owner's unit).
# cryptography<47: same Apple-Silicon SIGILL mitigation as the store stage (transitive dep here;
# the facade boot path never touches it, but a lazy import must not be a landmine).
RUN pip install -e . "cryptography<47"
# Same packaging-tooling CVE patch as the store stage (setuptools CVE-2025-47273,
# wheel CVE-2026-24049, jaraco.context CVE-2026-23949) — metadata-only, minimum bumps.
RUN pip install -U 'setuptools>=78.1.1' 'wheel>=0.46.2' 'jaraco.context>=6.1.0'

# Package-artifact config render (plan v7 §1.6 part 2): the ONLY place the finalizer/wings
# de-Owner happens. Renders from the shipped generic governance (docker build passes no env,
# so no overlay can leak in) and fails the image build loudly if governance is incoherent. This
# bakes a valid GENERIC default; the entrypoint RE-renders at startup from the effective
# governance so a runtime PANELLA_GOVERNANCE_OVERLAY is honored (P3b — GH Codex bot P2).
RUN panella-render-config --out /app/dist-config

# Startup render entrypoint: honors a runtime overlay's identity in the serving + MCP profiles.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN useradd --create-home --uid 10001 panella \
    && mkdir -p /app/data \
    && chown -R panella:panella /app/data /app/dist-config \
    && chgrp -R 0 /app/data /app/dist-config \
    && chmod -R g=rwX /app/data /app/dist-config
RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true
USER 10001

EXPOSE 8001
# Health = SERVING, not merely alive: an unauthenticated hit on a gated memory route must be
# 401 (auth armed, coherence gate open). 503 = the startup self-check REFUSED (wrong/missing
# overlay, unreadable store) — the container reports unhealthy so `up --wait` fails loud
# instead of a "healthy" stack whose memory surface is dark. 200 would mean auth is broken.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import sys,urllib.request,urllib.error\ntry:\n    urllib.request.urlopen('http://127.0.0.1:8001/v1/memory/search', timeout=4)\n    sys.exit(1)\nexcept urllib.error.HTTPError as e:\n    sys.exit(0 if e.code == 401 else 1)\nexcept Exception:\n    sys.exit(1)"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["panella-http"]
