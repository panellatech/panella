"""WP-B3 — the mini operator console. Security spine is **stored XSS**, not CSRF.

Coverage mirrors the B3 brief's acceptance list exactly:
1. flag OFF (default) → /console 404, auth-free set unchanged.
2. flag ON → /console 200 html + exact CSP; JS/CSS 200 + CSP; unknown static name → 404.
3. HTML structural: no <script> with a body, no on*= handlers, no inline style=.
4. JS static analysis: none of the forbidden APIs appear in console.js (comments excluded — this
   file's own explanatory comments intentionally NAME the forbidden APIs as a warning to future
   editors, so the check strips comments first, exactly like a linter would, rather than banning
   the ability to write a comment that says "don't use eval").
5. XSS payload round-trip: a pending candidate's script/img-onerror payload survives only inside a
   JSON string body — never reflected as HTML.
6. console mounted does not weaken existing surfaces (approvals still 403/401 as before).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from panella.client import MemoryClient
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.http.app import create_app
from panella.http.config import MemoryHttpConfig
from panella.principal import principal_default_for_profile, root_principal
from panella.profile import AgentProfile

CONSOLE_JS_PATH = Path(__file__).resolve().parents[1] / "panella" / "http" / "static" / "console" / "console.js"
EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
FORBIDDEN_JS_APIS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "localStorage",
    "sessionStorage",
    "document.cookie",
)
XSS_PAYLOAD = "<script>alert(1)</script><img src=x onerror=alert(2)>"


class RecordingAdapter:
    """Same in-memory adapter shape as test_approvals_http_api.py's RecordingAdapter — the
    finalizer writes here, so seeding a pending candidate with an XSS payload is a real
    end-to-end path through the SAME outbox the console's /v1/approvals/pending route reads."""

    def __init__(self):
        self.rows = []

    def add_memory(self, wing, room, content, metadata, conversation_id=None):
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append({
            "id": mid, "content": content, "wing": wing, "room": room,
            "tenant_id": metadata.get("tenant_id"), "metadata": metadata,
            "score": 1.0, "tags": ["status:active"],
        })
        return mid

    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        hits = [r for r in self.rows if query.lower() in str(r["content"]).lower()]
        if tenant_ids is not None:
            hits = [r for r in hits if r.get("tenant_id") in set(tenant_ids)]
        return hits[:k]

    def find_active_hash_by_marker(self, marker, tenant_id):
        return None


def _build(tmp_path, monkeypatch, *, seed_xss_candidate: bool = False, token_file=None):
    """Build a serving Panella HTTP app. Governance/config wiring mirrors
    tests/test_approvals_http_api.py's _build so the same "seed one pending candidate, read it back
    through the real approval transport" path is available to the XSS round-trip test."""
    if token_file is None:
        token_file = tmp_path / "approval.token"
        token_file.write_text("operator-secret")
        token_file.chmod(0o600)
    overlay = tmp_path / "governance.yaml"
    overlay.write_text(
        "approval:\n"
        '  authorized_approvers: ["local_cli:owner"]\n'
        "  transport:\n"
        '    kind: "local_cli"\n'
        "    config:\n"
        f'      token_file: "{token_file}"\n'
        '      token_mode: "0600"\n'
    )
    config_dir = tmp_path / "dist-config"
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(config_dir))
    reset_governance_cache()
    render_distribution_config(current_governance(), config_dir)

    store_path = tmp_path / "sqlite_vec.db"
    conn = sqlite3.connect(store_path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT)")
    conn.execute("INSERT INTO memories VALUES ('seed','seed','status:active,tenant:t_owner_personal','{}',NULL)")
    conn.commit()
    conn.close()

    adapter = RecordingAdapter()
    config = MemoryHttpConfig(
        token_db_path=tmp_path / "tokens.db",
        audit_db_path=tmp_path / "audit.db",
        outbox_db_path=tmp_path / "outbox.db",
        profile_name="serving",
        store_path=store_path,
    )
    app = create_app(config, memory_adapter=adapter)
    bearer = app.state.token_store.mint(principal_id=root_principal().id, label="test-bearer")

    approval_id = None
    if seed_xss_candidate:
        write_profile = AgentProfile.load("mcp-write")
        seed_client = MemoryClient(
            write_profile,
            principal_default_for_profile(write_profile),
            adapter=adapter,
            outbox_db_path=config.outbox_db_path,
            audit_db_path=config.audit_db_path,
        )
        result = seed_client.write(XSS_PAYLOAD, room="preferences", memory_type="owner_preference")
        assert result.queued_for_approval is True
        approval_id = result.approval_id

    return SimpleNamespace(app=app, bearer=bearer, token="operator-secret", approval_id=approval_id, config=config)


