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

Audit (invariant PR1): approval audit is owned by the SHARED service (decision B) — these routes
build an ``ApprovalAuditContext`` (audit DB + bearer principal + concrete tenant + source="http" +
request id) and never append approval events themselves, so the MCP and HTTP surfaces record
decisions/refusals/lists identically and the fail-closed pre-decision receipt is one code path.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from panella import approval_service
from panella.approval_audit import ApprovalAuditContext
from panella.approval_service import (
    ApprovalAuthError,
    ApprovalNotFinalized,
    ApprovalStateError,
)
from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.principal import Principal, default_tenant_id, root_principal

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


def _audit_ctx(request: Request, principal: Principal) -> ApprovalAuditContext:
    """The HTTP surface's audit sink for the shared approval service: the box's audit DB, the
    bearer principal, the deployment's CANONICAL tenant, source="http", and the request id for
    correlation. The tenant is ALWAYS ``default_tenant_id()`` — the approval queue is
    single-tenant and every governed payload is built for the canonical tenant, which is exactly
    what the finalizer's receipt gate verifies. Recording the bearer's own tenant instead (a root
    bearer carries ``"*"``; an owner bearer can be minted with any concrete scope) would stamp a
    receipt the gate can never accept — a valid approval stuck unfinalizable (GH bot P2)."""
    return ApprovalAuditContext(
        db_path=request.app.state.config.audit_db_path,
        principal=principal,
        tenant_accessed=default_tenant_id(),
        source="http",
        extra={"request_id": str(getattr(request.state, "request_id", ""))},
    )


def _require_owner(principal: Principal) -> None:
    """Approval routes are the OWNER's surface — like ``/mcp``. A merely-valid bearer is NOT enough:
    require the governance root principal, so a low-privilege / foreign-tenant bearer cannot borrow
    the approval surface even if the local_cli approval token leaks (defense in depth; mirrors the
    ``/mcp`` ``_authenticate_mcp_owner`` gate). The bearer is still routing-admission only — it never
    stamps ``approved_by`` — but it must be the owner to reach these routes at all."""
    if principal.id != root_principal().id:
        raise ApiError("forbidden", "approval routes require the owner (root) principal", 403)


@router.get("/v1/approvals/pending", response_model=PendingApprovalsResponse)
def list_pending_route(
    request: Request,
    principal: PrincipalDep,
    x_approval_token: ApprovalTokenHeader = None,
    limit: int = 20,
) -> PendingApprovalsResponse:
    _require_owner(principal)
    transport, governance = _resolve_transport(request)
    try:
        rows = approval_service.list_pending(
            request.app.state.config.outbox_db_path, transport, governance, x_approval_token,
            audit=_audit_ctx(request, principal), limit=limit,
        )
    except ApprovalAuthError as exc:
        # Uniform client-facing refusal — NO oracle distinguishing "bad token" from "valid token,
        # not an approver". The refusal (with its reason) is audited inside the shared service.
        raise ApiError("approval_refused", "approval refused", 403) from exc
    return PendingApprovalsResponse(pending=[PendingItem(**row) for row in rows])


@router.get("/v1/approvals/count", response_model=ApprovalCountResponse)
def count_route(request: Request, principal: PrincipalDep) -> ApprovalCountResponse:
    # Bearer-only (principal_dep requires a valid bearer): NO approval token, no content — only the
    # integer for a badge. Still gated on the box having an HTTP approval surface at all: a
    # non-local_cli box (e.g. telegram) 404s here too, so the whole /v1/approvals/ surface is
    # absent/consistent. Serving-gated by ServingGateMiddleware like the rest of /v1/approvals/.
    _require_owner(principal)
    _resolve_transport(request)
    return ApprovalCountResponse(pending_count=approval_service.count_pending(request.app.state.config.outbox_db_path))


@router.post("/v1/approvals/{approval_id}/approve", response_model=ApproveResponse)
def approve_route(
    request: Request,
    approval_id: int,
    principal: PrincipalDep,
    x_approval_token: ApprovalTokenHeader = None,
) -> ApproveResponse:
    _require_owner(principal)
    transport, governance = _resolve_transport(request)
    try:
        outcome = approval_service.approve(
            request.app.state.config.outbox_db_path,
            transport,
            governance,
            x_approval_token,
            approval_id,
            audit=_audit_ctx(request, principal),
            finalizer_adapter_factory=lambda: request.app.state.memory_adapter,
        )
    except ApprovalAuthError as exc:
        # Uniform client-facing refusal (no oracle); the refusal reason is audited in the service.
        raise ApiError("approval_refused", "approval refused", 403) from exc
    except ApprovalStateError as exc:
        # Caller passed the approver gate; the row just isn't approvable (missing/decided/claimed).
        # The pre-decision intent record (or refusal record) is already in the audit chain.
        raise ApiError("approval_refused", str(exc), 409) from exc
    except ApprovalNotFinalized as exc:
        # The row WAS stamped approved (a real queue state change) — the fail-closed pre-decision
        # record IS that state change's audit event; finalize did not complete (retriable).
        raise ApiError("not_finalized", str(exc), 503) from exc
    except Exception as exc:  # noqa: BLE001 — surface as 500, never leak internals
        raise ApiError("approval_failed", "approval failed", 500) from exc
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
    _require_owner(principal)
    transport, governance = _resolve_transport(request)
    try:
        approval_service.reject(
            request.app.state.config.outbox_db_path, transport, governance, x_approval_token, approval_id,
            audit=_audit_ctx(request, principal),
        )
    except ApprovalAuthError as exc:
        # Uniform client-facing refusal (no oracle); the refusal reason is audited in the service.
        raise ApiError("approval_refused", "approval refused", 403) from exc
    except ApprovalStateError as exc:
        raise ApiError("approval_refused", str(exc), 409) from exc
    return RejectResponse(rejected=True, approval_id=approval_id)
