"""Write route."""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from panella.client import QuotaExceeded
from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, memory_client
from panella.http.schemas import WriteOutcomeMetadata, WriteRequest, WriteResponse
from panella.principal import Principal
from panella.write_hygiene import (
    HTTP_BLOCKED_WRITE_METADATA,
    RESERVED_TAG_PREFIXES,
    strip_reserved_tags,
)

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]

# Ingress-hygiene constants now live in the neutral panella.write_hygiene module so the HTTP
# route and the MCP write tool share ONE source (see that module for the full rationale). The names
# are re-bound here so this route's body + its existing tests are unchanged.
# (author_agent_id / source_bridge / session_id stay caller-asserted BY DESIGN.)
_HTTP_BLOCKED_WRITE_METADATA = HTTP_BLOCKED_WRITE_METADATA
_HTTP_RESERVED_TAG_PREFIXES = RESERVED_TAG_PREFIXES
_strip_reserved_tags = strip_reserved_tags


@router.post("/v1/memory/write", response_model=WriteResponse)
def write_memory(request: Request, payload: WriteRequest, principal: PrincipalDep) -> WriteResponse:
    client = memory_client(request, principal)
    content_hash = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
    # Drop server-authoritative / internal-control keys an HTTP caller might try
    # to inject via metadata passthrough (see _HTTP_BLOCKED_WRITE_METADATA above).
    write_metadata = {
        k: v for k, v in payload.metadata.items() if k not in _HTTP_BLOCKED_WRITE_METADATA
    }
    # Also strip reserved control TAGS (e.g. ccsk:) so an HTTP caller can't plant a tag that
    # cc-sync's destructive source-version replace would later act on.
    if "tags" in write_metadata:
        write_metadata["tags"] = _strip_reserved_tags(write_metadata["tags"])
    try:
        # NOTE: we do NOT opt into raise_dedup_skipped. Letting the client
        # absorb dedup internally preserves quota accounting (the client
        # calls _record_write() after the dedup-catch). We read the dedup
        # signal from WriteResult.dedup_skipped instead.
        result = client.write(
            payload.content,
            room=payload.room,
            memory_type=payload.memory_type,
            **write_metadata,
        )
    except QuotaExceeded as exc:
        audit_http(request, principal, op="write", tenant_accessed=principal.tenant_id, details={"error": "quota_exceeded"})
        raise ApiError("quota_exceeded", str(exc), 429) from exc
    except PermissionError as exc:
        audit_http(request, principal, op="write", tenant_accessed=principal.tenant_id, details={"error": "forbidden"})
        raise ApiError("forbidden", str(exc), 403) from exc
    except ValueError as exc:
        audit_http(request, principal, op="write", tenant_accessed=principal.tenant_id, details={"error": "bad_request"})
        raise ApiError("bad_request", str(exc), 400) from exc
    except Exception as exc:
        audit_http(
            request,
            principal,
            op="write",
            tenant_accessed=principal.tenant_id,
            details={"error": "memory_backend_error"},
        )
        raise ApiError("memory_backend_error", str(exc), 500) from exc

    if result.queued_for_approval:
        outcome = "queued"
        outcome_metadata = WriteOutcomeMetadata(queued_reason="approval_required")
    elif result.dedup_skipped:
        outcome = "dedup_skipped"
        outcome_metadata = WriteOutcomeMetadata(is_existing=True)
    else:
        outcome = "stored"
        outcome_metadata = WriteOutcomeMetadata()

    audit_http(
        request,
        principal,
        op="write",
        tenant_accessed=str(payload.metadata.get("tenant_id") or principal.tenant_id),
        target_id=result.drawer_id,
        details={"outcome": outcome, "queued_for_approval": result.queued_for_approval},
    )
    return WriteResponse(
        drawer_id=result.drawer_id,
        wing=result.wing,
        room=result.room,
        queued_for_approval=result.queued_for_approval,
        approval_id=result.approval_id,
        outcome=outcome,
        outcome_metadata=outcome_metadata,
        content_hash=content_hash,
    )
