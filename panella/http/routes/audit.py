"""Audit route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, audit_rows
from panella.http.schemas import AuditEntry, AuditResponse
from panella.principal import Principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]


@router.get("/v1/memory/audit", response_model=AuditResponse)
def list_audit(
    request: Request,
    principal: PrincipalDep,
    tenant: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> AuditResponse:
    requested_tenant = tenant or principal.tenant_id
    if requested_tenant != principal.tenant_id and not principal.is_root_with_break_glass():
        raise ApiError("break_glass_required", "cross-tenant audit requires active break-glass", 403)

    if principal.is_root_with_break_glass() and requested_tenant == "*":
        rows = audit_rows(request.app.state.config.audit_db_path, limit=limit)
    else:
        rows = audit_rows(
            request.app.state.config.audit_db_path,
            where="WHERE tenant_accessed = ? OR principal_id = ?",
            params=(requested_tenant, principal.id),
            limit=limit,
        )
    audit_http(request, principal, op="audit_list", tenant_accessed=requested_tenant, details={"limit": limit})
    return AuditResponse(entries=[AuditEntry(**row) for row in rows])
