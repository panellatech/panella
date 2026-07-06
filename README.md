# Panella

**Governed, self-hosted memory for AI agents.**

Your agents write to a memory your company actually controls: every write is proposed, approved, and
attributed — never a silent background rewrite. A standard **MCP server**: Claude Code, Claude
Desktop, Cursor, or any MCP client connects with one line. Default-deny, fully auditable, runs on
your own box. Apache-2.0.

```bash
mkdir -p .panella && echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --wait
panella init                          # one command: owner token, approval token, governance overlay
printf 'PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml\nPANELLA_MCP_PROFILE=mcp-write\n' >> .env
docker compose up -d --wait           # restart write-capable
panella connect --print claude-code   # paste into your agent
```

Your agent proposes a memory → it queues → you approve it (CLI, console, or API) → your agent recalls
it next turn. Nothing becomes durable truth without a named approver and an audit trail.

## Two ways to build agent memory

Most memory layers consolidate in the background: memories are merged, summarized, and updated
automatically. That design is a deliberate, reasonable choice for personal assistants — speed over
ceremony.

Panella takes the other branch, for teams and companies: writes queue as proposals, a named person
approves them, and provenance is kept — so when someone asks *"who decided this was true, and where
did it come from?"*, the system has an answer.

- **Default-deny writes** — an agent proposes; nothing lands until a person approves.
- **Two-factor approval** — the agent's bearer is routing admission only; a separate operator-held
  approval token is the approver identity, re-verified independently at finalize time. An agent
  cannot approve its own memory.
- **Attributed, hash-chained audit** — every approval records who, when, and from what source.
- **Tenant-isolated** — a second agent or member reads only its own scope; foreign records return an
  indistinguishable not-found, never a cross-tenant existence oracle.
- **MCP-native** — a standard MCP server (Streamable HTTP). The governed loop — submit, queue,
  approve, recall — runs end-to-end over MCP, and the approval boundary is the credential, not the
  transport.
- **Runs on your box** — Docker Compose, SQLite, loopback-only by default. Your data, your bytes.

## Quickstart

```bash
mkdir -p .panella       # create it yourself — a compose-created bind mount would be root-owned
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up -d --wait
panella init            # provisions owner bearer + local approval token + governance overlay
printf 'PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml\nPANELLA_MCP_PROFILE=mcp-write\n' >> .env
docker compose up -d --wait   # restart into the write-capable MCP profile
panella init --verify   # confirms the box is serving and write-capable
```

For the full copy-paste path from a fresh box to your first approved, recalled memory — including
connecting Claude Code, Claude Desktop, Cursor, or any other MCP client — see
**[docs/QUICKSTART.md](docs/QUICKSTART.md)** (about 15 minutes).

## Operate it

- **[docs/SELF_HOST.md](docs/SELF_HOST.md)** — configuration and the Docker topology.
- **[docs/CONSOLE.md](docs/CONSOLE.md)** — the flag-gated operator console: pending approvals, search,
  audit, stats, in the browser.
- **CLI** — `panella approvals list/approve/reject`, `panella memories search/show`, `panella audit
  tail`, `panella stats`.
- **[docs/UPGRADE.md](docs/UPGRADE.md)** — backup, upgrade, and rollback.
- **[docs/GOVERNANCE.md](docs/GOVERNANCE.md)** · **[SECURITY.md](SECURITY.md)** — the governance model
  and the security posture.

## Why governed memory comes first

Memory tools have largely solved storage and retrieval; the part a company additionally needs is the
paper trail — and that's the part Panella makes the product. An auditor asks how a fact got here, and
the system has an answer.

That's the first rung of a longer direction. Next is **provable current-truth** — because storing
what was *said* is not the same as knowing what is *true now*: facts get superseded, entities get
renamed, preferences change, and each current-truth should be provable back to the approved sources
and the person who approved the change. Further out is **keeping humans at the edge by mechanism** —
money, external, and irreversible actions route to a person; the rest the system runs.

Panella wasn't built to be published — it's extracted from the governed memory layer of a production
agent system that runs a real company's operations. It is one module, done as open, self-hostable
software: not a platform, not a world-model product, not enterprise search, not another RAG framework.

## Developer setup

Install the package and run the facade directly (without Docker):

```bash
python -m pip install .
panella-render-config --out ./dist-config
PANELLA_CONFIG_DIR=./dist-config PANELLA_API_KEY=dev-secret PANELLA_FRESH_BOX=1 panella-http
```

## License

[Apache-2.0](LICENSE). The double-factor approval trust chain (`/v1/approvals`) is the heart of the
box: the owner bearer is routing admission only; a `local_cli` approval token (header-only) is the
approver identity, re-verified by the finalizer. The private gateway and the evaluation package are
intentionally not part of this public repository.
