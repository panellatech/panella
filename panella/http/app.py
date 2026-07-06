"""FastAPI application factory for the memory HTTP facade."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from panella._default_adapter import default_adapter
from panella.http import console
from panella.http.auth import AuthMiddleware, RateLimiter, resolve_bearer
from panella.http.config import MemoryHttpConfig, load_config
from panella.http.errors import ApiError, api_error_handler, error_payload, unhandled_error_handler
from panella.http.routes import approvals, audit, delete, health, principal, search, stats, write
from panella.http.tokens import TokenStore, normalize_principal_id
from panella.principal import root_principal
from panella.governance import GovernanceConfigError, current_governance
from panella.profile import AgentProfile, AgentProfileConfigError, ensure_rendered_profiles
from panella.store_probe import startup_self_check

logger = logging.getLogger(__name__)

# Routes the coherence gate refuses while incoherent (§1.5.3): the memory surface, the approval
# surface (approving finalizes a durable write — never do that on an incoherent box, WP-B2a), the
# break-glass token mint (an elevation minted against a wrong-identity box is worthless and
# confusing), and the operator console (WP-B3): the console is an unauthenticated page into which
# the operator PASTES the owner bearer + approval token, so an incoherent box must not serve it —
# that would invite secrets into a process whose own self-check says it cannot serve. /v1/health is
# the ONLY path that stays reachable while incoherent, so Doctor sees a live-but-refusing process.
_GATED_PREFIXES = ("/v1/memory/", "/v1/approvals/", "/console")
_GATED_EXACT = frozenset({"/v1/principal/break-glass"})


class PanellaBootConfigError(RuntimeError):
    """Raised when the serving factory detects a boot-time configuration error."""


class ServingGateMiddleware:
    """503s the gated memory routes until the startup self-check passes.

    Registered at FACTORY CONSTRUCTION (Starlette builds the middleware stack before lifespan
    runs — ``add_middleware`` from lifespan is rejected); reads ``app.state.memory_serving``,
    which the lifespan sets BEFORE yield after running the probe. The state defaults to False at
    construction, so a request that somehow races the lifespan is refused, never served blind."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = str(scope.get("path", ""))
            if any(path.startswith(prefix) for prefix in _GATED_PREFIXES) or path in _GATED_EXACT:
                state = scope.get("app").state if scope.get("app") else None
                if not getattr(state, "memory_serving", False):
                    reason = str(getattr(state, "memory_serving_reason", "startup self-check pending"))
                    response = JSONResponse(
                        status_code=503,
                        content=error_payload("memory_not_serving", reason, ""),
                        headers={"Retry-After": "30"},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def create_app(config: Any = None, *, memory_adapter: Any | None = None) -> FastAPI:
    http_config = load_config(config)
    logging.basicConfig(level=getattr(logging, http_config.log_level.upper(), logging.INFO))
    _preflight_boot_config(http_config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Coherence self-check BEFORE serving (§1.5.3): the middleware below is already in the
        # stack; this flips app.state.memory_serving. startup_self_check never raises.
        result = startup_self_check(http_config.store_path)
        app.state.memory_serving = result.serving
        app.state.memory_serving_reason = result.reason
        if result.serving:
            logger.info("memory self-check passed: %s", result.reason)
        else:
            logger.error("MEMORY SELF-CHECK FAILED — refusing memory routes (503): %s", result.reason)
        async with AsyncExitStack() as stack:
            # When the /mcp mount is enabled its Streamable-HTTP session manager needs a running
            # async context for the process lifetime (Slice-S P3b). Absent (owner's box) → no-op.
            session_manager = getattr(app.state, "mcp_session_manager", None)
            if session_manager is not None:
                await stack.enter_async_context(session_manager.run())
            try:
                yield
            finally:
                for context in list(app.state.break_glass_contexts.values()):
                    try:
                        context.__exit__(None, None, None)
                    except Exception:
                        logger.debug("failed to close break-glass context", exc_info=True)

    app = FastAPI(
        title="Panella HTTP API",
        version="1.0.0",
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.state.config = http_config
    app.state.started_at = time.monotonic()
    app.state.token_store = TokenStore(http_config.token_db_path)
    app.state.elevated_tokens = {}
    app.state.break_glass_contexts = {}
    # Fail-closed default until the lifespan probe runs.
    app.state.memory_serving = False
    app.state.memory_serving_reason = "startup self-check pending"
    app.state.memory_adapter = memory_adapter if memory_adapter is not None else default_adapter(
        source="panella-http",
    )
    # WP-B3 — the operator console shell has no data of its own (all data comes from JS fetch()
    # calls that DO send the bearer), so its page/asset paths must be reachable without one — a
    # browser's page-load navigation cannot attach custom headers. Computed BEFORE add_middleware
    # (AuthMiddleware is constructed at this call, not lazily) so the auth-free set is correct from
    # the first request. Flag OFF (default) leaves both collections exactly as they start below.
    auth_free_paths: set[str] = {"/v1/health"}
    auth_free_prefixes: tuple[str, ...] = ()
    if console.console_enabled():
        auth_free_paths, auth_free_prefixes = console.mount_console(
            app, auth_free_paths=auth_free_paths, auth_free_prefixes=auth_free_prefixes,
        )
    app.add_middleware(
        AuthMiddleware,
        token_store=app.state.token_store,
        rate_limiter=RateLimiter(http_config.rate_limit_per_minute),
        elevated_tokens=app.state.elevated_tokens,
        auth_free_paths=auth_free_paths,
        auth_free_prefixes=auth_free_prefixes,
    )
    # Added AFTER AuthMiddleware → runs BEFORE it (LIFO): an incoherent box refuses loudly even
    # to unauthenticated callers, and never burns rate-limit budget while dark.
    app.add_middleware(ServingGateMiddleware)
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(write.router)
    app.include_router(delete.router)
    app.include_router(audit.router)
    app.include_router(principal.router)
    app.include_router(stats.router)
    # WP-B2a — HTTP approval surface. Always registered; each route resolves the deployment's
    # approval transport per request (build_transport_if_approvable) → 404 on a non-local_cli box,
    # so a telegram/foreign box exposes no HTTP approval surface. Serving-gated above.
    app.include_router(approvals.router)
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]

    # Slice-S P3b — the network MCP surface. Opt-in (default OFF): when PANELLA_MCP_ENABLED is unset
    # (owner's live panella-http unit), this branch is skipped entirely and create_app returns the
    # bare FastAPI app — byte-for-byte the pre-P3b behavior. When enabled, /mcp is handled OUTSIDE
    # the REST middleware stack (no double-auth, no BaseHTTPMiddleware streaming buffer) by an outer
    # ASGI dispatcher, behind its own bearer + rate + serving gate.
    if http_config.mcp_enabled:
        return _mount_mcp(app, http_config)
    return app


class _McpDispatchApp:
    """Outer ASGI app (Slice-S P3b): routes ``/mcp`` to the Streamable-HTTP MCP session manager
    behind its own bearer + rate + serving gate; everything else goes to the FastAPI app WITH its
    REST middleware stack. This keeps ``/mcp`` entirely OUTSIDE ``AuthMiddleware``/``ServingGate`` —
    no double auth, no double rate-limit, and no ``BaseHTTPMiddleware`` buffering hop on the MCP
    streaming responses. The FastAPI lifespan (which enters ``session_manager.run()``) still runs
    because lifespan/websocket/non-``/mcp`` scopes are forwarded to the inner app."""

    def __init__(self, app: FastAPI, session_manager: Any, security: Any) -> None:
        self.app = app
        self.session_manager = session_manager
        # The SDK's DNS-rebinding/Host/Origin validator, reused (not re-implemented) so we can reject
        # a foreign Host BEFORE doing any auth work (token DB touch / rate-limit) — same matching the
        # session manager applies internally.
        self.security = security

    def __getattr__(self, name: str) -> Any:
        # Transparently delegate attribute access to the wrapped FastAPI app so this wrapper is a
        # faithful stand-in for it — ``create_app().openapi()`` / ``.state`` / ``.routes`` keep
        # working whether or not the /mcp mount is enabled. ``__call__`` (the ASGI entry) is the ONLY
        # thing this class overrides. __getattr__ fires only for MISSING attributes, so the
        # instance attrs below are found normally; guard them to avoid recursion if they're unset.
        if name in ("app", "session_manager", "security"):
            raise AttributeError(name)
        return getattr(self.app, name)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and _is_mcp_path(str(scope.get("path", ""))):
            await self._handle_mcp(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _handle_mcp(self, scope: Any, receive: Any, send: Any) -> None:
        state = self.app.state
        # DNS-rebinding / Host / Origin validation FIRST (before any auth work), reusing the SDK's
        # own validator so a foreign Host is rejected without touching the token DB or rate limiter.
        request = Request(scope, receive)
        security_error = await self.security.validate_request(request, is_post=(request.method == "POST"))
        if security_error is not None:
            await security_error(scope, receive, send)
            return
        # Serving gate — mirror ServingGateMiddleware: an incoherent box refuses /mcp with 503,
        # never serves blind. The default (False until the lifespan probe runs) covers a race.
        if not getattr(state, "memory_serving", False):
            reason = str(getattr(state, "memory_serving_reason", "startup self-check pending"))
            await _send_json_asgi(send, 503, error_payload("memory_not_serving", reason, ""), retry_after="30")
            return
        # Authenticate the bearer AND authorize it as the OWNER principal in one step (a valid token
        # is necessary but NOT sufficient — see _authenticate_mcp_owner). On failure it raises ApiError
        # with the exact code/status (401 unauth, 403 non-owner) and nothing is dispatched.
        try:
            record = self._authenticate_mcp_owner(scope)
        except ApiError as exc:
            await _send_json_asgi(send, exc.status_code, error_payload(exc.code, exc.message, ""))
            return
        # Per-token rate limit (the /mcp gate owns this — AuthMiddleware never sees /mcp).
        if not state.mcp_rate_limiter.allow(record.token_sha256):
            await _send_json_asgi(send, 429, error_payload("rate_limited", "rate limit exceeded", ""))
            return
        await self.session_manager.handle_request(scope, receive, send)

    def _authenticate_mcp_owner(self, scope: Any) -> Any:
        """Resolve the bearer token AND require it to be the OWNER (governance root) principal.

        /mcp is the box owner's surface: its tools run under the configured MCP profile's authority
        (a prebuilt principal), so a merely-valid token must NOT be enough — a low-privilege or
        foreign-tenant token could otherwise borrow that authority to read/enqueue under the owner
        wing. Bearer resolution uses the SHARED resolver (identical codes/order to the REST
        AuthMiddleware); the owner check compares the NORMALIZED principal id directly (never
        principal_from_record, which would AgentProfile.load an unknown principal and raise → a 500
        rather than a clean 403). Raises ApiError (401 unauth / 403 non-owner); returns the record.
        Per-token multi-principal MCP is a deliberate non-goal (single-owner box). GH Codex bot P1.
        """
        record = resolve_bearer(self.app.state.token_store, _asgi_header(scope, b"authorization"))
        if normalize_principal_id(record.principal_id) != root_principal().id:
            raise ApiError("forbidden", "MCP requires the owner (root) principal", 403)
        return record


def _is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def _default_loopback_hosts() -> list[str]:
    """DNS-rebinding allowed_hosts for a loopback-published mount. The SDK matches a Host exactly
    or against a ``host:*`` port wildcard, so this admits the compose 127.0.0.1:PORT bind without
    hardcoding the port. A deployment exposing /mcp beyond loopback sets PANELLA_MCP_ALLOWED_HOSTS."""
    return ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]


def _asgi_header(scope: Any, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


async def _send_json_asgi(send: Any, status: int, payload: dict[str, Any], *, retry_after: str | None = None) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = [(b"content-type", b"application/json")]
    if retry_after:
        headers.append((b"retry-after", retry_after.encode("ascii")))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _mount_mcp(app: FastAPI, http_config: MemoryHttpConfig) -> Any:
    """Build the /mcp Streamable-HTTP mount (Slice-S P3b) and wrap ``app`` in the dispatcher.

    Fails LOUD at factory time if the box requested MCP but the serving profile can't load (D2) —
    a box that asks for /mcp and can't serve it must crash at boot, never half-serve."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings

    from panella.client import MemoryClient
    from panella.mcp_tools import McpToolContext, build_mcp_server, build_transport_if_approvable
    from panella.principal import principal_default_for_profile

    profile = AgentProfile.load(http_config.mcp_profile)
    principal = principal_default_for_profile(profile)
    governance = current_governance()
    transport = build_transport_if_approvable(governance)
    client = MemoryClient(
        profile,
        principal,
        adapter=app.state.memory_adapter,
        outbox_db_path=http_config.outbox_db_path,
        audit_db_path=http_config.audit_db_path,
    )
    # ctx.serving stays True here — the dispatcher's serving gate reads app.state.memory_serving
    # authoritatively before every /mcp request, so /mcp can never serve while incoherent.
    ctx = McpToolContext(
        client=client,
        outbox_db_path=http_config.outbox_db_path,
        profile=profile,
        governance=governance,
        transport=transport,
        serving=True,
        serving_reason="",
    )
    server = build_mcp_server(ctx)

    allowed_hosts = list(http_config.mcp_allowed_hosts) or _default_loopback_hosts()
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=list(http_config.mcp_allowed_origins),
    )
    session_manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=False,
        security_settings=security,
    )
    app.state.mcp_session_manager = session_manager
    app.state.mcp_rate_limiter = RateLimiter(http_config.rate_limit_per_minute)
    logger.info(
        "MCP /mcp mount enabled (profile=%s, transport=%s, allowed_hosts=%s)",
        http_config.mcp_profile,
        getattr(transport, "name", None),
        allowed_hosts,
    )
    return _McpDispatchApp(app, session_manager, TransportSecurityMiddleware(security))


def _preflight_boot_config(http_config: MemoryHttpConfig) -> None:
    """Fail common self-host configuration mistakes at factory construction, not per request."""
    try:
        current_governance()
        ensure_rendered_profiles()
        _load_boot_profile(http_config.profile_name, env_var="PANELLA_HTTP_PROFILE")
        if http_config.mcp_enabled:
            _load_boot_profile(http_config.mcp_profile, env_var="PANELLA_MCP_PROFILE")
    except GovernanceConfigError as exc:
        raise PanellaBootConfigError(f"governance config error: {exc}") from None
    except AgentProfileConfigError as exc:
        raise PanellaBootConfigError(str(exc)) from None


def _load_boot_profile(name: str, *, env_var: str) -> None:
    try:
        AgentProfile.load(name)
    except AgentProfileConfigError:
        raise
    # ANY other failure to load a profile at boot is a config mistake — malformed profile YAML
    # (yaml.YAMLError), a valid-YAML-but-structurally-invalid profile missing required keys / wrong
    # types (KeyError / TypeError from from_dict), a missing/unreadable wings.yaml (OSError), or a
    # bad value (ValueError). Surface ALL of them as one actionable line + exit 2, never the opaque
    # traceback WP3 exists to eliminate. The exception type + message are preserved (and via
    # `from exc`), so a genuine bug still surfaces as a named boot-config error, not silently.
    except Exception as exc:
        raise AgentProfileConfigError(
            f"{env_var}={name!r} could not be loaded ({type(exc).__name__}: {exc}); "
            "check the rendered profile + wings.yaml, or rerun `panella-render-config --out <dir>` "
            "and set PANELLA_CONFIG_DIR=<dir>"
        ) from exc


def _openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {"type": "http", "scheme": "bearer"}
    for path, path_item in schema.get("paths", {}).items():
        if path == "/v1/health":
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"BearerAuth": []}])
    app.openapi_schema = schema
    return app.openapi_schema


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    _ = exc
    request_id = str(getattr(request.state, "request_id", ""))
    return JSONResponse(
        status_code=422,
        content=error_payload("validation_error", "request validation failed", request_id),
        headers={"X-Request-ID": request_id},
    )


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    request_id = str(getattr(request.state, "request_id", ""))
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload("http_error", str(exc.detail), request_id),
        headers={"X-Request-ID": request_id},
    )
