#!/bin/sh
# Slice-S P3b — render the per-distribution config at CONTAINER STARTUP from the EFFECTIVE
# governance (the shipped generic base + an optional PANELLA_GOVERNANCE_OVERLAY), so a runtime
# overlay's identity (default_tenant_id / owner_wing) is honored by the serving + MCP profiles —
# not frozen to the build-time generic artifact. Without an overlay this re-produces the same
# generic config (idempotent). Governance incoherence fails LOUD (non-zero exit) — the container
# refuses to serve on a broken render rather than starting with a wrong-identity config.
set -e
panella-render-config --out "${PANELLA_CONFIG_DIR:-/app/dist-config}"
exec "$@"
