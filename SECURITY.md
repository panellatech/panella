# Security Policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in Panella, please report it privately
to **security@panella.tech**. Do not open a public issue for a suspected vulnerability.

Please include enough detail to reproduce the issue (affected version, configuration, and
steps). We follow responsible disclosure: we ask that you give us a reasonable window to
investigate and ship a fix before any public disclosure, and we will keep you informed of
progress.

## Trust model

Panella is **self-hosted and owner-controlled**. There is no Panella-operated service in the
trust path — the box, its store, its tokens, and its governance config all live in the
operator's own environment. The security posture below describes what that box enforces and
what the operator is responsible for.

### Two separate secrets, two separate channels

Panella distinguishes two credentials that must **never be commingled**:

- **The owner bearer token** — the agent / routing credential. It is presented as an HTTP
  `Authorization: Bearer` header to reach the memory surface, and it authenticates a principal
  (see `panella/http/auth.py`). It is the key an agent uses to talk to the box.
- **The operator approval token** — the approval identity proof. For the self-host
  (`local_cli`) transport this is the contents of a mode-`0600` token file that an operator
  presents to *approve* a queued durable write (see `panella/approval_transport.py`). It
  proves "an authorized human said yes," not "an agent may call the API."

These are different secrets with different jobs on different channels. A bearer token can move
data through the box; it cannot approve a durable write. An approval token proves approval
identity; it is not a routing credential. Keep them stored and rotated separately. Handing an
agent the approval token, or wiring the approval token in as a bearer, collapses the
approval boundary — do not do it.

### MCP network surface: opt-in, owner-only, DNS-rebind protected

The network MCP surface (`/mcp`) is **off by default in the library** — it mounts only when
`PANELLA_MCP_ENABLED` is set. The **shipped Docker image, however, enables it**
(`PANELLA_MCP_ENABLED=1`, see the `Dockerfile` / `docs/SELF_HOST.md`), so a compose/image
deployment has `/mcp` live (read-only `mcp-read`) on the facade port out of the box, while a
from-source library run does not until you set it. Wherever it is mounted it is defense-in-depth
gated (`panella/http/app.py`):

- **Owner (root) principal required.** A merely-valid bearer token is necessary but not
  sufficient — the `/mcp` gate authorizes the token as the governance **root** principal
  (`403` otherwise). A low-privilege or foreign-tenant token cannot borrow the MCP profile's
  authority.
- **DNS-rebinding / Host / Origin protection.** The mount validates the request Host/Origin
  before any auth work, rejecting a foreign Host up front. It is loopback-only unless the
  operator explicitly sets `PANELLA_MCP_ALLOWED_HOSTS`.
- **Per-token rate limiting** on the MCP path.
- **Read by default for WRITES; approval tools are advertised but execution-gated.** The shipped
  `mcp-read` profile does not advertise `memory.submit_candidate` — `mcp-write` adds it. But under
  the default `local_cli` approval transport the operator approval tools
  (`memory.list_pending_approvals` / `approve_candidate` / `reject_candidate`) are advertised
  whenever that transport exists, i.e. **even under `mcp-read`**. Advertisement is not capability:
  the `0600` approval token plus the configured approver set gate **execution**, not visibility —
  without them an approve call is refused, and the finalizer's inert-closed default (empty approver
  set) refuses every finalize until approvers are configured. Submitted writes are candidates-only
  by construction — the MCP surface can never produce a direct durable write.

### Serving self-check: refuse rather than serve blind

At startup the box runs a coherence self-check against its own store
(`panella/store_probe.py`). If the box's governance identity does not match its corpus — the
overlay forgotten, or a generic config pointed at an owned store — every read would die in a
tenant-isolation error and the box would silently serve nothing.

Instead, the self-check turns that silent dark-out into a **loud refusal**: the memory routes
return `503` (and the break-glass mint is refused) while `/v1/health` stays reachable so a
monitor sees a live-but-refusing process. A box that cannot prove it is serving its own corpus
coherently refuses to serve at all. A genuinely fresh box acknowledges emptiness explicitly
(`PANELLA_FRESH_BOX=1`).

### Fail-closed defaults everywhere

The governance model is default-deny throughout (see `docs/GOVERNANCE.md`): an unset/missing
overlay pointer crashes rather than serving generic, an unknown approval transport is a
load-time error, an empty approver set approves nothing, and a provenance mismatch is refused.
Tenant isolation is enforced at the client boundary and fails closed on any ambiguous
attribution.

## Scope and honest limits

Panella runs as a single-process service. Within that process, code holding the store
credentials is inside the trust boundary — Panella does not claim in-process code isolation or
cryptographic attestation between components. Its security boundary is the authenticated
approval plus the provenance gate, not process-level sandboxing. Process isolation and
attestation are tracked as future work, and the finalizer module documents this assumption
directly.
