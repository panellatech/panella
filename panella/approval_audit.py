"""Approval-audit context — the leaf that lets the shared approval trust chain append to the
hash-chained audit log WITHOUT importing any HTTP/MCP surface type (and without a cycle back through
``approval_service`` → ``approval_finalizer``).

Why a dedicated leaf module: ``approval_service`` and ``approval_finalizer`` both need to name this
context, and ``approval_service`` imports ``approval_finalizer``. Defining the context in either of
those would risk an import cycle once the finalizer also references it. This module imports only
``panella.principal`` (+ stdlib), so both can depend on it freely.

The context carries exactly what ``panella.audit.audit_write`` needs that the shared trust chain does
NOT already derive from the transport (``approved_by``/``approved_via`` come from the verified
credential, not from here):

- ``db_path``      — the deployment's configured audit DB (HTTP and MCP share ONE chain because both
                     are built from the same ``http_config.audit_db_path``).
- ``principal``    — the surface/executing principal recorded as ``principal_id`` (HTTP bearer; for
                     MCP the root bearer identity, since ``/mcp`` is owner-only and the dispatcher
                     discards the record after admission). NOT the approver — the approver
                     (``approved_by``) is a separate audit detail (decision A).
- ``tenant_accessed`` — the CONCRETE deployment/candidate tenant, never a root principal's ``"*"``.
- ``source``       — ``"http"`` | ``"mcp"`` — the transport, stamped into audit details.
- ``extra``        — transport-specific detail (e.g. ``{"request_id": ...}`` for HTTP); merged into
                     the audit ``details`` under the surface's control, never trusted for identity.

The context is a REQUIRED argument to the shared ``approve``/``reject``/``list_pending`` (no default),
so no transport — present or future — can silently skip the audit: the type system forces every caller
to supply it. On the MCP surface it rides ``McpToolContext.approval_audit`` (optional there ONLY so the
bare search-only server, which registers no approval tools, still constructs).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panella.principal import Principal


@dataclass(frozen=True)
class ApprovalAuditContext:
    """Immutable audit-sink context for a single approval-surface call. See module docstring."""

    db_path: str | Path
    principal: Principal
    tenant_accessed: str
    source: str
    extra: dict[str, Any] | None = None
