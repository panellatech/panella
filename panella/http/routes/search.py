"""Search route."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from panella.client import TenantIsolationError
from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, memory_client
from panella.http.schemas import SearchRequest, SearchResponse
from panella.principal import Principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]


@router.post("/v1/memory/search", response_model=SearchResponse)
def search_memory(request: Request, payload: SearchRequest, principal: PrincipalDep) -> SearchResponse:
    client = memory_client(request, principal)
    try:
        hits = client.search(payload.query, k=payload.k, wings_hint=payload.wings_hint)
    except PermissionError as exc:
        audit_http(request, principal, op="search", tenant_accessed=principal.tenant_id, details={"error": "forbidden"})
        raise ApiError("forbidden", str(exc), 403) from exc
    except TenantIsolationError as exc:
        audit_http(
            request,
            principal,
            op="search",
            tenant_accessed=principal.tenant_id,
            details={"error": "tenant_isolation"},
        )
        raise ApiError("tenant_isolation", str(exc), 403) from exc
    except Exception as exc:
        audit_http(
            request,
            principal,
            op="search",
            tenant_accessed=principal.tenant_id,
            details={"error": "memory_backend_error"},
        )
        raise ApiError("memory_backend_error", str(exc), 500) from exc
    audit_http(
        request,
        principal,
        op="search",
        tenant_accessed=principal.tenant_id,
        details={"k": payload.k, "hit_count": len(hits)},
    )
    return SearchResponse(hits=hits)
