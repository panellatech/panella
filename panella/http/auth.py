"""Authentication and request middleware for the memory HTTP facade."""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from panella.http.errors import ApiError, error_payload
from panella.http.tokens import TokenRecord, TokenStore, principal_from_record
from panella.principal import Principal

logger = logging.getLogger(__name__)

CallNext = Callable[[Request], Awaitable[Response]]


class RateLimiter:
    def __init__(self, requests_per_minute: int = 100, *, clock: Callable[[], float] = time.time) -> None:
        self.requests_per_minute = requests_per_minute
        self.clock = clock
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.clock()
        bucket = self._buckets[key]
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= self.requests_per_minute:
            return False
        bucket.append(now)
        return True


def resolve_bearer(token_store: TokenStore, auth_header: str) -> TokenRecord:
    """Shared bearer-token resolution: header parse → store resolve → revoked/expired checks.

    Raises ``ApiError`` with the exact codes/statuses the REST ``AuthMiddleware`` and the network
    ``/mcp`` gate BOTH use, so the two surfaces cannot drift on how a token is authenticated
    (Slice-S P3b). Does NOT touch ``request.state`` or break-glass elevation — those are REST-only
    concerns the middleware layers on top. Returns the validated ``TokenRecord`` (its
    ``token_sha256`` is the rate-limit key)."""
    if not auth_header:
        raise ApiError("missing_token", "missing bearer token", 401)
    if not auth_header.startswith("Bearer ") or len(auth_header.split()) != 2:
        raise ApiError("malformed_token", "malformed bearer token", 401)
    raw_token = auth_header.split(" ", 1)[1].strip()
    if not raw_token:
        raise ApiError("malformed_token", "malformed bearer token", 401)
    record = token_store.resolve(raw_token)
    if record is None:
        raise ApiError("malformed_token", "unknown bearer token", 401)
    now = datetime.now(UTC)
    if record.revoked_at is not None and record.revoked_at <= now:
        raise ApiError("revoked_token", "token has been revoked", 403)
    if record.expired:
        raise ApiError("expired_token", "token has expired", 403)
    return record


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Any,
        *,
        token_store: TokenStore,
        rate_limiter: RateLimiter,
        elevated_tokens: dict[str, Principal] | None = None,
        auth_free_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self.token_store = token_store
        self.rate_limiter = rate_limiter
        self.elevated_tokens = elevated_tokens if elevated_tokens is not None else {}
        self.auth_free_paths = auth_free_paths or {"/v1/health"}

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.monotonic()
        try:
            if request.url.path not in self.auth_free_paths:
                record = self._authenticate(request)
                if not self.rate_limiter.allow(record.token_sha256):
                    raise ApiError("rate_limited", "rate limit exceeded", 429)
            response = await call_next(request)
        except ApiError as exc:
            response = JSONResponse(
                status_code=exc.status_code,
                content=error_payload(exc.code, exc.message, request_id),
            )
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "memory_http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "latency_ms": round((time.monotonic() - start) * 1000, 2),
            },
        )
        return response

    def _authenticate(self, request: Request) -> TokenRecord:
        record = resolve_bearer(self.token_store, request.headers.get("authorization", ""))
        request.state.token_record = record
        # record.token_sha256 == token_sha256(presented raw token) — the store resolved BY that
        # digest — so it is both the request-state key and the elevated-token map key.
        digest = record.token_sha256
        request.state.token_sha256 = digest
        cached = self.elevated_tokens.get(digest)
        if cached is not None:
            if not cached.is_root_with_break_glass():
                raise ApiError("expired_token", "token has expired", 403)
            request.state.principal = cached
        else:
            request.state.principal = principal_from_record(record)
        return record


def principal(request: Request):
    value = getattr(request.state, "principal", None)
    if value is None:
        raise ApiError("missing_principal", "request principal is unavailable", 401)
    return value
