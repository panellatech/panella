"""Shared route helpers."""

from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from panella.audit import audit_connect, audit_write
from panella.client import MemoryClient
from panella.http.errors import ApiError
from panella.principal import Principal
from panella.profile import AgentProfile


def memory_client(request: Request, principal: Principal) -> MemoryClient:
    config = request.app.state.config
    adapter = getattr(request.app.state, "memory_adapter", None)
    profile = AgentProfile.load(config.profile_name)
    return MemoryClient(
        profile,
        principal,
        adapter=adapter,
        outbox_db_path=config.outbox_db_path,
        audit_db_path=config.audit_db_path,
    )


def audit_http(request: Request, principal: Principal, *, op: str, tenant_accessed: str, target_id: str | None = None, details: dict[str, Any] | None = None) -> int:
    enriched = {"source": "http", "request_id": request.state.request_id}
    if details:
        enriched.update(details)
    return audit_write(
        principal=principal,
        tenant_accessed=tenant_accessed,
        op=op,
        target_id=target_id,
        reason_code=principal.break_glass_token.reason if principal.break_glass_token else None,
        details=enriched,
        db_path=request.app.state.config.audit_db_path,
    )


def audit_rows(db_path: str, *, where: str = "", params: tuple[Any, ...] = (), limit: int = 100) -> list[dict[str, Any]]:
    with audit_connect(db_path) as conn:
        query = (
            "SELECT seq, ts_iso, principal_id, tenant_accessed, op, target_id, reason_code, "
            "details_json, prev_hash, this_hash FROM audit_log "
            f"{where} ORDER BY seq DESC LIMIT ?"
        )
        rows = conn.execute(query, (*params, limit)).fetchall()
    return [_audit_row_to_dict(row) for row in rows]


def require_break_glass(principal: Principal) -> None:
    if not principal.is_root_with_break_glass():
        raise ApiError("break_glass_required", "active break-glass token required", 403)


def _audit_row_to_dict(row: Any) -> dict[str, Any]:
    raw = dict(row)
    details_json = raw.pop("details_json")
    raw["details"] = json.loads(details_json) if details_json else None
    return raw
