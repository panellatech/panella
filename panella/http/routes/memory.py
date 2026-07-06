"""Single-memory read route."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from panella.client import TenantIsolationError
from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, memory_client
from panella.principal import Principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]


@router.get("/v1/memory/{memory_id}", response_model=dict[str, Any])
def get_memory(request: Request, memory_id: str, principal: PrincipalDep) -> dict[str, Any]:
    client = memory_client(request, principal)
    try:
        hit = client.get_memory(memory_id)
    except PermissionError as exc:
        audit_http(
            request,
            principal,
            op="memory_get",
            tenant_accessed=principal.tenant_id,
            target_id=memory_id,
            details={"error": "forbidden"},
        )
        raise ApiError("forbidden", str(exc), 403) from exc
    except TenantIsolationError as exc:
        audit_http(
            request,
            principal,
            op="memory_get",
            tenant_accessed=principal.tenant_id,
            target_id=memory_id,
            details={"error": "tenant_isolation"},
        )
        raise ApiError("memory_not_found", "memory not found", 404) from exc
    except Exception as exc:
        audit_http(
            request,
            principal,
            op="memory_get",
            tenant_accessed=principal.tenant_id,
            target_id=memory_id,
            details={"error": "memory_backend_error"},
        )
        raise ApiError("memory_backend_error", str(exc), 500) from exc
    if hit is None:
        audit_http(
            request,
            principal,
            op="memory_get",
            tenant_accessed=principal.tenant_id,
            target_id=memory_id,
            details={"found": False},
        )
        raise ApiError("memory_not_found", "memory not found", 404)
    audit_http(
        request,
        principal,
        op="memory_get",
        tenant_accessed=str(hit.get("tenant_id") or principal.tenant_id),
        target_id=memory_id,
        details={"found": True},
    )
    return hit
