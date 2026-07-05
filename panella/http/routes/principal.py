"""Principal routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, memory_client
from panella.http.schemas import BreakGlassRequest, BreakGlassResponse
from panella.http.tokens import token_sha256
from panella.principal import Principal, root_principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]


@router.post("/v1/principal/break-glass", response_model=BreakGlassResponse)
def open_break_glass(
    request: Request,
    payload: BreakGlassRequest,
    principal: PrincipalDep,
) -> BreakGlassResponse:
    client = memory_client(request, principal)
    try:
        context = client.break_glass(payload.reason, ttl_seconds=payload.ttl_seconds)
        elevated = context.__enter__()
    except PermissionError as exc:
        raise ApiError("forbidden", str(exc), 403) from exc
    except ValueError as exc:
        raise ApiError("bad_request", str(exc), 400) from exc

    label = f"break-glass-{request.state.request_id}"
    token = request.app.state.token_store.mint(
        # Lockstep with break_glass._validate_root_caller: the minted elevation names the
        # governance root operator, never a hardcoded id.
        principal_id=root_principal().id,
        label=label,
        tenant_scope=("*",),
        ttl_seconds=payload.ttl_seconds,
    )
    digest = token_sha256(token)
    request.app.state.elevated_tokens[digest] = elevated
    request.app.state.break_glass_contexts[digest] = context
    audit_http(
        request,
        elevated,
        op="break_glass_grant",
        tenant_accessed="*",
        details={"ttl_seconds": payload.ttl_seconds, "requested_tenants": payload.requested_tenants},
    )
    expires_at = elevated.break_glass_token.expires_at.isoformat() if elevated.break_glass_token else ""
    return BreakGlassResponse(token=token, expires_at=expires_at)
