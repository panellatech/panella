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

### Running as your own uid (native Linux)

The approval token is a host file (mode `0600`) that the `panella-http` container also reads to
verify a presented token. On Docker Desktop (macOS/Windows) it is readable inside the container
regardless of uid. On **native Linux**, bind mounts preserve host uid/gid and the image runs as uid
`10001`, so a `0600` file owned by your host user is unreadable inside the container and every
approval silently fails. `panella init --verify` catches this — it runs the token check *inside* the
running container, so a "looks fine on the host" false pass is impossible.

The fix is to run the service under your own uid and give it writable homes for the TWO places it
writes: the `panella-http-data` volume (token/audit/outbox DBs) **and** `/app/dist-config`, which the
entrypoint re-renders on every startup and is image-owned by uid `10001` — with only the `user:`
override the container crashes at boot with `PermissionError: /app/dist-config/...` before it ever
serves. Bind the rendered-config dir to a host-owned path alongside the uid override:

```yaml
# docker-compose.override.yml
services:
  panella-http:
    user: "${UID:-1000}:${GID:-1000}"
    volumes:
      # entrypoint re-renders config here each boot; must be writable by YOUR uid
      - ./.panella/dist-config:/app/dist-config
```

```bash
mkdir -p .panella/dist-config
```

Compose reads `${UID}`/`${GID}` from the process environment or a `.env` file, and the shell's `UID`
is **not exported by default** — so persist your real uid/gid where Compose will see them (otherwise
the fallback `1000` is used and, unless you happen to be uid 1000, the container still can't read the
0600 token):

```bash
printf 'UID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

Then fix the data volume's ownership. Order matters: Docker populates a named volume from the image
on FIRST use and that copy carries the image's `10001:10001` ownership — a chown done *before* the
first `up` is silently overwritten. So bring the stack up once, then chown the populated volume, then
restart the service:

```bash
docker compose up -d --wait || true   # first boot populates the volume (10001-owned)
docker compose stop panella-http
# volume name is namespaced by the compose project (name: panella-selfhost)
docker run --rm -v panella-selfhost_panella-http-data:/app/data alpine \
  chown -R "$(id -u):$(id -g)" /app/data
docker compose up -d --wait
```

Now the container process shares your uid — it can read the mounted `0600` token, write its state,
and render its config. Re-run `panella init --verify` and expect every check to PASS, including the
two `[container]` lines. (Docker Desktop users can ignore all of this.)

This sequence is exactly what a real native-Linux deployment exercised end-to-end (both failure modes
above were hit and confirmed before this section was corrected). First-class arbitrary-uid support in
the image — so none of this is needed — is tracked as a follow-up issue.
