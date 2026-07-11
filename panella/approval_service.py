"""Shared approval-queue trust chain — the ONE gate both the MCP tool surface
(``panella.mcp_tools``) and the HTTP ``/v1/approvals`` routes (``panella.http.routes.approvals``)
call, so the two surfaces cannot drift on how an approval is authenticated, authorized, audited,
and finalized (WP-B2a; audit-invariant PR1).

Security invariant (identical for MCP and HTTP): the presenter's identity as an approver is
derived ONLY from the deployment's CONFIGURED transport — ``transport.verify_presser(raw_credential)``
→ canonical ``approved_by`` — which must then be in ``governance.approval.authorized_approvers``.
No caller-supplied ``approved_by``/``approved_via`` is ever trusted; the HTTP bearer (or the MCP
session) is routing-admission ONLY. The raw queue helpers in ``client_raw`` have NO auth of their
own — this module is the only sanctioned path to them, so a surface that skipped it would be an
un-gated mutation. ``verify_approver`` is that single gate; ``list_pending``/``approve``/``reject``
all call it before any content read or state change.

Audit invariant (PR1): this module OWNS all service-level approval audit (decision B) — the
surfaces carry an ``ApprovalAuditContext`` (REQUIRED keyword) and never append approval events
themselves. ``approve``/``reject`` append a FAIL-CLOSED pre-decision record
(``op="approval_decision"``, phase ``authorized_intent``, with the candidate fingerprint on
approve) to the hash-chained audit log BEFORE any queue mutation — an append failure aborts the
call, so no approval state ever changes without a committed record. The returned (seq, this_hash)
receipt is stored atomically with the approved-status flip, and the finalizer independently
re-verifies it (bounded chain walk + semantics + fingerprint) before any durable write.
``list_pending`` appends a fail-closed ``approval_list`` record — candidate content never egresses
unrecorded. Refused credentials append a best-effort ``approval_refused`` record.

This module imports only ``panella.*`` and the standard library (governance-layer extractability);
it holds no HTTP/MCP payload shapes — each surface adapts the results/exceptions to its own envelope.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panella.approval_audit import ApprovalAuditContext
from panella.approval_finalizer import finalize_approved_candidate
from panella.approval_transport import ApprovalTransport
from panella.audit import audit_row_hash, audit_write
from panella.client_raw import (
    candidate_fingerprint,
    count_pending_approvals,
    get_approval_candidate_json,
    list_pending_approvals,
    mcp_approve_or_redrive,
    proposed_by_profile,
    update_approval_status,
)
from panella.governance import Governance

logger = logging.getLogger(__name__)


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
    the row ``finalizer_state='failed'``, or its audit receipt failed the finalizer's gate).
    Recoverable for transient causes: re-invoking approve re-enters the redrive path and re-runs
    finalize; a gate refusal persists until the underlying receipt/audit-chain problem is fixed."""


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


def _details(audit: ApprovalAuditContext, core: dict[str, Any]) -> dict[str, Any]:
    """Audit ``details`` = surface extras (e.g. ``request_id``) + the surface name + the core
    fields. Core keys are applied LAST so an extra can never shadow a semantic field."""
    merged: dict[str, Any] = dict(audit.extra or {})
    merged["source"] = audit.source
    merged.update(core)
    return merged


def _append_decision(
    audit: ApprovalAuditContext,
    *,
    decision: str,
    approval_id: int,
    approved_by: str,
    approved_via: str,
    candidate_sha256: str | None = None,
    proposer: str | None = None,
) -> tuple[int, str]:
    """FAIL-CLOSED pre-decision append (``op="approval_decision"``, phase ``authorized_intent``):
    returns the (seq, this_hash) RECEIPT. Any append/fetch failure PROPAGATES — the caller must
    not mutate the queue without this committed record. ``proposer`` is a convenience PROJECTION
    of the server-stamped proposing profile (the fingerprint already binds the candidate bytes it
    came from; the finalizer gate does NOT check it — PR2 decision, keep the gate exactly PR1)."""
    core: dict[str, Any] = {
        "phase": "authorized_intent",
        "decision": decision,
        "approval_id": approval_id,
        "approved_by": approved_by,
        "approved_via": approved_via,
    }
    if candidate_sha256 is not None:
        core["candidate_sha256"] = candidate_sha256
    if proposer is not None:
        core["proposed_by_profile"] = proposer
    seq = audit_write(
        principal=audit.principal,
        tenant_accessed=audit.tenant_accessed,
        op="approval_decision",
        target_id=str(approval_id),
        reason_code=(audit.principal.break_glass_token.reason if audit.principal.break_glass_token else None),
        details=_details(audit, core),
        db_path=audit.db_path,
    )
    return seq, audit_row_hash(seq, db_path=audit.db_path)


def _audit_refused(audit: ApprovalAuditContext, *, action: str, approval_id: int | None, reason: str) -> None:
    """Best-effort refusal record (``op="approval_refused"``). The refusal itself must surface to
    the caller even when the audit DB is unavailable, so append failures are logged, never raised."""
    try:
        audit_write(
            principal=audit.principal,
            tenant_accessed=audit.tenant_accessed,
            op="approval_refused",
            target_id=str(approval_id) if approval_id is not None else None,
            details=_details(audit, {"action": action, "reason": reason}),
            db_path=audit.db_path,
        )
    except Exception:  # noqa: BLE001 — never mask the refusal with an audit outage
        logger.warning("approval refusal audit append failed (action=%s)", action, exc_info=True)


