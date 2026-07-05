"""Structured HTTP error envelope for the memory API."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass
class ApiError(Exception):
    code: str
    message: str
    status_code: int = 400


def error_payload(code: str, message: str, request_id: str) -> dict[str, str]:
    return {"code": code, "message": message, "request_id": request_id}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    request_id = str(getattr(request.state, "request_id", ""))
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(exc.code, exc.message, request_id),
        headers={"X-Request-ID": request_id},
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    _ = exc
    request_id = str(getattr(request.state, "request_id", ""))
    return JSONResponse(
        status_code=500,
        content=error_payload("internal_error", "internal server error", request_id),
        headers={"X-Request-ID": request_id},
    )
