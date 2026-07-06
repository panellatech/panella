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
`panella-http-data`. It also mounts local `.panella/` to `/app/local` for an optional
operator-owned governance overlay; the local approval token should live in `panella-http-data`
with mode `0600`. The store runs with local SQLite embeddings and no provider API key.

## Approval Setup

The shipped default governance is inert-closed for approvals. `panella init` does the whole setup in
one command (mints the owner bearer, writes the `local_cli` token file at mode `0600`, writes the
approver overlay, enabling `PANELLA_MCP_PROFILE=mcp-write`); `panella init --verify` then confirms it
end to end. The owner bearer belongs in the MCP client config; the approval token is operator-only.

See [QUICKSTART.md](QUICKSTART.md) for the end-to-end submit, approve, and recall flow.

### Token file must be readable by the container (native Linux)

The approval token is a host file (mode `0600`) that the `panella-http` container also reads to
verify a presented token. On Docker Desktop (macOS/Windows) it is readable inside the container
regardless of uid. On **native Linux**, bind mounts preserve host uid/gid and the image runs as uid
`10001`, so a `0600` file owned by your host user is unreadable inside the container and every
approval silently fails. `panella init --verify` catches this — it runs the token check *inside* the
running container, so a "looks fine on the host" false pass is impossible.

The fix is to run the service under your own uid **and** make its data writable by that uid — the
`panella-http` service also writes its token/audit/outbox DBs into the `panella-http-data` volume,
which is initialized owned by the image uid `10001`, so changing only the process uid would leave it
unable to write its state and the container would fail before verification:

```yaml
# docker-compose.override.yml
services:
  panella-http:
    user: "${UID:-1000}:${GID:-1000}"
```

Then, **before the first `docker compose up`** (or once, on an existing box), give that uid ownership
of the data volume:

```bash
# fresh box: create the volume and chown it to your uid before starting the stack
docker volume create panella_panella-http-data
docker run --rm -v panella_panella-http-data:/app/data alpine chown -R "$(id -u):$(id -g)" /app/data
```

Now the container process shares your uid — it can both read the mounted `0600` token and write its
own state. (Docker Desktop users can ignore all of this.)