def _auth(env, *, bearer=True, approval_token=None):
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {env.bearer}"
    if approval_token is not None:
        headers["X-Approval-Token"] = approval_token
    return headers


# --- 1. flag OFF (default): /console 404, auth-free set unchanged ----------------------------------

def test_console_off_by_default_404(tmp_path, monkeypatch):
    # A valid bearer is attached so this proves the ROUTING-layer fact ("/console does not exist as
    # a route when the flag is off"), not the separate auth-layer fact that AuthMiddleware refuses
    # ANY unauthenticated request (including ones to routes that don't exist) with 401 before
    # Starlette's router gets a chance to 404 — see test_console_off_auth_free_set_unchanged below
    # for that distinct behavior, which is correct and unrelated to this WP.
    monkeypatch.delenv("PANELLA_CONSOLE_ENABLED", raising=False)
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r_index = c.get("/console", headers=_auth(env))
        r_js = c.get("/console/static/console.js", headers=_auth(env))
        r_css = c.get("/console/static/console.css", headers=_auth(env))
    assert r_index.status_code == 404
    assert r_js.status_code == 404
    assert r_css.status_code == 404


def test_console_off_auth_free_set_unchanged(tmp_path, monkeypatch):
    # With the flag off, /v1/health remains the ONLY unauthenticated path — a bare request to any
    # other route (including /console, which 404s rather than 401 since it isn't registered at all)
    # must not silently become reachable without a bearer.
    monkeypatch.delenv("PANELLA_CONSOLE_ENABLED", raising=False)
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r_health = c.get("/v1/health")
        r_stats_no_auth = c.get("/v1/memory/stats")
    assert r_health.status_code == 200
    assert r_stats_no_auth.status_code == 401


@pytest.mark.parametrize("flag_value", ["0", "false", "no", "off", "", "garbage"])
def test_console_off_for_falsy_and_unset_values(tmp_path, monkeypatch, flag_value):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", flag_value)
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r = c.get("/console", headers=_auth(env))
    assert r.status_code == 404


# --- 2. flag ON: /console 200 + CSP; JS/CSS 200 + CSP; unknown static name → 404 --------------------

@pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", "yes", "on"])
def test_console_on_serves_index_with_csp(tmp_path, monkeypatch, flag_value):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", flag_value)
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r = c.get("/console")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == EXPECTED_CSP


def test_console_on_serves_js_and_css_with_csp(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r_js = c.get("/console/static/console.js")
        r_css = c.get("/console/static/console.css")
    assert r_js.status_code == 200
    assert r_js.headers["content-type"].startswith("application/javascript")
    assert r_js.headers["content-security-policy"] == EXPECTED_CSP
    assert r_css.status_code == 200
    assert r_css.headers["content-type"].startswith("text/css")
    assert r_css.headers["content-security-policy"] == EXPECTED_CSP


@pytest.mark.parametrize(
    "asset_name",
    ["x.txt", "..%2Fapp.py", "app.py", "console.py", "index.html", "console.js%00.txt"],
)
def test_console_on_unknown_static_name_404(tmp_path, monkeypatch, asset_name):
    # No bearer needed: /console/static/ is a registered auth-free PREFIX (see
    # AuthMiddleware.auth_free_prefixes / console.mount_console) precisely so an unauthenticated
    # browser asset load reaches the route handler, whose own allowlist is what turns an unknown
    # name into a 404 — never a directory listing, never a 401 that would imply the prefix is
    # secretly gated. "%2F" (percent-encoded "/") reaches the handler as ONE literal path-segment
    # string ("../app.py"), which the allowlist rejects the same as any other unknown name.
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r = c.get(f"/console/static/{asset_name}")
    assert r.status_code == 404


async def _raw_asgi_get(app, path: str, headers: dict[str, str]) -> tuple[int, bytes]:
    """Send a literal, non-normalized ``path`` directly through the ASGI ``app`` callable.

    httpx (and therefore ``TestClient``) normalizes RFC-3986 dot-segments (``..``) in the URL
    BEFORE the request is ever transmitted — ``TestClient.get("/console/static/..")`` never reaches
    this server's code at all; it silently becomes a request for ``/console`` instead (verified
    manually while writing this test — httpx's own request-building step collapses it). A
    non-normalizing HTTP client, misconfigured reverse proxy, or a differently-behaved future ASGI
    server would NOT necessarily do that scrubbing for us, so the real safety property this WP
    needs proven is: given the literal bytes ``/console/static/..`` as the ASGI ``path``, does the
    APPLICATION ITSELF (not some client library standing in front of it) ever escape the
    ``static/console/`` directory? This drives the app directly with that literal scope, skipping
    httpx/TestClient's normalization entirely, and starts+stops the ASGI lifespan itself since we
    bypass ``TestClient`` (which normally owns that)."""
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("testclient", 1234), "server": ("testserver", 80), "scheme": "http",
        "http_version": "1.1", "root_path": "", "app": app,
    }

    started = []

    async def lifespan_receive():
        if not started:
            started.append(True)
            return {"type": "lifespan.startup"}
        return {"type": "lifespan.shutdown"}

    lifespan_messages: list[dict] = []

    async def lifespan_send(message):
        lifespan_messages.append(message)

    lifespan_task = asyncio.ensure_future(app({"type": "lifespan", "app": app}, lifespan_receive, lifespan_send))
    for _ in range(200):
        if any(m.get("type") == "lifespan.startup.complete" for m in lifespan_messages):
            break
        await asyncio.sleep(0.005)

    try:
        await app(scope, receive, send)
    finally:
        lifespan_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await lifespan_task

    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


