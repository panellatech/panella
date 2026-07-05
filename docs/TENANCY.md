<!-- positioning: maintainers' final draft pending -->

# Tenancy (Draft)

> **Draft / evolving contract, subject to change before v1.** This page sketches the tenancy
> port as it exists in the code today. A formal multi-tenant conformance contract is
> forthcoming; do not treat this as a stable spec.

## The idea

Panella scopes memory by **tenant**. A principal belongs to a tenant, and it may read and
mutate rows only within the tenant(s) its scope allows. The default deployment is
single-tenant (one owner, one default tenant); the tenancy port is the seam that keeps a box
honest if and when more than one tenant shares a store.

## Where isolation is enforced today

Tenant isolation is enforced at the **client / query layer** — see `panella/client.py`:

- Each operation resolves the caller's allowed tenant ids (`_tenant_ids`). A non-root
  principal is pinned to its own concrete tenant; the wildcard `*` scope is reserved for the
  root principal with an active break-glass token.
- On the read path, `_filter_hits` drops any hit whose tenant is not in scope. A hit that
  claims an out-of-scope tenant is not silently filtered — it raises **`TenantIsolationError`**
  (`panella/client.py`), failing closed rather than leaking a foreign-tenant row. A row with
  no tenant attribution is blocked for a non-root caller.
- On mutation paths (tombstone / supersede / hard-delete), `_require_mutation_target_owned`
  verifies the target row's tenant before acting, closing the IDOR where a guessable id could
  let one tenant mutate another's row.
- A profile declares the tenants it may touch via `tenant_scope`
  (`AgentProfile.allows_tenant`), validated against the governance-configured tenant prefix.

The intent of the port: **a caller can only ever see or change rows in a tenant it owns**, and
any ambiguity resolves to a refusal.

## What is not settled yet

This is a stub, not a specification. Still open, and expected to change before v1:

- A formal **multi-tenant conformance contract** — the exact set of invariants a store adapter
  and query layer must satisfy to be called tenant-isolating, plus a conformance test vector
  any implementation can run.
- The attribution precedence rules and their edge cases (metadata vs. tag vs. fallback) as a
  written, testable contract rather than code-defined behavior.
- Cross-tenant administrative operations (the root / break-glass path) as an explicit,
  audited contract.

Until that contract lands, treat single-tenant self-host as the supported shape and read the
code as the source of truth for isolation behavior.
