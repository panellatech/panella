# Self Hosting Panella

## One Command

```bash
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up --wait
```

The compose stack builds two targets from the same Dockerfile: `store` runs the pinned local
SQLite Panella store, and `app` runs the governed facade. The facade mounts the store volume
read-only for startup coherence checks; all writes go through the HTTP store adapter.

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `PANELLA_API_KEY` | required in compose | Shared internal store/facade secret. |
| `PANELLA_CONFIG_DIR` | `/app/dist-config` | Rendered agent profile and wing config directory. |
| `PANELLA_HTTP_PROFILE` | `serving` | HTTP facade profile. |
| `PANELLA_HTTP_HOST` | `0.0.0.0` in container | Bind host inside the container. |
| `PANELLA_HTTP_PORT` | `8001` | Facade port. |
| `PANELLA_BASE_URL` | `http://panella:8000` in compose | Store base URL used by the facade adapter. |
| `PANELLA_STORE_PATH` | `/data/sqlite_vec.db` in compose | Read-only store path for coherence checks. |
| `PANELLA_GOVERNANCE_OVERLAY` | unset | Optional local overlay merged over the generic base governance. |
| `PANELLA_MCP_ENABLED` | `1` in image | Enables the `/mcp` network surface. |
| `PANELLA_MCP_PROFILE` | `mcp-read` | Use `mcp-write` only after provisioning a local approval token and approver overlay. |
| `PANELLA_MCP_ALLOWED_HOSTS` | loopback hosts | Host allowlist for the MCP mount. |

## Data

Compose creates three named volumes: `panella-store`, `panella-model-cache`, and
`panella-http-data`. The store runs with local SQLite embeddings and no provider API key.

## Approval Setup

The shipped default governance is inert-closed for approvals. To enable MCP writes, create a local
overlay that sets `approval.authorized_approvers`, writes a `local_cli` token file with mode `0600`,
sets `PANELLA_GOVERNANCE_OVERLAY` to that overlay, and starts the facade with
`PANELLA_MCP_PROFILE=mcp-write`.
