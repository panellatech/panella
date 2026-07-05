"""HTTP approval routes (WP-B2a) — the operator's network face of the approval queue.

Trust chain (identical to the MCP surface — both go through ``panella.approval_service``):

- The HTTP **bearer** is routing-admission ONLY (``AuthMiddleware``); it NEVER stamps ``approved_by``.
- Every route that returns candidate content or changes state ALSO requires a raw ``local_cli``
  approval token, carried in the ``X-Approval-Token`` header (never query/path — a token in the URL
  would leak into access logs). The token is verified through the configured transport →
  canonical ``approved_by`` → authorized-approver check → the shared finalizer chain.
- The single bearer-only route is ``GET /v1/approvals/count``: a bare integer (zero ids, zero
  content) for a console badge.

The router is always registered, but every route first resolves the deployment's approval transport
via ``build_transport_if_approvable(current_governance())`` — the SAME single source the MCP surface
uses. On a telegram/foreign box that returns None and the route answers 404 (no HTTP approval
surface). ``ServingGateMiddleware`` also covers ``/v1/approvals/*``, so an incoherent box refuses
(503) rather than finalizing a durable write blind. Resolving per-request (not at mount) keeps
governance-load timing unchanged for every other box and lets a runtime approver/overlay change take
effect without a restart.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from panella import approval_service
from panella.approval_service import (
    ApprovalAuthError,
    ApprovalNotFinalized,
    ApprovalStateError,
)
from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http
from panella.principal import Principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]
ApprovalTokenHeader = Annotated[str | None, Header(alias="X-Approval-Token")]


class PendingItem(BaseModel):
    approval_id: int
    wing: str | None = None
    room: str | None = None
    memory_type: str | None = None
    created_at: str | None = None
    content_preview: str = ""


class PendingApprovalsResponse(BaseModel):
    pending: list[PendingItem]


class ApprovalCountResponse(BaseModel):
    pending_count: int


class ApproveResponse(BaseModel):
    approved: bool
    finalized: bool
    durable_id: str | None
    retried: bool


class RejectResponse(BaseModel):
    rejected: bool
    approval_id: int


def _resolve_transport(request: Request):
    """Resolve the deployment's approval transport + governance per request from the SAME single
    source the MCP surface uses (``build_transport_if_approvable(current_governance())``). A
    non-``local_cli``-approvable box (e.g. telegram) has no HTTP approval surface → 404. Imported
    lazily so a box that never hits ``/v1/approvals`` pays nothing."""
    from panella.governance import current_governance
    from panella.mcp_tools import build_transport_if_approvable

    governance = current_governance()
    transport = build_transport_if_approvable(governance)
    if transport is None:
        raise ApiError(
            "approvals_unavailable",
            "approval routes are not available on this deployment (approval transport is not local_cli-approvable)",
            404,
        )
    return transport, governance


@router.get("/v1/approvals/pending", response_model=PendingApprovalsResponse)
def list_pending_route(
    request: Request,
    principal: PrincipalDep,
    x_approval_token: ApprovalTokenHeader = None,
    limit: int = 20,
) -> PendingApprovalsResponse:
    transport, governance = _resolve_transport(request)
    try:
        rows = approval_service.list_pending(
            request.app.state.config.outbox_db_path, transport, governance, x_approval_token, limit=limit
        )
    except ApprovalAuthError as exc:
        # Uniform client-facing refusal — NO oracle distinguishing "bad token" from "valid token,
        # not an approver". The specific reason is kept for the server audit log only.
        audit_http(request, principal, op="approvals_list", tenant_accessed=principal.tenant_id,
                   details={"error": "approval_refused", "reason": str(exc)})
        raise ApiError("approval_refused", "approval refused", 403) from exc
    audit_http(request, principal, op="approvals_list", tenant_accessed=principal.tenant_id,
               details={"listed": len(rows)})
    return PendingApprovalsResponse(pending=[PendingItem(**row) for row in rows])


@router.get("/v1/approvals/count", response_model=ApprovalCountResponse)
def count_route(request: Request, principal: PrincipalDep) -> ApprovalCountResponse:
    # Bearer-only (principal_dep requires a valid bearer): NO approval token, no content — only the
    # integer for a badge. Still gated on the box having an HTTP approval surface at all: a
    # non-local_cli box (e.g. telegram) 404s here too, so the whole /v1/approvals/ surface is
    # absent/consistent. Serving-gated by ServingGateMiddleware like the rest of /v1/approvals/.
    _resolve_transport(request)
    _ = principal
    return ApprovalCountResponse(pending_count=approval_service.count_pending(request.app.state.config.outbox_db_path))


@router.post("/v1/approvals/{approval_id}/approve", response_model=ApproveResponse)
def approve_route(
    request: Request,
    approval_id: int,
    principal: PrincipalDep,
    x_approval_token: ApprovalTokenHeader = None,
) -> ApproveResponse:
    transport, governance = _resolve_transport(request)
    try:
        outcome = approval_service.approve(
            request.app.state.config.outbox_db_path,
            transport,
            governance,
            x_approval_token,
            approval_id,
            finalizer_adapter_factory=lambda: request.app.state.memory_adapter,
        )
    except ApprovalAuthError as exc:
        # Uniform client-facing refusal (no oracle); specific reason → server audit only.
        audit_http(request, principal, op="approvals_approve", tenant_accessed=principal.tenant_id,
                   details={"error": "approval_refused", "reason": str(exc), "approval_id": approval_id})
        raise ApiError("approval_refused", "approval refused", 403) from exc
    except ApprovalStateError as exc:
        # Caller passed the approver gate; the row just isn't approvable (missing/decided/claimed).
        audit_http(request, principal, op="approvals_approve", tenant_accessed=principal.tenant_id,
                   details={"error": "state_refused", "reason": str(exc), "approval_id": approval_id})
        raise ApiError("approval_refused", str(exc), 409) from exc
    except ApprovalNotFinalized as exc:
        # The row WAS stamped approved (a real queue state change) but finalize did not complete —
        # audit it: a security-relevant state change must never happen with no HTTP audit event.
        audit_http(request, principal, op="approvals_approve", tenant_accessed=principal.tenant_id,
                   details={"error": "not_finalized", "state_changed": True, "approval_id": approval_id})
        raise ApiError("not_finalized", str(exc), 503) from exc
    except Exception as exc:  # noqa: BLE001 — surface as 500, never leak internals
        audit_http(request, principal, op="approvals_approve", tenant_accessed=principal.tenant_id,
                   details={"error": "approval_failed", "approval_id": approval_id})
        raise ApiError("approval_failed", "approval failed", 500) from exc
    audit_http(request, principal, op="approvals_approve", tenant_accessed=principal.tenant_id,
               target_id=outcome.durable_id,
               details={"finalized": outcome.finalized, "retried": outcome.retried, "approval_id": approval_id})
    return ApproveResponse(
        approved=outcome.approved, finalized=outcome.finalized,
        durable_id=outcome.durable_id, retried=outcome.retried,
    )


@router.post("/v1/approvals/{approval_id}/reject", response_model=RejectResponse)
def reject_route(
    request: Request,
    approval_id: int,
    principal: PrincipalDep,
    x_approval_token: ApprovalTokenHeader = None,
) -> RejectResponse:
    transport, governance = _resolve_transport(request)
    try:
        approval_service.reject(
            request.app.state.config.outbox_db_path, transport, governance, x_approval_token, approval_id
        )
    except ApprovalAuthError as exc:
        # Uniform client-facing refusal (no oracle); specific reason → server audit only.
        audit_http(request, principal, op="approvals_reject", tenant_accessed=principal.tenant_id,
                   details={"error": "approval_refused", "reason": str(exc), "approval_id": approval_id})
        raise ApiError("approval_refused", "approval refused", 403) from exc
    except ApprovalStateError as exc:
        audit_http(request, principal, op="approvals_reject", tenant_accessed=principal.tenant_id,
                   details={"error": "state_refused", "reason": str(exc), "approval_id": approval_id})
        raise ApiError("approval_refused", str(exc), 409) from exc
    audit_http(request, principal, op="approvals_reject", tenant_accessed=principal.tenant_id,
               details={"rejected": True, "approval_id": approval_id})
    return RejectResponse(rejected=True, approval_id=approval_id)
