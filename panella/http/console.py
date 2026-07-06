"""WP-B3 — the mini operator console (governance visibility, single static page).

Security spine of this module is **stored XSS**, not CSRF (public-release-plan v8 §WP-B3): the
console renders ``content_preview``/search-hit/audit-row fields that are attacker-influenced (any
candidate that reached the approval queue). The console itself carries ZERO data and ZERO secrets —
it is three static files (HTML shell, JS, CSS) that the operator's browser loads WITHOUT a bearer
(a page load cannot carry custom headers), then the JS makes its own ``fetch()`` calls against the
existing ``/v1/approvals``, ``/v1/memory/search``, ``/v1/memory/audit``, ``/v1/memory/stats`` routes
WITH the bearer the operator pastes into a password field. All rendering-safety obligations therefore
live in ``console.js`` (textContent-only DOM construction) and are proven structurally by
``tests/test_console.py`` (no ``innerHTML``/``eval``/inline handlers, exact CSP on every response).

Flag-gated (``PANELLA_CONSOLE_ENABLED`` — unset/empty ⇒ OFF, mirroring ``_env_flag`` in
``panella/http/config.py``): when OFF, ``mount_console`` is never called and the three routes below
do not exist — zero routes, zero auth-free paths, byte-identical to a build without this module.

Static-file serving is a tiny explicit allowlist (NOT ``StaticFiles``): only the three known
filenames map to a route, computed from a directory constant, and every response is read via
``Path.name`` equality (not a caller-supplied path segment concatenated onto a directory) — so
there is no path-traversal surface to reason about, no directory listing, and no possibility of a
mistyped glob accidentally serving an unintended file out of this package.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import PlainTextResponse, Response

STATIC_DIR = Path(__file__).resolve().parent / "static" / "console"

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

# The three (and only three) files this module ever serves, keyed by the URL segment the operator's
# browser requests. Adding a fourth asset means adding a line here — there is no wildcard path.
_ASSET_CONTENT_TYPES: dict[str, str] = {
    "console.js": "application/javascript; charset=utf-8",
    "console.css": "text/css; charset=utf-8",
}

CONSOLE_PATH = "/console"
CONSOLE_STATIC_PREFIX = "/console/static/"
# The whole "/console/" namespace is auth-free (the exact "/console" shell is in auth_free_paths):
# every path under it resolves to an inert allowlisted asset or a CSP-covered 404, so a browser can
# reach any of them without a bearer — content-gating is the route handler's job, not auth's.
CONSOLE_NAMESPACE_PREFIX = "/console/"


def console_enabled() -> bool:
    """Truthy env flag — unset/empty/anything-but-{1,true,yes,on} (case-insensitive) is OFF.

    Copies the exact semantics of ``_env_flag`` in ``panella/http/config.py`` (that module is
    outside this WP's file surface per the B3 brief, so the parse is duplicated here rather than
    imported — a one-line, well-known truthy check, not worth widening the diff's blast radius to
    share). See ``tests/test_console.py`` for the OFF-by-default coverage."""
    return os.environ.get("PANELLA_CONSOLE_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def build_console_router() -> APIRouter:
    """The three console routes: the HTML shell + the two static assets. Callers only ever get this
    router when ``console_enabled()`` is True (see ``mount_console``) — calling this directly with
    the flag off would still build working routes, so the OFF gate is enforced once, at the call
    site in ``app.py``, not duplicated inside every handler."""
    router = APIRouter()

    @router.get(CONSOLE_PATH, include_in_schema=False)
    def console_index() -> Response:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return Response(content=html, media_type="text/html; charset=utf-8", headers={"Content-Security-Policy": _CSP})

    @router.get(CONSOLE_STATIC_PREFIX + "{asset_name}", include_in_schema=False)
    def console_static(asset_name: str) -> Response:
        content_type = _ASSET_CONTENT_TYPES.get(asset_name)
        if content_type is None:
            # Unknown name (typo, an extension we don't ship) — 404, never a directory listing and
            # never an attempt to resolve it against the filesystem.
            return PlainTextResponse("not found", status_code=404, headers={"Content-Security-Policy": _CSP})
        body = (STATIC_DIR / asset_name).read_text(encoding="utf-8")
        return Response(content=body, media_type=content_type, headers={"Content-Security-Policy": _CSP})

    @router.api_route("/console/{rest:path}", include_in_schema=False,
                      methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def console_catch_all(rest: str) -> Response:
        # A slash-bearing path under /console (e.g. ``/console/static/../app.py`` or
        # ``/console/static//console.js``) never matches the single-segment ``{asset_name}`` route
        # (Starlette's default converter is ``[^/]+``), so without this it would fall through to the
        # app's default 404 handler, which emits NO Content-Security-Policy — falsifying the "CSP on
        # every console-namespace response" invariant (Codex B3 security review, P2). This catch-all
        # keeps the whole ``/console`` namespace CSP-covered and inert: always a 404, never resolves
        # ``rest`` against the filesystem, never serves anything. It is registered LAST so the two
        # real routes above win for their exact paths.
        return PlainTextResponse("not found", status_code=404, headers={"Content-Security-Policy": _CSP})

    return router


def mount_console(app: FastAPI, *, auth_free_paths: set[str], auth_free_prefixes: tuple[str, ...]) -> tuple[set[str], tuple[str, ...]]:
    """Register the console router on ``app`` and return the widened
    ``(auth_free_paths, auth_free_prefixes)`` the ``AuthMiddleware`` needs.

    Called from ``create_app`` ONLY when ``console_enabled()`` is True — when the flag is off this
    function is never invoked, so the app has zero console routes and both auth-free collections are
    untouched (paths stay exactly ``{"/v1/health"}``, prefixes stay ``()``), which is what
    ``tests/test_console.py`` asserts for the default-OFF case.

    Two different auth-free SHAPES, deliberately:
    - ``/console`` (the HTML shell) is added to the exact-match set — there is exactly one URL for
      it, no reason to prefix-match.
    - ``/console/static/`` is added as a PREFIX, not three exact names. The browser loading these
      assets on page-load genuinely cannot attach a bearer, so the whole prefix must be reachable
      unauthenticated — but that must NOT mean "any name under this prefix is servable": the route
      handler's own allowlist (``_ASSET_CONTENT_TYPES``) is what turns an unknown name into a 404
      (see ``console_static`` above). This mirrors an ordinary static-asset directory on any web
      server: the directory is public, a 404 for a missing file doesn't require a login prompt first.

    ``ServingGateMiddleware`` gates the whole ``/console`` prefix (see ``_GATED_PREFIXES`` in
    ``app.py``): an incoherent box refuses the console with 503, exactly like the memory/approval
    surfaces. The console is NOT inert — it is where the operator pastes the owner bearer + approval
    token — so a box whose own self-check says it cannot serve must not offer that page. ``/v1/health``
    stays the single always-reachable path.
    """
    app.include_router(build_console_router())
    widened_paths = auth_free_paths | {CONSOLE_PATH}
    # Auth-free the WHOLE ``/console/`` namespace, not just ``/console/static/``: the catch-all
    # (``/console/{rest:path}``) must be reachable so a trailing-slash ``/console/`` or any
    # ``/console/<x>`` gets the intended CSP-covered 404 instead of a bare, CSP-less 401 from
    # AuthMiddleware (GH-bot B3 P2). The routes under this prefix are inert — an allowlisted asset or
    # a 404 — so opening the prefix exposes no data; content-gating stays the route handler's job.
    widened_prefixes = auth_free_prefixes + (CONSOLE_NAMESPACE_PREFIX,)
    return widened_paths, widened_prefixes
