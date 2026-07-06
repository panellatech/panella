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

- A Linux or macOS host your teammates' Claude Code sessions can reach. Start with the box on
  loopback on a single machine (this recipe's shape); if your team is not all on that one machine,
  see [docs/SELF_HOST.md](../SELF_HOST.md) for LAN/tailnet hardening notes before you open the bind
  beyond `127.0.0.1` — that step is out of scope here.
- **Native Linux only**: apply the uid override from
  [docs/SELF_HOST.md](../SELF_HOST.md#running-as-your-own-uid-native-linux) BEFORE Step 4. Bind
  mounts preserve host uids there and the image runs as uid `10001`, so without the override the
  container cannot read the `0600` operator files `panella init` writes and Step 4's
  `[container]` verify lines FAIL. (macOS Docker Desktop is unaffected — its file sharing maps
  ownership for you.)
- Docker and Docker Compose.
- A clone of this repository (`git clone` + `python -m pip install .` from the checkout — see
  step 1 below).
- About 15 minutes.

## 3. AGENT RUNBOOK

Paste this section at your agent (or follow it yourself) on the host that will run the box. Each
step is one command, its expected output, and a one-line triage if it doesn't match.

### Step 1 — clone and install the CLI

```bash
git clone <this-repo-url> panella && cd panella
python -m pip install .
```

**Expected:** `pip` finishes with `Successfully installed panella-<version>`.
**On failure:** confirm `python -m pip --version` works and you have network access to fetch
build dependencies; retry with `python -m pip install -v .` for verbose output.

### Step 2 — generate the internal service secret

```bash
mkdir -p .panella
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
```

**Expected:** no output; `.env` now contains one `PANELLA_API_KEY=...` line.
**On failure:** if `openssl` is missing, generate 32 random hex bytes any other way (e.g.
`python3 -c "import secrets; print(secrets.token_hex(32))"`) and paste it into `.env` by hand.

### Step 3 — start the box

```bash
docker compose up -d --wait
```

**Expected:** both `panella` and `panella-http` services report healthy; the command returns.
**On failure:** run `docker compose logs panella-http` — a missing `.env` (Step 2 skipped) is the
most common cause; `docker compose up -d --wait` again after fixing it.

### Step 4 — provision the box (one shot)

```bash
set -o pipefail
OWNER_BEARER="$(panella init --yes | tee /dev/stderr | sed -n '1p')" || echo "INIT FAILED — do not proceed"
```

The `set -o pipefail` line is load-bearing: without it, the capture pipeline reports `sed`'s exit
code and a failed `panella init` would still look like a successful assignment.

**Expected:** the first stdout line is the owner bearer (also saved to `.panella/owner-bearer`,
mode `0600`); stderr shows the approval token and governance overlay written, `.env` updated with
`PANELLA_GOVERNANCE_OVERLAY` and `PANELLA_MCP_PROFILE=mcp-write`, the compose stack restarted, and
a block of self-verify lines all starting `PASS`. No `INIT FAILED` line.
**On failure:** `INIT FAILED` (or any `FAIL` verify line) means the box is not yet write-capable —
re-run `panella init --yes` after resolving the printed cause; do not proceed to Step 5 until every
line reads `PASS`.

> NOTE — capture the bearer: the line above is printed exactly once. If you lose it, mint a fresh
> one with `docker compose exec -T panella-http panella tokens mint --label owner-replacement`
> (verified below in §5) rather than re-running `panella init --yes` on an already-provisioned box.

### Step 5 — print the Claude Code connection snippet

```bash
panella connect --print claude-code
```

**Expected:** one line on stdout —
`claude mcp add --transport http panella http://127.0.0.1:8001/mcp --header "Authorization: Bearer <bearer>"`
(the bearer is read from `.panella/owner-bearer` automatically) — plus a stderr warning that the
output embeds a live credential.
**On failure:** if the bearer in the printed line reads `PANELLA_BEARER_HERE`, the automatic
owner-bearer read failed — fall back to
`panella connect --print claude-code --token "$OWNER_BEARER"` using the bearer captured in Step 4.

### Step 6 — connect Claude Code

Run the exact line Step 5 printed, for example:

```bash
claude mcp add --transport http panella http://127.0.0.1:8001/mcp --header "Authorization: Bearer $OWNER_BEARER"
```

**Expected:** `claude mcp add` confirms the server was added; `claude mcp list` shows `panella`.
**On failure:** re-check the URL is reachable (`curl -sf http://127.0.0.1:8001/v1/health`); re-run
`claude mcp remove panella` then retry the add.

### Step 7 — smoke-test the read path

In the connected Claude Code session, call the `memory.search` MCP tool for any query, for example
"team preferences".

**Expected:** a response with an empty or near-empty result set — this is fine, it proves the read
path works before anything has been approved yet.
**On failure:** a connection or auth error means Step 6 did not complete — re-check
`claude mcp list` and the header value.

### Step 8 — submit a marker candidate

In the same session, ask the agent to store a fact through Panella, for example: "Use Panella to
remember: this box is the team's shared Claude Code memory." The agent calls
`memory.submit_candidate` with that text.

**Expected:** the tool call reports the candidate was queued (an `approval_id`, not a durable
write) — the write profile cannot write durably by itself.
**On failure:** an error here usually means the box is not write-capable — re-run
`panella init --yes` and confirm every Step 4 line reads `PASS` before retrying.

### STOP — hand back to a human

**An approval is now pending. A HUMAN must run the next block — the agent stops here.**

### Step 9 — operator: authenticate and review

Run this block from the operator's own shell (not the agent's), on the box host, in the checkout
directory. A fresh shell does not inherit the agent's `$OWNER_BEARER` variable — read the bearer
from the file `panella init` saved for exactly this purpose:

```bash
export PANELLA_BEARER="$(cat .panella/owner-bearer)"
panella approvals list
```

**Expected:** a table with one row — `ID  WING  ROOM  TYPE  CREATED  PREVIEW` — showing the marker
candidate from Step 8.
**On failure:** `No pending approvals.` means Step 8 did not actually queue (re-check the agent's
tool call succeeded); a token error means `.panella/owner-bearer` is missing or not the bearer from
Step 4 — re-check you are in the directory Step 4 ran in.

### Step 10 — operator: approve it

```bash
panella approvals approve <id>
```

Substitute `<id>` with the `ID` column value from Step 9.

**Expected:** `approved <id> durable_id=<n>`.
**On failure:** `approval token file not found` means `.panella/approval-token` is missing or
unreadable from this shell — confirm you're in the same directory Step 4 ran in.

### Step 11 — agent: confirm the fact is now recallable

Back in the connected Claude Code session, call `memory.search` again for the same query as
Step 7.

**Expected:** the hit set now includes the fact approved in Step 10.
**On failure:** repeat the search once — indexing can lag the approval by a moment. If it still
does not appear, re-check Step 10 returned a `durable_id`.

### Done

The box is provisioned, one teammate is connected, and the full submit-approve-recall loop is
proven end to end. Repeat §5 below for each additional teammate.

## 4. The governance boundary

The agent installs everything above except the power to approve itself. Panella enforces this by
**credential separation**, not filesystem sandboxing: the owner bearer (what the agent's MCP client
holds) can only route requests and submit candidates; approving requires a second credential — the
operator-only approval token — that is never handed to the agent, and the finalizer independently
re-verifies it before anything becomes durable (`docs/GOVERNANCE.md`). That is why `--yes`
automation stops exactly at Step 8: submitting is scriptable, approving is not.

On a single-uid host, a process that can read the operator's files could in principle read
`.panella/approval-token` too — Panella does not claim the agent "cannot" read it in that setup.
What is true is narrower and still load-bearing: approving requires a credential the agent's
process is never given. Keep that credential on the operator's side of a real boundary — run
approvals from the operator's own shell/session (Steps 9-10 above), and on a shared host keep
`.panella/` outside the agent's workspace root and/or run the agent as a different user. The
operator console (`docs/CONSOLE.md`) and the CLI's `approvals` commands are both just front ends to
that same server-side check — neither is a stronger boundary than the credential itself.

## 5. Team on-ramp

Each additional teammate needs their own bearer pointed at the same box — never hand out the
operator's own bearer. Two ways to do that:

- **Operator shares the same connect snippet.** Every teammate's Claude Code gets the identical
  `panella connect --print claude-code` output the operator already has. Simple, but the operator
  can't tell teammates apart in the audit trail and can't revoke one without revoking all.
- **Mint a fresh bearer per teammate**, inside the running container (a bare host-side
  `panella tokens mint` writes to the *host's* default token DB — not the box the team is actually
  talking to):

  ```bash
  TEAMMATE_BEARER="$(docker compose exec -T panella-http panella tokens mint --label teammate-<name>)"
  panella connect --print claude-code --token "$TEAMMATE_BEARER"
  ```

  Paste the printed line into that teammate's Claude Code. Omitting `--token` here would make
  `connect` fall back to reading the *operator's* `.panella/owner-bearer` — exactly the wrong
  credential to hand a teammate.

**Honesty constraint:** per-teammate tokens give you *recognition*, not control — a distinct,
operator-recognizable label in the token database, nothing more today. They are NOT least-privilege
identity, they do NOT give per-teammate audit attribution (every bearer minted this way is bound to
the same owner/root principal — the `/mcp` surface requires it — and the audit trail records the
*principal*, so all teammates' actions appear under that shared identity), and there is NO exposed
revoke surface yet (`panella tokens` currently only mints; the token store schema supports
revocation, but no CLI/HTTP operation performs it). If a teammate must lose access today, the ONLY
thing that actually invalidates bearers is resetting the token database — rotating
`PANELLA_API_KEY` or re-running `panella init --force` does NOT help (bearers resolve from the
token DB alone, and old bearers stay valid after a re-provision). The reset kills EVERY bearer at
once while preserving the audit trail and outbox (they live in separate files in the same volume):

```bash
docker compose exec -T panella-http sh -c 'rm /app/data/memory_tokens.db*'
docker compose restart panella-http
```

(The glob matters: the token DB runs in SQLite WAL mode, so `-shm`/`-wal` sidecars sit next to it.)

Then re-mint the operator's bearer and each remaining teammate's (§5 above), and reconnect every
client. Blunt, but honest — until a real `tokens revoke` ships, offboarding one teammate costs
re-issuing everyone.

## 6. Daily rhythm

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
  raw entries. (All teammates currently act as the shared owner principal — see §5's honesty
  constraint — so per-person attribution is limited either way.)

## 7. Uninstall / reset

```bash
docker compose down -v
rm -rf .panella
claude mcp remove panella
```

This stops the box, deletes its volumes (all stored memories and the token database), removes the
local operator secrets, and disconnects Claude Code. There is no recovery after `down -v` without a
prior `panella backup`.