def list_pending(
    outbox_db_path: str | Path,
    transport: ApprovalTransport,
    governance: Governance,
    credential: Any,
    *,
    audit: ApprovalAuditContext,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Authorized-approver-gated read of the pending queue (returns candidate content previews, so
    it requires the SAME authorization as approve/reject — never merely a token holder). The read
    is recorded FAIL-CLOSED (``op="approval_list"``) before any content is returned: if the access
    cannot be audited, the content does not egress."""
    try:
        canonical = verify_approver(transport, governance, credential)
    except ApprovalAuthError as exc:
        _audit_refused(audit, action="list_pending", approval_id=None, reason=str(exc))
        raise
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        limit = 20
    rows = list_pending_approvals(outbox_db_path, limit=limit)
    audit_write(
        principal=audit.principal,
        tenant_accessed=audit.tenant_accessed,
        op="approval_list",
        details=_details(audit, {"listed": len(rows), "approved_by": canonical}),
        db_path=audit.db_path,
    )
    return rows


def count_pending(outbox_db_path: str | Path) -> int:
    """Bare pending count — NO credential (the caller gates this bearer-only for a console badge).
    Returns only the integer: zero ids, zero content (hence no audit record either)."""
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
    audit: ApprovalAuditContext,
    finalizer_adapter_factory: Any | None = None,
) -> ApproveOutcome:
    """Approve a pending candidate and finalize it durably — two-phase audited.

    Derives ``approved_via``/``approved_by`` ONLY from the verified transport (never from the
    caller). PHASE 1 (fail-closed): append the pre-decision record binding the candidate's exact
    bytes (``candidate_fingerprint``) → the (seq, hash) RECEIPT; an append failure aborts with no
    state change. The receipt is stored atomically with the approved-status flip
    (``mcp_approve_or_redrive`` → ``_approve_in_conn``, one BEGIN IMMEDIATE txn, with an in-txn
    fingerprint recheck). PHASE 2: ``finalize_approved_candidate`` independently re-verifies the
    STORED receipt (bounded chain walk + semantics + fingerprint) before the durable write; a
    redrive keeps the row's original receipt (this call's append stands as the retry's intent
    record). Raises ``ApprovalAuthError`` (unauthorized), ``ApprovalStateError``
    (missing/decided/claimed), or ``ApprovalNotFinalized`` (stamped but not durable)."""
    try:
        canonical = verify_approver(transport, governance, credential)
    except ApprovalAuthError as exc:
        _audit_refused(audit, action="approve", approval_id=approval_id, reason=str(exc))
        raise
    via = transport.stamp_provenance()
    candidate_json = get_approval_candidate_json(outbox_db_path, approval_id)
    if candidate_json is None:
        _audit_refused(audit, action="approve", approval_id=approval_id, reason="approval row not found")
        raise ApprovalStateError(f"approval_queue row not found: {approval_id}")
    fingerprint = candidate_fingerprint(candidate_json)
    try:
        proposer = proposed_by_profile(json.loads(candidate_json))
    except (ValueError, TypeError):
        proposer = None  # malformed candidate text — the stamp txn will refuse it downstream
    receipt_seq, receipt_hash = _append_decision(
        audit,
        decision="approve",
        approval_id=approval_id,
        approved_by=canonical,
        approved_via=via,
        candidate_sha256=fingerprint,
        proposer=proposer,
    )
    approvers = set(governance.approval.authorized_approvers)
    finalize_kwargs: dict[str, Any] = {}
    if finalizer_adapter_factory is not None:
        finalize_kwargs["adapter_factory"] = finalizer_adapter_factory
    try:
        mode = mcp_approve_or_redrive(
            outbox_db_path,
            approval_id,
            approved_via=via,
            approved_by=canonical,
            audit_receipt_seq=receipt_seq,
            audit_receipt_hash=receipt_hash,
            candidate_sha256=fingerprint,
        )
    except ValueError as exc:
        # Missing row / not an awaiting-or-retriable candidate (decided/foreign/claimed) / altered
        # candidate bytes. The pre-decision append above stands in the chain as the attempt record.
        raise ApprovalStateError(str(exc)) from exc
    durable_id = finalize_approved_candidate(
        approval_id,
        authorized_approvers=approvers,
        expected_approved_via=via,
        db_path=outbox_db_path,
        audit_db_path=audit.db_path,
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
    *,
    audit: ApprovalAuditContext,
) -> None:
    """Reject a pending candidate (aligned with MCP ``TOOL_REJECT``: same non-terminal guard, same
    ``decided_by=canonical`` stamp). The rejection is PRE-recorded fail-closed (a decision append
    that fails aborts the call — no status change without a committed record). Raises
    ``ApprovalAuthError`` (unauthorized) or ``ApprovalStateError`` when no pending row changed
    (missing or already decided) — so a caller never reports a rejection that did not happen."""
    try:
        canonical = verify_approver(transport, governance, credential)
    except ApprovalAuthError as exc:
        _audit_refused(audit, action="reject", approval_id=approval_id, reason=str(exc))
        raise
    via = transport.stamp_provenance()
    _append_decision(
        audit,
        decision="reject",
        approval_id=approval_id,
        approved_by=canonical,
        approved_via=via,
    )
    changed = update_approval_status(outbox_db_path, approval_id, "rejected", decided_by=canonical)
    if not changed:
        raise ApprovalStateError(
            f"approval {approval_id} is not a pending candidate (missing or already decided)"
        )
