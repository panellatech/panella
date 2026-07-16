# Self Hosting Panella

## One Command

```bash
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
mkdir -m 0700 .panella      # create it yourself first — see the native-Linux note below for why
docker compose up --wait
```

The compose stack builds two targets from the same Dockerfile: `store` runs the pinned local
SQLite Panella store, and `app` runs the governed facade. The facade mounts the store volume
read-only for startup coherence checks; all writes go through the HTTP store adapter.

## Embedding model: baked, offline by default

The store image bakes `sentence-transformers/all-MiniLM-L6-v2` at revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`. Its ten required model files are SHA-256-manifested
at build time, and released images are cosign-signed. Runtime starts with `HF_HUB_OFFLINE=1`,
`TRANSFORMERS_OFFLINE=1`, and `HF_HUB_DISABLE_TELEMETRY=1`: ordinary first boot therefore makes
no model or telemetry network request.

Before serving, the store entrypoint verifies that the build-time fail-loud guard is present and
loads/encodes with the selected backend. A missing baked model or unusable local backend stops the
container before it serves. The guard independently rejects upstream's pure-Python hash-embedding
fallback; it remains active even if preflight is skipped.

`BAKE_EMBEDDING_MODEL=0` is an expert bring-your-own-model build contract, not an image-size
toggle. Choose one of these explicit paths: build the default model into the image (the normal
setting); make a custom model available in the runtime cache; or deliberately allow first-start
download for a custom model with all three settings below:

```yaml
HF_HOME: /home/panella/.cache/huggingface
HF_HUB_OFFLINE: "0"
TRANSFORMERS_OFFLINE: "0"
```

The same three-setting recipe is required when `MCP_EMBEDDING_MODEL` selects a custom model not
already cached. Preflight is the first downloader in that mode and persists it below `HF_HOME`.

`MCP_MEMORY_USE_ONNX=1` is an opt-in path. It requires `onnxruntime`, `tokenizers`, and a usable
`$HOME/.cache/mcp_memory/onnx_models/all-MiniLM-L6-v2/onnx/model.onnx` cache; mount/populate that
cache or leave the variable unset. Upstream can still transiently fall back from a failed ONNX
initialization to SentenceTransformer, so treat ONNX as an explicit operational choice rather than
the default model path.

`MCP_EXTERNAL_EMBEDDING_URL` delegates to upstream's external fail-loud path only for the
`sqlite_vec` backend. `hybrid` still needs its local SQLite primary and local model; `cloudflare`
uses Workers AI and has no local-model preflight requirement.

Two independent emergency knobs exist. `PANELLA_SKIP_EMBEDDING_PREFLIGHT=1` skips only the
entrypoint's model checks and prints a warning on every boot; failures then surface at serving
start. `PANELLA_REQUIRE_REAL_EMBEDDINGS=0` permits the guarded hash fallback and also prints a
warning. Silent degraded hash embeddings require both settings to be deliberately enabled.

## Zero-clone bootstrap

For a released wheel, bootstrap one self-hosted box without cloning the repository:

```bash
uvx panella up --yes
```

`panella up` materializes the wheel-embedded digest-pinned compose file and `.env` in
`~/panella-box` (or `PANELLA_HOME` / `--home`), starts the box, activates it with `panella init`,
and prints a Claude Code connection block. The generated `.env` pins `PANELLA_UID`/`PANELLA_GID`
to the invoking user so the containers can read the bind-mounted operator files on native Linux.
One canonical home maps to one Compose project; use a different `--home` for a separate box. It is
intentionally not a development command: when run from a clone, use `panella init` instead.

The embedded compose asset is release-specific. Hand edits, drift, or an asset from a different
release are refused rather than upgraded in place; follow [UPGRADE.md](UPGRADE.md) for upgrades.
An air-gapped machine still needs the wheel and the digest-pinned images available locally: `up`
does not fetch a compose file, but Docker may need to pull images unless they have been preloaded.

`up`/agent workflows never need, and are never handed, the approval credential — bearer and
approval-token are separate credentials, and the agent-facing write path is propose-only by
design (the MCP approval endpoints that do exist require that separate approval credential).
Mode `0600` blocks subjects running as *other* UIDs. Since the arbitrary-uid work, the containers
run as the operator's own UID, so neither the container nor a full-shell agent under that UID is
a separate subject; the hard subject boundary is the agent's sandbox/permission model, or moving
approval to another OS user/device (the operator console / C0-B `.mcpb` approval endpoint).

The per-home lock uses POSIX `flock`; it coordinates concurrent `up` calls on the same host only.
`up` and a separately started `init` are not a transaction and should not be run concurrently. If
`.panella` is lost while project containers or volumes remain, `up` stops and prints a recovery or
explicit destructive-reset command; it never deletes those resources itself.

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
| `PANELLA_UID` | `10001` | Container uid; set to `id -u` on native Linux. |
| `PANELLA_GID` | `0` | Container primary gid; set to `id -g` on native Linux. |

## Data

Compose creates three named volumes: `panella-store`, `panella-model-cache`, and
`panella-http-data`. It also mounts local `.panella/` to `/app/local` for an optional
operator-owned governance overlay; the local approval token should live in `panella-http-data`
with mode `0600`. The store runs with local SQLite embeddings and no provider API key. Its named
model-cache volume is for the optional ONNX cache and other runtime caches; the default
SentenceTransformer model is baked at `/opt/hf-cache` and is not hidden by that volume.

## Approval Setup

The shipped default governance is inert-closed for approvals. `panella init` does the whole setup in
one command (mints the owner bearer, writes the `local_cli` token file at mode `0600`, writes the
approver overlay, enabling `PANELLA_MCP_PROFILE=mcp-write`); `panella init --verify` then confirms it
end to end. The owner bearer belongs in the MCP client config; the approval token is operator-only.

See [QUICKSTART.md](QUICKSTART.md) for the end-to-end submit, approve, and recall flow.

### Running as your own uid (native Linux)

The approval token is a host file (mode `0600`) that `panella-http` reads through the `/app/local`
bind mount. On native Linux, make both services run as your host uid from their first instruction,
and create `.panella` yourself first — the compose file bind-mounts `./.panella:/app/local:ro`, and
if the directory is missing at first boot the Docker daemon creates it **root-owned**, which then
blocks `panella init` (running as your uid) from writing the approval token and overlay:

```bash
printf 'PANELLA_UID=%s\nPANELLA_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
mkdir -m 0700 .panella          # create it as your uid BEFORE the first `docker compose up`
```

Compose adds supplementary group `0` to both services. The image makes each mutable image path
group-`0` writable, so a fresh install needs no migration: the caller uid can create state in those
directories, and the files it creates are caller-owned. Facade token and audit files stay mode `0600`,
which their owner can read and enforce on every connection. Docker Desktop on macOS needs none of these
lines; leaving both unset keeps the default `10001:0` container identity.

### Upgrading pre-C0-U named volumes

Fresh installs need no migration. Existing named volumes retain their old `10001:10001` ownership
(including any `0600` facade DB files), because the image's build-time group change does not alter a
non-empty volume. For a native-Linux upgrade, stop the stack and run these one-time commands to make
every existing volume entry owned by the caller identity that the services will use:

```bash
docker compose down
# TARGET must be the SAME identity Compose runs the services as. Ask Compose for its EFFECTIVE
# resolved `user:` — this honors the real interpolation precedence (a shell-exported PANELLA_UID
# outranks the .env file), so the chown can never disagree with the uid the services actually start
# as. Both services resolve to the same value.
TARGET="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["panella-http"]["user"])')"
docker compose run --rm --no-deps --user 0:0 -e TARGET="$TARGET" --entrypoint sh panella-http -c \
  'find -P /app/data \( -type d -o -type f \) -exec chown "$TARGET" {} +'
docker compose run --rm --no-deps --user 0:0 -e TARGET="$TARGET" --entrypoint sh panella -c \
  'find -P /data /home/panella/.cache \( -type d -o -type f \) -exec chown "$TARGET" {} +'
docker compose up -d --wait
```

The migration preserves existing modes: it uses `chown`, not `chgrp` plus `chmod`. This is necessary
for existing `0600` token and audit DB files: the caller must own them so its normal connection-time
`chmod 0600` succeeds, rather than failing with `EPERM` as a non-owner. The helpers are explicitly root
only for this one ownership repair; normal services and their healthchecks remain non-root. The walk
is symlink-safe: `find -P` never traverses a symlinked directory, and `\( -type d -o -type f \)`
restricts `chown` to real directories and files, so no symlink is ever dereferenced (a stale link
could otherwise point `chown` outside the volume). Re-run `panella init --verify` after the stack is
healthy; every check should PASS, including the two `[container]` lines.
