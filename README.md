# Panella

Panella is governed, self-hosted memory for AI agents. It provides a Python package, HTTP facade,
MCP tool surface, local approval transport, generic governance config renderer, and Docker
self-host box backed by a real store.

## Install

```bash
python -m pip install .
```

## Run Locally

```bash
panella-render-config --out ./dist-config
PANELLA_CONFIG_DIR=./dist-config PANELLA_API_KEY=dev-secret PANELLA_FRESH_BOX=1 panella-http
```

## Self Host

```bash
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up --wait
```

For a copy-paste path from fresh box to first approved, recalled memory, see
[docs/QUICKSTART.md](docs/QUICKSTART.md).

See [docs/SELF_HOST.md](docs/SELF_HOST.md) for configuration and the Docker topology.

The HTTP approval API (`/v1/approvals`) ships with a double-factor trust chain: the owner bearer is
routing admission only, and a `local_cli` approval token (header-only) is the approver identity that
the finalizer independently re-verifies. Deferred surfaces intentionally absent from this public
product repo: the private gateway and the eval package.
