# Recipe: Claude Code team memory

Give a small team one governed memory box: every teammate's Claude Code can search it and propose
new facts, one operator approves what becomes durable, and the audit trail records every approval.

## 1. What you get

- One Panella box (Docker Compose, one host) holding your team's shared memory.
- Every teammate's Claude Code connects over MCP: it can search the box and propose new facts.
- Nothing becomes durable on its own — a human operator approves each candidate before it is
  recallable.
- An audit trail (`panella audit tail`) shows what was approved and when; add `--json` for the full rows including the acting principal.

This is NOT auto-consolidation. Panella never merges, summarizes, or promotes memories in the
background — a fact becomes durable only when a named human approves it.

## 2. Prerequisites

- A Linux or macOS host your teammates' Claude Code sessions can reach (WSL2 counts as Linux).
  Start with the box on loopback on a single machine (this recipe's shape); if your team is not
  all on that one machine, see [docs/SELF_HOST.md](../SELF_HOST.md) for LAN/tailnet hardening
  notes before you open the bind beyond `127.0.0.1` — that step is out of scope here.
- Docker and the Docker Compose v2 plugin.
- [`uv`](https://docs.astral.sh/uv/) — the install below uses `uv tool install`.
- About 15 minutes.

## 3. Install the box — operator, one command

Run this on the host that will hold the box. Teammates never run it — they only connect their
clients later (§6).

`panella up` is the whole install: it materializes a release-pinned `docker-compose.yml` and
`.env` into a **box home** directory, starts the containers, provisions tokens and governance
(`panella init`), and prints a Claude Code connection block. Choose the home explicitly:

```bash
uv tool install panella==0.2.0      # pin the release you are installing
mkdir -p ~/panella-box && cd ~/panella-box
panella up --yes --home "$PWD"
```

This is the persistent-runner form of the install contract in
[llms-install.md](../../llms-install.md) §0 (`uv tool install`, then plain `panella` for every
later command) — chosen here because the operator keeps using the CLI daily (§7), not just for
the install. The explicit `--home "$PWD"` makes the directory you chose and the directory `up`
provisions the same thing by construction.

**Expected:** exit `0`; after the containers report healthy, stdout ends with a
`claude mcp add --transport http panella http://127.0.0.1:8001/mcp --header "Authorization: Bearer <bearer>"`
line — the connection block, which embeds a live credential — followed by
`Other clients — run from <home>: …`. Confirm that printed home is the directory you chose. The
first run pulls the box images, so allow a few minutes; the embedding model is baked into the
image, so there is no first-boot model download.
**On failure:** `up` normally exits with a designed code and a one-line cause on stderr; match
it against the exit-code and troubleshooting tables in [llms-install.md](../../llms-install.md)
(§3 and §7). A raw traceback instead of a designed message is not one of those paths — stop and
report it as found. Re-running `up` on the same home is idempotent: it does not re-mint secrets
or recreate healthy containers.

The box home (`~/panella-box` here) now holds `docker-compose.yml`, `.env`, and the operator
secrets under `.panella/`. One home is one box is one Compose project — **every later command in
this recipe (token minting, approvals, uninstall) runs from this directory**, which is how it
lands on the right box. On native Linux there is no manual uid step: `up` pins the container
identity to your uid/gid in the generated `.env` and pre-creates `.panella` with safe modes.

Two neighbouring paths lead to the same box; both are documented once elsewhere, and this recipe
deliberately does not restate them:

- **Delegate the install to an agent.** Paste the prompt from the
  [README "For agents" section](../../README.md#for-agents); the agent follows
  [llms-install.md](../../llms-install.md) — the same `panella up`, plus wiring its own MCP
  client, objective verification, and an approval hand-back. Read §4 below first: it changes
  where the approval credential may live.
- **Working from a git clone** (developing Panella, or building images yourself): use
  `panella init` in the checkout instead of `up` — see the
  [README quickstart](../../README.md#quickstart) and [docs/QUICKSTART.md](../QUICKSTART.md).
  Everything from §4 on applies unchanged, with box home = your checkout directory.

## 4. The governance boundary — who runs what

`panella up` mints two separate credentials into `<box-home>/.panella/`:

- the **owner bearer** (`.panella/owner-bearer`) — what MCP clients hold. It can route requests
  and *propose* candidates; it can never approve them.
- the **approval token** (`.panella/approval-token`) — operator-only. Approving requires it, and
  the finalizer independently re-verifies it before anything becomes durable
  ([docs/GOVERNANCE.md](../GOVERNANCE.md)). That is why agent automation stops exactly at the
  approval step: submitting is scriptable, approving is not.

Panella enforces this by **credential separation**, not filesystem sandboxing. On a single-uid
host, a process that can read the operator's files could in principle read
`.panella/approval-token` too — Panella does not claim the agent "cannot" read it in that setup.
What is true is narrower and still load-bearing: approving requires a credential the agent's
process is never *given*. Keep that credential on the operator's side of a real boundary:

- run the approval steps (§5 and §7) from your own shell, never inside the agent's session;
- if you delegated the install (§3), the box home is readable by that agent — deny
  `.panella/approval-token` in the agent's sandbox configuration, keep the box home outside the
  agent's workspace root, or run the agent as a different OS user;
- the operator console ([docs/CONSOLE.md](../CONSOLE.md)) and the CLI `approvals` commands are
  both just front ends to the same server-side check — neither is a stronger boundary than the
  credential itself.

## 5. Prove the loop: propose → queue → approve → recall

### Step 1 — connect your own Claude Code

Run the exact `claude mcp add …` line that `up` printed, **from the project directory where you
use Claude Code** (Claude Code's default scope registers the server for the current project
path, so running it from the box home would register it for the wrong project):

```bash
claude mcp add --transport http panella http://127.0.0.1:8001/mcp --header "Authorization: Bearer <bearer>"
```

**Expected:** `claude mcp add` confirms the server was added; `claude mcp list` shows `panella`.
**On failure:** if you lost the printed line, regenerate it from the box home with
`panella connect --print claude-code` — it reads the bearer from `.panella/owner-bearer`
automatically. A `PANELLA_BEARER_HERE` placeholder in the output means that file is missing,
unreadable, or malformed — mint a replacement bearer instead (§6's in-container
`tokens mint`, with a label like `owner-replacement`) and pass it via `--token`, or re-provision
with `panella init --force` (an operator decision: it mints a new bearer and does not revoke
existing ones).

### Step 2 — smoke-test the read path

In the connected Claude Code session, call the `memory.search` MCP tool for any query, for
example "team preferences".

**Expected:** a response with an empty or near-empty result set — this is fine, it proves the
read path works before anything has been approved yet.
**On failure:** a connection or auth error means Step 1 did not complete — re-check
`claude mcp list` and the header value, and that `curl -sf http://127.0.0.1:8001/v1/health`
succeeds on the box host.

### Step 3 — submit a marker candidate

In the same session, ask the agent to store a fact through Panella, for example: "Use Panella to
remember that this box is the team's shared Claude Code memory — store it in room `preferences`
with memory_type `owner_preference`." `memory.submit_candidate` requires all three of `content`,
`room`, and `memory_type` (it returns `invalid_arguments` if `room`/`memory_type` are missing), so
name the room and type explicitly rather than relying on the agent to guess them. `preferences` /
`owner_preference` are valid out of the box.

**Expected:** the tool call reports the candidate was queued (an `approval_id`, not a durable
write) — the write profile cannot write durably by itself.
**On failure:** an error here usually means the box is not write-capable — run
`panella init --verify` from the box home and confirm every line reads `PASS` before retrying.

### STOP — approval is a human move

**A candidate is now pending. The operator runs the next step from their own shell — an agent
stops here.**

### Step 4 — operator: review and approve

From the box home. The `approvals` CLI reads the approval token automatically from
`.panella/approval-token`; the owner bearer is taken only from `--token` or the `PANELLA_BEARER`
environment variable — it is not auto-read:

```bash
cd ~/panella-box
export PANELLA_BEARER="$(cat .panella/owner-bearer)"
panella approvals list
panella approvals approve <id>
```

Substitute `<id>` with the `ID` column value from the listed marker candidate.

**Expected:** `approvals list` shows a table with one row — `ID  BY  WING  ROOM  TYPE  CREATED
PREVIEW`; `approve` prints `approved <id> durable_id=<digest>`.
**On failure:** `No pending approvals.` means Step 3 did not actually queue (re-check the agent's
tool call succeeded). An `approval token file not found` error means you are not in the box home —
the approval-token path resolves relative to the current directory. Any other auth error:
re-check the `PANELLA_BEARER` export (empty if you skipped the `cd`), the readability of the two
`.panella` files, and that `curl -sf http://127.0.0.1:8001/v1/health` still succeeds.

### Step 5 — confirm the fact is now recallable

Back in the connected Claude Code session, call `memory.search` again for the same query as
Step 2.

**Expected:** the hit set now includes the fact approved in Step 4.
**On failure:** repeat the search once — indexing can lag the approval by a moment. If it still
does not appear, re-check Step 4 returned a `durable_id`.

### Done

The box is provisioned, one teammate is connected, and the full submit-approve-recall loop is
proven end to end. Repeat §6 below for each additional teammate.

## 6. Team on-ramp

Each additional teammate needs a bearer pointed at the same box — never hand out the operator's
own `.panella/owner-bearer`. Both paths below mint a *separate* token so the operator's own bearer
never leaves the host. Run them from the box home; the token commands execute inside the running
container (run from the box home, the CLI itself enforces this: a bare `panella tokens mint`
fail-closes and prints the in-container form below, rather than writing to a host-side token
database the box never reads). Two ways to do it:

- **One shared team bearer.** Mint a single extra bearer and give its connect snippet to everyone.
  Simple, but you can't tell teammates apart in the audit trail and can't cut one off without
  re-issuing all:

  ```bash
  TEAM_BEARER="$(docker compose exec -T panella-http panella tokens mint --label team-shared)"
  panella connect --print claude-code --token "$TEAM_BEARER"
  ```

- **Mint a fresh bearer per teammate:**

  ```bash
  TEAMMATE_BEARER="$(docker compose exec -T panella-http panella tokens mint --label teammate-<name>)"
  panella connect --print claude-code --token "$TEAMMATE_BEARER"
  ```

  Paste the printed line into that teammate's Claude Code. Omitting `--token` here would make
  `connect` fall back to reading the *operator's* `.panella/owner-bearer` — exactly the wrong
  credential to hand a teammate.

**Offboard one teammate:** revoke their labelled bearer:

```bash
docker compose exec -T panella-http panella tokens revoke --label teammate-<name>
```

The bearer is rejected on every surface (HTTP `/v1` and `/mcp`) immediately; the others are
untouched. `panella tokens list` shows each token's label, principal, and status
(`active` / `revoked@…`). To see what to revoke:

```bash
docker compose exec -T panella-http panella tokens list
```

**Honesty constraint:** labels give you *revocation and recognition*, but NOT least-privilege
identity or per-teammate audit attribution — every bearer minted this way is bound to the same
owner/root principal (the `/mcp` surface requires it), and the audit trail records the
*principal*, so all teammates' actions still appear under that shared identity. Per-user scoping
and per-user attribution do not exist yet. (Rotating `PANELLA_API_KEY` or re-running
`panella init --force` does NOT invalidate a bearer — bearers resolve from the token DB alone;
`tokens revoke` is the operation that actually cuts one off.)

## 7. Daily rhythm

The operator CLI commands below (`approvals`, `stats`, `audit`) read the owner bearer from
`--token` or `PANELLA_BEARER` — they do NOT auto-read `.panella/owner-bearer`. In a fresh daily
shell, export it once first, from the box home:

```bash
cd ~/panella-box
export PANELLA_BEARER="$(cat .panella/owner-bearer)"
```

- **Submit** whenever something worth remembering comes up in a session: a decision, a stated
  preference, a gotcha worth not re-discovering. This is the agent's job — it happens through
  `memory.submit_candidate` with no operator involvement.
- **Approve** in a batch, once a day — end of day is a reasonable default. Run
  `panella approvals list`, then `panella approvals approve <id>` for each candidate worth keeping
  (or `panella approvals reject <id>` for one that isn't).
- **Review** the corpus and the trail periodically:

  ```bash
  panella stats
  panella audit tail --limit 20
  ```

  `stats` shows aggregate counts per wing. `audit tail`'s table shows the most recent
  approval/reject events (when, what, which tenant); the table does NOT print the acting
  principal — use `panella audit tail --json --limit 20` when you need attribution fields on the
  raw entries. (All teammates currently act as the shared owner principal — see §6's honesty
  constraint — so per-person attribution is limited either way.)

## 8. Uninstall / reset

From the box home, in this order — `panella up` refuses to reprovision a home whose `.panella`
was deleted while the box's containers or volumes still exist, so take the stack down before
removing the home:

```bash
cd ~/panella-box
docker compose down -v
cd .. && rm -rf panella-box
```

Then disconnect the clients. Claude Code's default scope keys the registration to the project
path — the same reason §5 Step 1 ran the `add` from the project directory — so each user runs
this in each project directory where they added it:

```bash
claude mcp remove panella
```

This stops the box, deletes its volumes (all stored memories and the token database), removes the
compose file, `.env`, and the local operator secrets, and disconnects Claude Code. There is no
recovery after `down -v` without a prior `panella backup`.
