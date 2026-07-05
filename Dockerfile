# Self-host packaging for the governed Panella store memory product (Slice-S P3a).
#
# Two independent targets, composed by docker-compose.yml:
#   store — near-stock upstream mcp-memory-service, PINNED to the version whose HTTP contract
#           the facade adapter is proven against (tests/fixtures/panella_openapi_v10.31.2.json;
#           the [sqlite] extra = local ONNX embeddings, so the store runs with NO API key).
#   app   — this repo's memory HTTP facade (panella.http.app:create_app) plus the
#           package-rendered per-distribution config artifact (/app/dist-config).
#
# `app` is last so a bare `docker build .` produces the facade image.

# --------------------------------------------------------------------------------------------
FROM python:3.12-slim AS store

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
# cryptography<47: the >=47.0.0 aarch64 wheels die with SIGILL (illegal instruction) inside
# the Rust extension under Apple-Silicon Docker (Virtualization.framework VM); 46.0.7 is proven
# good there (bisected 45.0.5 OK / 46.0.7 OK / 47.0.0+48.0.2+49.0.0 SIGILL), and upstream
# requires >=46.0.6. Image-layer mitigation only — drop once upstream wheels stop faulting.
RUN pip install "mcp-memory-service[sqlite]==10.31.2" "cryptography<47"

# /home/panella/.cache must exist owned by panella BEFORE the named volume mounts over it —
# a mountpoint Docker has to create itself is root-owned and the uid-10001 store could
# never persist its ONNX/HF embedding-model cache there.
RUN useradd --create-home --uid 10001 panella \
    && mkdir -p /data /home/panella/.cache \
    && chown -R panella:panella /data /home/panella/.cache
USER panella

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=45s --retries=12 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

CMD ["memory", "server", "--http"]

# --------------------------------------------------------------------------------------------
FROM python:3.12-slim AS app

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
    && chown -R panella:panella /app/data /app/dist-config
USER panella

EXPOSE 8001
# Health = SERVING, not merely alive: an unauthenticated hit on a gated memory route must be
# 401 (auth armed, coherence gate open). 503 = the startup self-check REFUSED (wrong/missing
# overlay, unreadable store) — the container reports unhealthy so `up --wait` fails loud
# instead of a "healthy" stack whose memory surface is dark. 200 would mean auth is broken.
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 \
    CMD ["python", "-c", "import sys,urllib.request,urllib.error\ntry:\n    urllib.request.urlopen('http://127.0.0.1:8001/v1/memory/search', timeout=4)\n    sys.exit(1)\nexcept urllib.error.HTTPError as e:\n    sys.exit(0 if e.code == 401 else 1)\nexcept Exception:\n    sys.exit(1)"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["panella-http"]