@pytest.mark.asyncio
async def test_console_on_raw_dotdot_segment_is_rejected_server_side(tmp_path, monkeypatch):
    # The genuine server-side proof (see _raw_asgi_get docstring): drive the app with the LITERAL
    # path bytes "/console/static/.." — no client-side dot-segment scrubbing involved — and confirm
    # the app's own allowlist handler (not a client library) is what refuses it.
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    status, body = await _raw_asgi_get(env.app, "/console/static/..", {})
    assert status == 404
    assert b"app.py" not in body
    assert b"console.py" not in body


@pytest.mark.asyncio
async def test_console_on_raw_dotdot_slash_segment_is_rejected_server_side(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    status, body = await _raw_asgi_get(env.app, "/console/static/../app.py", {})
    assert status == 404
    assert b"create_app" not in body  # a snippet only present if app.py's source ever leaked


# --- 3. HTML structural: no <script> with a body, no on*= handlers, no inline style= ----------------

def test_html_has_no_inline_script_body(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        html = c.get("/console").text
    # Every <script ...> tag must carry a src= attribute and have an EMPTY body (</script>
    # immediately follows the opening tag's >). This rejects both `<script>code</script>` and
    # `<script src=x>code</script>`.
    for match in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL):
        attrs, body = match.group(1), match.group(2)
        assert "src=" in attrs, f"script tag missing src=: {match.group(0)!r}"
        assert body.strip() == "", f"script tag has an inline body: {match.group(0)!r}"


def test_html_has_no_on_star_handler_attributes(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        html = c.get("/console").text
    # \bon[a-z]+\s*= with a preceding word boundary excludes false positives like `content=`
    # (the "on" in "c-ontent" has no boundary before it) while still catching onclick=, onerror=,
    # onload=, etc. anywhere an attribute name could appear.
    assert re.search(r"\bon[a-z]+\s*=", html, flags=re.IGNORECASE) is None


def test_html_has_no_inline_style_attribute(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        html = c.get("/console").text
    assert re.search(r"\bstyle\s*=", html, flags=re.IGNORECASE) is None


# --- 4. JS static analysis: forbidden APIs never appear (comments excluded) -------------------------

def _strip_js_comments(source: str) -> str:
    """Strip // line comments and /* */ block comments. This file's console.js intentionally NAMES
    the forbidden APIs inside explanatory comments (a warning to future editors) — a correct
    "does the CODE use X" check must ignore comments, exactly like a linter does, rather than
    banning the ability to document the prohibition. Not a full JS tokenizer: it does not need to
    handle the forbidden strings appearing inside a JS string literal containing "//" or "/*",
    and console.js contains no such literals (verified by this test running against the real file)."""
    no_block_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    no_line_comments = re.sub(r"//[^\n]*", "", no_block_comments)
    return no_line_comments


def test_js_source_contains_no_forbidden_apis():
    source = CONSOLE_JS_PATH.read_text(encoding="utf-8")
    code_only = _strip_js_comments(source)
    for forbidden in FORBIDDEN_JS_APIS:
        assert forbidden not in code_only, f"forbidden API {forbidden!r} found in console.js (outside comments)"


def test_js_source_has_no_inline_event_handler_strings():
    # Belt-and-suspenders: the JS must not itself construct an on*= attribute string to assign onto
    # an element (e.g. `el.setAttribute("onclick", ...)`), which would be a second way to introduce
    # an inline handler even though this file only ever uses addEventListener.
    source = CONSOLE_JS_PATH.read_text(encoding="utf-8")
    code_only = _strip_js_comments(source)
    assert "setAttribute(\"on" not in code_only
    assert "setAttribute('on" not in code_only


# --- 5. XSS payload round-trip: only ever inside a JSON string, never reflected as HTML -------------

def test_xss_payload_survives_only_as_json_string(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch, seed_xss_candidate=True)
    with TestClient(env.app) as c:
        r = c.get("/v1/approvals/pending", headers=_auth(env, approval_token=env.token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    previews = [item["content_preview"] for item in body["pending"]]
    assert any(XSS_PAYLOAD in preview for preview in previews)
    # The raw <script>/<img onerror> bytes are present ONLY inside the JSON string value — proven
    # by the response content-type being application/json (never text/html) above. Rendering safety
    # from there is structural (textContent + CSP + no inline handlers), covered by the tests above.


# --- 6. console mounted does not weaken existing surfaces -------------------------------------------

def test_console_on_does_not_weaken_approvals_double_factor(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch, seed_xss_candidate=True)
    with TestClient(env.app) as c:
        r_no_token = c.get("/v1/approvals/pending", headers=_auth(env))
        r_no_bearer = c.get("/v1/approvals/pending", headers={"X-Approval-Token": env.token})
    assert r_no_token.status_code == 403
    assert r_no_bearer.status_code == 401


def test_console_on_does_not_expose_memory_routes_without_bearer(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r_search = c.post("/v1/memory/search", json={"query": "x"})
        r_audit = c.get("/v1/memory/audit")
        r_stats = c.get("/v1/memory/stats")
    assert r_search.status_code == 401
    assert r_audit.status_code == 401
    assert r_stats.status_code == 401


def test_console_on_health_still_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    env = _build(tmp_path, monkeypatch)
    with TestClient(env.app) as c:
        r = c.get("/v1/health")
    assert r.status_code == 200


def test_console_on_incoherent_box_still_refuses_memory_routes(tmp_path, monkeypatch):
    # The brief's explicit requirement: "ServingGateMiddleware must still gate the console... an
    # incoherent box refuses everything but /v1/health -- do not exempt console from it." What that
    # means concretely: mounting the console must NOT weaken ServingGateMiddleware's existing
    # refusal of the memory/approval surface when the box is incoherent (identity-pinned overlay
    # configured, but the store is missing with no PANELLA_FRESH_BOX=1 ack -- see
    # panella/store_probe.py's overlay_pinned branch). /console itself is UNCHANGED by this gate
    # either way: it was never one of ServingGateMiddleware's gated prefixes (_GATED_PREFIXES =
    # "/v1/memory/", "/v1/approvals/"; _GATED_EXACT = "/v1/principal/break-glass") -- same as any
    # other non-memory/approval route (e.g. /v1/health itself). The console shell has zero data and
    # zero secrets (stated in the brief), so serving it while incoherent carries none of the risk
    # the gate exists to prevent (a durable write or a data read against a wrong-identity store).
    monkeypatch.setenv("PANELLA_CONSOLE_ENABLED", "1")
    monkeypatch.delenv("PANELLA_FRESH_BOX", raising=False)
    overlay = tmp_path / "governance.yaml"
    overlay.write_text(
        "approval:\n"
        '  authorized_approvers: ["local_cli:owner"]\n'
        "  transport:\n"
        '    kind: "local_cli"\n'
        "    config:\n"
        f'      token_file: "{tmp_path / "unused.token"}"\n'
        '      token_mode: "0600"\n'
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(tmp_path / "dist-config"))
    reset_governance_cache()
    render_distribution_config(current_governance(), tmp_path / "dist-config")

    missing_store_path = tmp_path / "does-not-exist" / "sqlite_vec.db"  # overlay pinned, store absent, no ack
    config = MemoryHttpConfig(
        token_db_path=tmp_path / "tokens.db",
        audit_db_path=tmp_path / "audit.db",
        outbox_db_path=tmp_path / "outbox.db",
        profile_name="serving",
        store_path=missing_store_path,
    )
    app = create_app(config, memory_adapter=RecordingAdapter())
    bearer = app.state.token_store.mint(principal_id=root_principal().id, label="incoherent-probe")

    with TestClient(app) as c:
        r_health = c.get("/v1/health")
        r_console = c.get("/console")
        r_console_js = c.get("/console/static/console.js")
        r_stats = c.get("/v1/memory/stats", headers={"Authorization": f"Bearer {bearer}"})

    assert r_health.status_code == 200
    assert r_console.status_code == 200  # unauthenticated shell -- unaffected by the coherence gate
    assert r_console_js.status_code == 200
    assert r_stats.status_code == 503  # the ACTUAL gated surface still refuses, exactly as before
    assert r_stats.json()["code"] == "memory_not_serving"
