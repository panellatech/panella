"""Shared approval-queue trust chain — the ONE gate both the MCP tool surface
(``panella.mcp_tools``) and the HTTP ``/v1/approvals`` routes (``panella.http.routes.approvals``)
call, so the two surfaces cannot drift on how an approval is authenticated, authorized, and
finalized (WP-B2a).

Security invariant (identical for MCP and HTTP): the presenter's identity as an approver is
derived ONLY from the deployment's CONFIGURED transport — ``transport.verify_presser(raw_credential)``
→ canonical ``approved_by`` — which must then be in ``governance.approval.authorized_approvers``.
No caller-supplied ``approved_by``/``approved_via`` is ever trusted; the HTTP bearer (or the MCP
session) is routing-admission ONLY. The raw queue helpers in ``client_raw`` have NO auth of their
own — this module is the only sanctioned path to them, so a surface that skipped it would be an
un-gated mutation. ``verify_approver`` is that single gate; ``list_pending``/``approve``/``reject``
all call it before any content read or state change.

This module imports only ``panella.*`` and the standard library (governance-layer extractability);
it holds no HTTP/MCP payload shapes — each surface adapts the results/exceptions to its own envelope.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panella.approval_finalizer import finalize_approved_candidate
from panella.approval_transport import ApprovalTransport
from panella.client_raw import (
    count_pending_approvals,
    list_pending_approvals,
    mcp_approve_or_redrive,
    update_approval_status,
)
from panella.governance import Governance


class ApprovalAuthError(Exception):
    """The presented credential is missing/invalid, OR the canonical presser is not an authorized
    approver (fail-closed). Both surfaces map this to a single "refused" response and MUST NOT
    distinguish the two cases to the caller — no oracle that tells "bad token" from "valid token,
    not an approver". The message is kept for the SERVER audit log, not for a leaky client reply."""


class ApprovalStateError(Exception):
    """The target row is missing / already decided / claimed / not an awaiting-or-retriable
    candidate — a client condition (mapped to a refusal), not a server failure."""


class ApprovalNotFinalized(Exception):
    """Stamped or re-driven, but finalize did not complete (e.g. a transient store outage marked
    the row ``finalizer_state='failed'``). Recoverable: re-invoking approve re-enters the redrive
    path and re-runs finalize (the only recovery on a self-host box with no background sweep)."""


def verify_approver(transport: ApprovalTransport, governance: Governance, credential: Any) -> str:
    """Verify a presented raw credential through the CONFIGURED transport AND require the canonical
    presser to be an authorized approver. Returns the canonical ``approved_by`` or raises
    ``ApprovalAuthError`` (fail-closed). This is the SINGLE authorization gate — every content read
    or state change goes through it. An empty ``authorized_approvers`` set (the inert-closed
    default) rejects everyone, so nothing is ever approvable on an unprovisioned box."""
    if not isinstance(credential, str) or not credential:
        raise ApprovalAuthError("approval credential rejected")
    canonical = transport.verify_presser(credential)
    if canonical is None:
        raise ApprovalAuthError("approval credential rejected")
    if canonical not in set(governance.approval.authorized_approvers):
        raise ApprovalAuthError("presser is not an authorized approver")
    return canonical


def list_pending(
    outbox_db_path: str | Path,
    transport: ApprovalTransport,
    governance: Governance,
    credential: Any,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Authorized-approver-gated read of the pending queue (returns candidate content previews, so
    it requires the SAME authorization as approve/reject — never merely a token holder)."""
    verify_approver(transport, governance, credential)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        limit = 20
    return list_pending_approvals(outbox_db_path, limit=limit)


def count_pending(outbox_db_path: str | Path) -> int:
    """Bare pending count — NO credential (the caller gates this bearer-only for a console badge).
    Returns only the integer: zero ids, zero content."""
    return count_pending_approvals(outbox_db_path)


@dataclass(frozen=True)
class ApproveOutcome:
    approved: bool
    finalized: bool
    durable_id: str | None
    retried: bool


def approve(
    outbox_db_path: str | Path,
    transport: ApprovalTransport,
    governance: Governance,
    credential: Any,
    approval_id: int,
    *,
    finalizer_adapter_factory: Any | None = None,
) -> ApproveOutcome:
    """Approve a pending candidate and finalize it durably. Derives ``approved_via``/``approved_by``
    ONLY from the verified transport (never from the caller), stamps a fresh candidate OR authorizes
    a redrive of a stuck one via ``mcp_approve_or_redrive``, then runs ``finalize_approved_candidate``
    under the SAME provenance the finalizer independently re-verifies. Raises ``ApprovalAuthError``
    (unauthorized), ``ApprovalStateError`` (missing/decided/claimed), or ``ApprovalNotFinalized``
    (stamped but not durable — retriable)."""
    canonical = verify_approver(transport, governance, credential)
    via = transport.stamp_provenance()
    approvers = set(governance.approval.authorized_approvers)
    finalize_kwargs: dict[str, Any] = {}
    if finalizer_adapter_factory is not None:
        finalize_kwargs["adapter_factory"] = finalizer_adapter_factory
    try:
        mode = mcp_approve_or_redrive(outbox_db_path, approval_id, approved_via=via, approved_by=canonical)
    except ValueError as exc:
        # Missing row / not an awaiting-or-retriable candidate (decided/foreign/claimed).
        raise ApprovalStateError(str(exc)) from exc
    durable_id = finalize_approved_candidate(
        approval_id,
        authorized_approvers=approvers,
        expected_approved_via=via,
        db_path=outbox_db_path,
        **finalize_kwargs,
    )
    if durable_id is None:
        raise ApprovalNotFinalized(
            f"approval {approval_id} is approved but not yet durable (finalize did not complete — "
            "likely a transient store issue); retry to redrive it"
        )
    return ApproveOutcome(approved=True, finalized=True, durable_id=durable_id, retried=(mode == "redrive"))


def reject(
    outbox_db_path: str | Path,
    transport: ApprovalTransport,
    governance: Governance,
    credential: Any,
    approval_id: int,
) -> None:
    """Reject a pending candidate (aligned with MCP ``TOOL_REJECT``: same non-terminal guard, same
    ``decided_by=canonical`` stamp). Raises ``ApprovalAuthError`` (unauthorized) or
    ``ApprovalStateError`` when no pending row changed (missing or already decided) — so a caller
    never reports a rejection that did not happen."""
    canonical = verify_approver(transport, governance, credential)
    changed = update_approval_status(outbox_db_path, approval_id, "rejected", decided_by=canonical)
    if not changed:
        raise ApprovalStateError(
            f"approval {approval_id} is not a pending candidate (missing or already decided)"
        )
