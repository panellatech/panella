"""Stats route — corpus aggregate diagnostics. Metadata only.

60-second in-memory TTL cache: aggregate_stats walks the entire corpus via
paginated /api/memories (one full round-trip per ~100 drawers — ~98 calls
for a 9811-drawer corpus). Cache is keyed on (principal_tenants, wing_filter)
so different tenants/scopes get isolated entries. Fresh-bypass via
`?fresh=1` for incident response.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from panella.http.auth import principal as principal_dep
from panella.http.errors import ApiError
from panella.http.routes.common import audit_http, memory_client
from panella.http.schemas import StatsResponse, WingStats
from panella.principal import Principal

router = APIRouter()
PrincipalDep = Annotated[Principal, Depends(principal_dep)]

# Cache entry: (expires_at_monotonic, payload_dict)
_CACHE_TTL_SECONDS = 60.0
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = Lock()


def _cache_key(principal: Principal, wing_filter: str | None) -> tuple[str, str]:
    # Tenant scope is the principal's read scope at request time. Keying on
    # the JOINED + sorted tenant list keeps {iris→t_owner_personal} and
    # {root→[t_owner_personal,t_wife_personal]} as distinct entries.
    # TenantScope wraps a tuple — read .tenant_ids directly.
    tenants = ",".join(sorted(principal.tenant_scope.tenant_ids))
    return (tenants, wing_filter or "")


def _cache_get(key: tuple[str, str]) -> dict[str, Any] | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if time.monotonic() >= expires_at:
            _cache.pop(key, None)
            return None
        return payload


def _cache_put(key: tuple[str, str], payload: dict[str, Any]) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, payload)


def _cache_clear_for_tests() -> None:
    with _cache_lock:
        _cache.clear()


@router.get("/v1/memory/stats", response_model=StatsResponse)
def get_stats(
    request: Request,
    principal: PrincipalDep,
    wing: str | None = Query(default=None, description="filter to one wing"),
    fresh: bool = Query(default=False, description="bypass 60s TTL cache"),
) -> StatsResponse:
    """Corpus aggregate stats. Bearer auth + principal scope. Metadata only — never returns content."""
    cache_key = _cache_key(principal, wing)
    cached: dict[str, Any] | None = None if fresh else _cache_get(cache_key)
    if cached is None:
        client = memory_client(request, principal)
        try:
            raw = client.aggregate_stats(wing_filter=wing)
        except PermissionError as exc:
            audit_http(
                request,
                principal,
                op="stats",
                tenant_accessed=principal.tenant_id,
                details={"error": "forbidden"},
            )
            raise ApiError("forbidden", str(exc), 403) from exc
        except Exception as exc:
            audit_http(
                request,
                principal,
                op="stats",
                tenant_accessed=principal.tenant_id,
                details={"error": "memory_backend_error", "exception": type(exc).__name__},
            )
            raise ApiError("memory_backend_error", str(exc), 500) from exc
        _cache_put(cache_key, raw)
        raw_payload = raw
        served_from = "fresh"
    else:
        raw_payload = cached
        served_from = "cache"
    audit_http(
        request,
        principal,
        op="stats",
        tenant_accessed=principal.tenant_id,
        details={
            "wing_filter": wing,
            "total_drawers": raw_payload["total_drawers"],
            "served_from": served_from,
        },
    )
    return StatsResponse(
        total_drawers=raw_payload["total_drawers"],
        wing_breakdown=[WingStats(**row) for row in raw_payload["wing_breakdown"]],
        last_synced_ts=raw_payload.get("last_synced_ts"),
    )
