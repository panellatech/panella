"""Health route."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from panella.http.schemas import HealthResponse

router = APIRouter()


@router.get("/v1/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    uptime = time.monotonic() - request.app.state.started_at
    return HealthResponse(ok=True, build_sha=request.app.state.config.build_sha, uptime_seconds=round(uptime, 3))
