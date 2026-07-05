"""Delete route."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from panella.client import RtbfFinalizeInFlight
from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, memory_client
from panella.http.schemas import DeleteRequest, DeleteResponse
from panella.principal import Principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]


@router.post("/v1/memory/delete", response_model=DeleteResponse)
def delete_memory(
    request: Request,
    payload: DeleteRequest,
    principal: PrincipalDep,
    mode: Literal["soft", "hard"] = Query(default="soft"),
) -> DeleteResponse:
    client = memory_client(request, principal)
    try:
        if mode == "hard":
            deleted = client.hard_delete(payload.drawer_id, payload.reason)
        else:
            deleted = client.tombstone(payload.drawer_id, payload.reason)
    except PermissionError as exc:
        audit_http(
            request,
            principal,
            op="hard_delete" if mode == "hard" else "tombstone",
            tenant_accessed=principal.tenant_id,
            target_id=payload.drawer_id,
            details={"error": "forbidden", "hard_delete": mode == "hard"},
        )
        raise ApiError("forbidden", str(exc), 403) from exc
    except RtbfFinalizeInFlight as exc:
        # Stage 2 P0 — the forget is DEFERRED because the target is mid-finalization; it is
        # retryable once the finalize is terminal. 409 Conflict (not a 500 backend error).
        audit_http(
            request,
            principal,
            op="hard_delete" if mode == "hard" else "tombstone",
            tenant_accessed=principal.tenant_id,
            target_id=payload.drawer_id,
            details={"error": "forget_deferred_finalize_in_flight", "hard_delete": mode == "hard"},
        )
        raise ApiError("forget_deferred", str(exc), 409) from exc
    except ValueError as exc:
        audit_http(
            request,
            principal,
            op="hard_delete" if mode == "hard" else "tombstone",
            tenant_accessed=principal.tenant_id,
            target_id=payload.drawer_id,
            details={"error": "bad_request", "hard_delete": mode == "hard"},
        )
        raise ApiError("bad_request", str(exc), 400) from exc
    except Exception as exc:
        audit_http(
            request,
            principal,
            op="hard_delete" if mode == "hard" else "tombstone",
            tenant_accessed=principal.tenant_id,
            target_id=payload.drawer_id,
            details={"error": "memory_backend_error", "hard_delete": mode == "hard"},
        )
        raise ApiError("memory_backend_error", str(exc), 500) from exc
    audit_http(
        request,
        principal,
        op="hard_delete" if mode == "hard" else "tombstone",
        tenant_accessed=principal.tenant_id,
        target_id=payload.drawer_id,
        details={"hard_delete": mode == "hard", "deleted": deleted, "reason": payload.reason},
    )
    return DeleteResponse(deleted=deleted, drawer_id=payload.drawer_id, mode=mode)
