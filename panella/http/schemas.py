"""Pydantic v2 HTTP schemas for the versioned memory API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchRequest(StrictModel):
    query: str = Field(min_length=1)
    k: int | None = Field(default=None, ge=1, le=100)
    wings_hint: list[str] | None = None


class SearchResponse(StrictModel):
    hits: list[dict[str, Any]]


class WriteRequest(StrictModel):
    content: str = Field(min_length=1)
    room: str = Field(min_length=1)
    memory_type: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WriteOutcomeMetadata(StrictModel):
    is_existing: bool = False
    queued_reason: str | None = None


class WriteResponse(StrictModel):
    drawer_id: str
    wing: str
    room: str
    queued_for_approval: bool = False
    approval_id: int | None = None
    # Open enum: "stored" | "dedup_skipped" | "queued" | <future-value>.
    # Clients SHOULD tolerate unknown values per docs/panella-http-contract.md.
    outcome: str = "stored"
    outcome_metadata: WriteOutcomeMetadata = Field(default_factory=WriteOutcomeMetadata)
    # sha256 of the request body content; lets clients trace dedup matches.
    content_hash: str = ""


class DeleteRequest(StrictModel):
    drawer_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DeleteResponse(StrictModel):
    deleted: bool
    drawer_id: str
    mode: Literal["soft", "hard"]


class AuditEntry(StrictModel):
    seq: int
    ts_iso: str
    principal_id: str
    tenant_accessed: str
    op: str
    target_id: str | None = None
    reason_code: str | None = None
    details: dict[str, Any] | None = None
    prev_hash: str
    this_hash: str


class AuditResponse(StrictModel):
    entries: list[AuditEntry]


class BreakGlassRequest(StrictModel):
    reason: str = Field(min_length=1)
    ttl_seconds: int = Field(default=600, ge=1, le=3600)
    requested_tenants: list[str] = Field(default_factory=lambda: ["*"])


class BreakGlassResponse(StrictModel):
    token: str
    expires_at: str
    token_type: Literal["bearer"] = "bearer"


class HealthResponse(StrictModel):
    ok: bool
    build_sha: str
    uptime_seconds: float


class WingStats(StrictModel):
    """Per-wing corpus aggregate. Metadata only — never includes drawer content."""

    wing: str
    drawer_count: int
    rooms: dict[str, int] = Field(default_factory=dict)
    most_recent_write_ts: str | None = None


class StatsResponse(StrictModel):
    """Corpus aggregate stats from `/v1/memory/stats`. Honors read_allowlist."""

    total_drawers: int
    wing_breakdown: list[WingStats] = Field(default_factory=list)
    last_synced_ts: str | None = None


class ErrorResponse(StrictModel):
    code: str
    message: str
    request_id: str
