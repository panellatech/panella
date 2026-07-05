<!-- positioning: maintainers' final draft pending -->

# Governance Model

This document describes how Panella's governance works, drawn from the code that enforces
it. It is written for an operator running a box, not for a reader deciding whether to try
Panella.

The single through-line: **default-deny, fail-closed**. Every gate below starts in the most
restrictive posture and opens only when a deployment explicitly, correctly configures it. A
misconfiguration is a loud refusal, never a silent open door.

## Where governance lives

Governance is a config, not code. The generic base ships in `config/governance.yaml`. A
deployment layers its own identity, approval, and path values on top via an **overlay** file
pointed to by the `PANELLA_GOVERNANCE_OVERLAY` environment variable
(`panella/governance.py`). The overlay is deep-merged over the base — overlay wins per key,
nested mappings merge, scalars and lists are replaced wholesale.

The overlay lives *outside* the repo so a code deploy never overwrites it and a fresh
worktree or `git clean` never erases it. The loader is fail-loud by construction: if
`PANELLA_GOVERNANCE_OVERLAY` is *set* but points at a missing file, load raises
`GovernanceConfigError` rather than silently falling back to the generic base — a wrong
deploy ordering (code shipped before its overlay) is a crash, not a quiet generic-identity
serve. A section that is present but malformed is also a load-time error; only a section that
is *entirely absent* degrades to the shipped generic default.

The merged config is parsed and cached once per process (`current_governance()`). All the
de-identified call sites — principal defaults, approval payload identity, the finalizer gate,
the HTTP self-check — read the deployment's identity through this one cached entry point.

## Identity model

The `identity` section (`IdentityConfig`) defines who the box belongs to:

- **Root principal** (`identity.root_principal`) — the root operator identity
  (`root_principal()` in `panella/principal.py`). It carries `id` (default `human:owner`),
  `subject_id` (default `u_owner`), and `roles`. The root principal runs with tenant `*`
  (cross-tenant scope) and the `*` scope; it is the only principal that may cross tenant
  boundaries, and only with an active break-glass token.
- **Default tenant** (`identity.default_tenant_id`, default `t_owner_personal`) — the tenant
  new writes and default principals are attributed to. A tenant id must carry the configured
  `tenant_id_prefix` (default `t_`); the deployment's own default tenant is admitted verbatim
  even if it predates the prefix convention.
- **Owner wing** (`identity.owner_wing`, default `owner`) plus `content_owner_label` and
  `owner_slug` — these template the *durable identity* of approved writes (the content prefix,
  the memory-type / source-system prefix, the target wing). A deployment that reproduces its
  historical bytes keeps its corpus fork-free across the de-identification seam.

Agent principals (`principal_default_for_profile()`) are non-root: they are pinned to a single
concrete tenant from their profile's `tenant_scope`, never `*`, and carry only
`memory.read` / `memory.write` scopes.

## Approval model

Durable writes that a profile marks as approval-required do not land directly. They enter an
**approval queue** as candidates (`client.py` `_enqueue_approval`). A candidate is inert text
in a local SQLite queue with no durable store row behind it until an authorized approver
approves it and the finalizer runs.

The `approval` section (`ApprovalConfig`) has two load-bearing fields:

- **`authorized_approvers`** — the set of canonical approver identities whose approvals the
  system trusts. **This defaults to empty, and empty means inert-closed.** An empty approver
  set is the finalizer keystone: nothing durable ever finalizes, no queue content is ever
  exposed over the MCP surface, nothing is ever approved. A freshly installed box approves
  nothing until its operator deliberately configures an approver. This is the safe default —
  a box you forgot to finish configuring cannot silently accept writes.
- **`transport`** — the approval channel (`kind` + `config`), described next.

## Approval transports

An approval transport (`panella/approval_transport.py`) is the seam that maps a *raw presser
credential* to a *canonical approver identity*, and stamps the provenance string an authorized
approval carries. Two kinds exist:

- **`local_cli`** — the self-host transport. The presser presents the contents of an
  operator-held token file (created at provisioning, mode `0600`). A missing, empty, or
  loose-permission token file fails closed: every verification returns `None` until the box is
  provisioned correctly. An authorized presser resolves to `local_cli:owner`.
- **`telegram`** — a bot-callback transport for deployments that approve through a Telegram
  bot. Its binding equivalent is a bot-sent message id, which the network MCP surface has no
  analogue for, so telegram boxes approve through the bot rather than over MCP.

The transport vocabulary is a **closed, fail-closed set** — `KNOWN_TRANSPORT_KINDS`. The
governance loader rejects an empty or unknown `transport.kind` at *load time* with
`GovernanceConfigError`. A typo can never reach the finalizer gate as a silently-inert
transport that refuses every approval with no hint at the cause.

## The finalizer provenance gate

Approval alone does not write. The **finalizer** (`panella/approval_finalizer.py`,
`finalize_approved_candidate`) is the only consumer that turns an approved candidate into a
durable store row, and it trusts a candidate *only* when all of these hold:

1. The queue row's status is `approved` and it has a linked `memory_event_id`.
2. **`approved_via` equals the deployment's configured transport name**
   (`governance approval.transport.kind`). A row stamped by a channel the box does not run —
   for example a stale `telegram` stamp on a `local_cli` box — is refused and marked failed.
3. **`approved_by` is in the configured `authorized_approvers` set.** With the default empty
   set, this can never be satisfied, so the inert-closed default holds end-to-end.

A raw enqueue, a hand-edited `status='approved'` row, or an approval whose provenance does not
match the configured transport is *not finalizable*. If no approvers are configured, the
finalizer surfaces the misconfiguration and skips — it does not mark rows failed (a
misconfiguration is not a forged row).

The durable payload the finalizer writes is rebuilt from an explicit allowlist of canonical
metadata keys, not copied from the candidate. Candidate-controlled provenance (author,
source, session) and reserved adapter fields cannot survive into the durable write. The
security boundary here is the authenticated approval plus this provenance gate — not
in-process code isolation, which a single-process Python daemon cannot enforce (documented
honestly in the finalizer module).

## Fail-closed, in one place

Every governance decision defaults to refuse:

- Overlay set-but-missing → crash, not silent generic serve.
- Unknown / empty transport kind → load-time error.
- Empty approver set → nothing approves, nothing finalizes, no queue content exposed.
- Provenance mismatch → refused and marked failed.
- Store/identity incoherence → the box refuses to serve (see `SECURITY.md` on the serving
  self-check).

An operator's job is to open exactly the gates they mean to open. Everything they leave alone
stays shut.
