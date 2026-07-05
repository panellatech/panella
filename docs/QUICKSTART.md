# Quickstart: first approved, recalled memory

This compose path starts a fresh local box, mints the agent-facing owner bearer,
arms local approval, submits a candidate, approves it through the shipped MCP tool,
and recalls it. The two secrets are separate:

- Owner bearer: goes in the agent/MCP client config.
- Approval token: operator-only; never put it in an agent/MCP client config.

## 0-3 min: start the box

```bash
mkdir -p .panella
echo "PANELLA_API_KEY=$(openssl rand -hex 32)" > .env
docker compose up --wait
```

## 3-4 min: mint the owner bearer

```bash
OWNER_BEARER="$(docker compose exec -T panella-http panella tokens mint)"
```

Keep this shell open. The bearer is captured in `OWNER_BEARER` and is not
recoverable from the token DB later.

## 4-7 min: arm local approval

```bash
APPROVAL_CREDENTIAL="$(openssl rand -hex 32)"
docker compose exec -T -e APPROVAL_CREDENTIAL="$APPROVAL_CREDENTIAL" panella-http \
  sh -lc 'umask 077; printf "%s\n" "$APPROVAL_CREDENTIAL" > /app/data/approval.token'
cp config/governance.example.yaml .panella/governance.yaml
python3 - <<'PY'
from pathlib import Path

path = Path(".panella/governance.yaml")
text = path.read_text(encoding="utf-8")
text = text.replace('token_file: "~/.panella/approval.token"', 'token_file: "/app/data/approval.token"')
# NOTE: no need to edit the overlay's `config_dir` — the app image bakes
# PANELLA_CONFIG_DIR=/app/dist-config, and the env var takes precedence over paths.config_dir.
path.write_text(text, encoding="utf-8")
PY
cat >> .env <<'EOF'
PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml
PANELLA_MCP_PROFILE=mcp-write
EOF
docker compose up --wait --force-recreate panella-http
```

Adding `local_cli:owner` arms the finalizer. Without that approver, approval
requests remain inert-closed.

## 7-9 min: add the MCP server

Use this MCP config shape for Claude Code, Claude Desktop, or Cursor, replacing
`<OWNER_BEARER>` with the value in your shell variable. This config contains only
the owner bearer. The approval token stays operator-only.

```json
{
  "mcpServers": {
    "panella": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": {
        "Authorization": "Bearer <OWNER_BEARER>"
      }
    }
  }
}
```

## 9-15 min: submit, approve, recall

This uses the same network MCP endpoint your agent uses. The operator approval is
the shipped `memory.approve_candidate` MCP tool; the approval token is presented
only as that operator action's `credential` argument.

```bash
docker compose exec -T -e OWNER_BEARER="$OWNER_BEARER" -e APPROVAL_CREDENTIAL="$APPROVAL_CREDENTIAL" \
  panella-http python - <<'PY'
import anyio
import json
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def payload(result):
    return json.loads(result.content[0].text)


async def main():
    headers = {"Authorization": f"Bearer {os.environ['OWNER_BEARER']}"}
    credential = os.environ["APPROVAL_CREDENTIAL"]
    async with streamablehttp_client("http://127.0.0.1:8001/mcp", headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            submitted = payload(await session.call_tool("memory.submit_candidate", {
                "content": "Panella remembers operator-approved local preferences.",
                "room": "preferences",
                "memory_type": "owner_preference",
            }))
            approved = payload(await session.call_tool("memory.approve_candidate", {
                "approval_id": submitted["approval_id"],
                "credential": credential,
            }))
            recalled = payload(await session.call_tool("memory.search", {
                "query": "operator-approved local preferences",
                "k": 3,
            }))
    print(json.dumps({"submitted": submitted, "approved": approved, "recalled": recalled}, indent=2))


anyio.run(main)
PY
```

Done means the final `recalled.hits` array includes the memory text submitted in
the first MCP call. (If the just-approved memory isn't in the first `search` yet,
re-run the `memory.search` call — the store indexes the durable write moments after
the approval finalizes.)
