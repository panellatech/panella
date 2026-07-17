# Quickstart: 15 minutes to your first approved memory

One person, one box, the full governed loop: install, connect an MCP client, queue one memory
candidate, approve it with the operator-only token, and recall it.

The two secrets stay separate throughout:

- **Owner bearer**: paste only into the agent/MCP client config.
- **Approval token**: operator-only; never paste it into an agent/MCP client config.

(Delegating the install to an AI agent instead? Point it at
[llms-install.md](../llms-install.md) — the agent-facing runbook for the same install.)

## 0-5 min: install and start the box

`panella up` is the whole install: it materializes a release-pinned `docker-compose.yml` and
`.env` into a **box home** directory, starts the containers, provisions the owner bearer,
approval token, and governance overlay (`panella init`), and prints a Claude Code connection
block:

```bash
uv tool install panella==0.2.0      # pin the release you are installing
mkdir -p ~/panella-box && cd ~/panella-box
panella up --yes --home "$PWD"
```

The first run pulls the box images — allow a few minutes. The embedding model is baked into the
image, so there is no first-boot model download. On success, stdout ends with:

```
Claude Code
claude mcp add --transport http panella http://127.0.0.1:8001/mcp --header "Authorization: Bearer <bearer>"
Other clients — run from <home>: `panella connect --print claude-desktop` or `panella connect --print cursor`
Next steps: keep the operator approval token outside agent configuration.
```

The `claude mcp add …` line embeds the live owner bearer — treat it as a secret. The bearer is
also saved to `.panella/owner-bearer` (mode `0600`) in the box home, so `panella connect` can
re-print it and you can export it for the approvals CLI later (the approvals CLI does not
auto-read it). The operator-only approval token lives at `.panella/approval-token`; nothing in
this walkthrough ever pastes it anywhere.

Verify the box end to end, then stay in the box home — the compose project, `.env`, and both
credential files resolve relative to it:

```bash
panella init --verify
```

Expected: every line starts with `PASS`.

## 5-7 min: connect your MCP client

**Claude Code**: run the exact `claude mcp add` line `up` printed, **from the project directory
where you use Claude Code** — the default scope registers the server for the current project
path, so running it from the box home would register it for the wrong project.

**Claude Desktop / Cursor**: print the snippet from the box home and merge it into that
client's MCP settings file (preserve your other servers):

```bash
panella connect --print claude-desktop   # or: cursor
```

Each snippet contains only the owner bearer for `http://127.0.0.1:8001/mcp` — never the
approval token.

## 7-10 min: queue a memory candidate

In the connected client, ask the agent to store a memory through Panella:

```text
Use Panella to remember that this box passed its quickstart — store it in room `preferences`
with memory_type `owner_preference`.
```

The write queues as a candidate (the tool result carries an `approval_id`, not a durable
write) — the MCP write profile cannot write durably by itself. Reads stay clean until approval:
a `memory.search` for the same text does not return it yet.

## 10-12 min: approve it

Approval is double-factor: the owner bearer admits the route, and the operator-only approval
token (read automatically from `.panella/approval-token`) authorizes the decision. From the box
home:

```bash
export PANELLA_BEARER="$(cat .panella/owner-bearer)"
panella approvals list
panella approvals approve <id>
```

Expected: the list shows your candidate in a table (`ID  BY  WING  ROOM  TYPE  CREATED
PREVIEW`); `approve` prints `approved <id> durable_id=<digest>`.

## 12-13 min: recall it

In the MCP client, ask:

```text
Search Panella for: this box passed its quickstart.
```

The hits now include the approved memory. If it does not appear immediately, repeat the search
once; the store indexes the approved durable write moments after approval finalizes.

## From a git checkout instead

Developing Panella, or building the images yourself? The checkout flow provisions the same box
with `panella init` — one shot: it mints both credentials, writes the governance overlay,
updates `.env` for the write-capable MCP profile, and restarts the stack:

```bash
python -m pip install .
mkdir -p .panella      # create it yourself — a compose-created bind mount would be root-owned
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
# native Linux: apply the uid override from SELF_HOST.md first (Docker Desktop: skip)
docker compose up -d --wait
panella init --yes
panella init --verify
```

Box home = the checkout directory; everything above applies unchanged.

## Next steps

- One box for your whole team — teammate bearers, offboarding, and the daily
  approval rhythm: [recipes/claude-code-team-memory.md](recipes/claude-code-team-memory.md)
- Approvals in the browser — the flag-gated operator console: [CONSOLE.md](CONSOLE.md)
- Configuration and Docker topology: [SELF_HOST.md](SELF_HOST.md) · backup, upgrade, rollback:
  [UPGRADE.md](UPGRADE.md)
