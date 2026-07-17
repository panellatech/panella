# Quickstart: 15 minutes to your first approved memory

This path starts a local Panella box, provisions the owner bearer and local
approval files, connects an MCP client, queues one memory candidate, approves it
with the operator-only token, and recalls it.

The two secrets stay separate:

- Owner bearer: paste only into the agent/MCP client config.
- Approval token: operator-only; never paste it into an agent/MCP client config.

## 0-3 min: start the local box

```bash
mkdir -p .panella
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up --wait
```

The first boot uses the safe read-only MCP profile. Writes are enabled only after
the next step creates the local approval overlay.

## 3-5 min: provision first-run access

Run `panella init` from the checkout on the host. It mints the owner bearer in
the running `panella-http` container when compose is up, writes
`.panella/approval-token` and `.panella/owner-bearer` with mode `0600`, writes
`.panella/governance.yaml`, updates `.env` for write-capable MCP, restarts
compose, and verifies the running box.

```bash
OWNER_BEARER="$(panella init --yes | tee /dev/stderr | sed -n '1p')"
```

Store the owner bearer now. It is printed once and is not recoverable from the
token database later. It is also saved to `.panella/owner-bearer` so
`panella connect` can read it automatically.

The approval token value is not printed. Keep `.panella/approval-token` local and
operator-only.

## 5-7 min: restart with write-capable MCP

`panella init --yes` writes these compose dotenv lines and runs
`docker compose up -d --wait` for you:

```dotenv
PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml
PANELLA_MCP_PROFILE=mcp-write
```

Verify the running box:

```bash
panella init --verify
```

Expected result: every line starts with `PASS`.

## 7-9 min: connect your MCP client

Print the snippet for your client and paste it into that client.

Claude Code:

```bash
panella connect --print claude-code
```

Claude Desktop:

```bash
panella connect --print claude-desktop
```

Cursor:

```bash
panella connect --print cursor
```

Each snippet contains only the owner bearer for
`http://127.0.0.1:8001/mcp`. It never includes the approval token or the token
file path.

## 9-12 min: queue a memory candidate

In the MCP client, ask the agent to store a memory through Panella:

```text
Use Panella to remember: Panella remembers operator-approved local preferences.
```

The write is queued as a candidate. The MCP write profile cannot write durably by
itself.

List pending candidates from the operator shell and capture the first id:

```bash
PENDING_JSON="$(curl -sS \
  -H "Authorization: Bearer $OWNER_BEARER" \
  -H "X-Approval-Token: $(cat .panella/approval-token)" \
  http://127.0.0.1:8001/v1/approvals/pending)"
printf '%s\n' "$PENDING_JSON" | python3 -m json.tool
APPROVAL_ID="$(printf '%s\n' "$PENDING_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["pending"][0]["approval_id"])')"
```

## 12-14 min: approve it

Approval is double-factor: the owner bearer admits the HTTP route, and the
operator token authorizes the local approval action.

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $OWNER_BEARER" \
  -H "X-Approval-Token: $(cat .panella/approval-token)" \
  "http://127.0.0.1:8001/v1/approvals/$APPROVAL_ID/approve" | python3 -m json.tool
```

A `panella approvals` CLI is coming in B2b; for now the curl route is the
documented operator approval surface.

## 14-15 min: recall it

In the MCP client, ask:

```text
Search Panella for operator-approved local preferences.
```

The returned hits should include the memory text you submitted. If it does not
appear immediately, repeat the search once; the store indexes the approved
durable write moments after approval finalizes.

## Next steps

- One box for your whole team — teammate bearers, offboarding, and the daily
  approval rhythm: [recipes/claude-code-team-memory.md](recipes/claude-code-team-memory.md)
