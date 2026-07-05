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

Deferred surfaces are intentionally absent from this public product repo: private gateway, eval
package, and HTTP approval routes.
