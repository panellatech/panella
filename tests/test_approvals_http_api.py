"""WP-B2a — the HTTP approval API's negative-test matrix + happy paths.

The trust-chain invariant (identical to the MCP surface, both via ``panella.approval_service``):
the HTTP bearer is routing-admission ONLY; every content-returning or state-changing route ALSO
requires a raw ``local_cli`` approval token in the ``X-Approval-Token`` header, verified through the
configured transport → canonical ``approved_by`` → authorized-approver gate → the finalizer chain.
The only bearer-only route is ``GET /v1/approvals/count`` (a bare integer). This file drives the
9-scenario matrix from the B2a brief plus the approve/reject happy paths.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from panella.audit import audit_verify_chain
from panella.client import MemoryClient
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.http.app import create_app
from panella.http.config import MemoryHttpConfig
from panella.principal import principal_default_for_profile, root_principal
from panella.profile import AgentProfile

APPROVAL_TOKEN = "operator-secret"


class RecordingAdapter:
    """In-memory store adapter (mirrors test_approval_mcp_loop): the finalizer writes here and the
    HTTP search route reads here, so approve→read is a real end-to-end loop without a live store."""

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

    def find_active_hash_by_marker(self, marker, tenant_id):  # only hit on the dedup path
        return None


def _overlay_body(transport: str, token_file, approvers: str = '["local_cli:owner"]') -> str:
    if transport == "local_cli":
        return (
            "approval:\n"
            f"  authorized_approvers: {approvers}\n"
            "  transport:\n"
            '    kind: "local_cli"\n'
            "    config:\n"
            f'      token_file: "{token_file}"\n'
            '      token_mode: "0600"\n'
        )
    # telegram: a real closed-vocabulary transport that is NOT MCP/HTTP-approvable → no HTTP surface
    return (
        "approval:\n"
        "  authorized_approvers: []\n"
        "  transport:\n"
        '    kind: "telegram"\n'
        "    config:\n"
        '      allowed_author_id: "123456"\n'
    )


def _build(tmp_path, monkeypatch, *, transport: str = "local_cli", seed_candidate: bool = True,
           approvers: str = '["local_cli:owner"]'):
    """Build a serving Panella HTTP app with the given approval transport, a minted owner bearer,
    and (optionally) one pending candidate seeded into the SAME outbox the routes read."""
    token_file = tmp_path / "approval.token"
    token_file.write_text(APPROVAL_TOKEN)
    token_file.chmod(0o600)
    overlay = tmp_path / "governance.yaml"
    overlay.write_text(_overlay_body(transport, token_file, approvers))
    config_dir = tmp_path / "dist-config"
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(config_dir))
    reset_governance_cache()
    render_distribution_config(current_governance(), config_dir)

    # A serving store: one owner-active row so the startup coherence self-check passes (else the
    # serving gate 503s /v1/approvals/* before the route runs).
    store_path = tmp_path / "sqlite_vec.db"
    conn = sqlite3.connect(store_path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT)")
    conn.execute(
        "INSERT INTO memories VALUES ('seed','seed','status:active,tenant:t_owner_personal','{}',NULL)"
    )
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
    if seed_candidate:
        write_profile = AgentProfile.load("mcp-write")
        seed_client = MemoryClient(
            write_profile,
            principal_default_for_profile(write_profile),
            adapter=adapter,
            outbox_db_path=config.outbox_db_path,
            audit_db_path=config.audit_db_path,
        )
        result = seed_client.write(
            "Panella keeps governed memories.", room="preferences", memory_type="owner_preference"
        )
        assert result.queued_for_approval is True
        approval_id = result.approval_id

    return SimpleNamespace(app=app, bearer=bearer, token=APPROVAL_TOKEN, approval_id=approval_id,
                           adapter=adapter, config=config)


@pytest.fixture
def local(tmp_path, monkeypatch):
    return _build(tmp_path, monkeypatch, transport="local_cli")


def _auth(env, *, bearer=True, token=None, query_token=False):
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {env.bearer}"
    if token is not None and not query_token:
        headers["X-Approval-Token"] = token
    return headers


# --- scenario 1: bearer only, no approval token → all double-factor routes refuse, no content -----

def test_s1_bearer_only_no_token_refused(local):
    with TestClient(local.app) as c:
        r_list = c.get("/v1/approvals/pending", headers=_auth(local))
        r_appr = c.post(f"/v1/approvals/{local.approval_id}/approve", headers=_auth(local))
        r_rej = c.post(f"/v1/approvals/{local.approval_id}/reject", headers=_auth(local))
    for r in (r_list, r_appr, r_rej):
        assert r.status_code == 403
    # no candidate content leaks in the refusal body
    assert "Panella keeps governed memories" not in r_list.text
    assert "content_preview" not in r_list.text


# --- scenario 2: valid approval token but missing/invalid bearer → 401 (routing admission) --------

def test_s2_missing_or_bad_bearer_is_401(local):
    with TestClient(local.app) as c:
        no_bearer = c.get("/v1/approvals/pending", headers={"X-Approval-Token": local.token})
        bad_bearer = c.get("/v1/approvals/pending",
                           headers={"Authorization": "Bearer nope", "X-Approval-Token": local.token})
    assert no_bearer.status_code == 401
    assert bad_bearer.status_code == 401


# --- scenario 3: invalid approval token + valid bearer → 403 and the attempt is audited -----------

def test_s3_bad_token_refused_and_audited(local):
    with TestClient(local.app) as c:
        r = c.get("/v1/approvals/pending", headers=_auth(local, token="wrong-token"))
    assert r.status_code == 403
    with sqlite3.connect(local.config.audit_db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op = 'approval_refused'"
        ).fetchone()
    assert count >= 1


# --- scenario 4: transport != local_cli (telegram) → no HTTP approval surface (404) ---------------

def test_s4_non_local_cli_transport_has_no_http_surface(tmp_path, monkeypatch):
    env = _build(tmp_path, monkeypatch, transport="telegram")
    with TestClient(env.app) as c:
        r_list = c.get("/v1/approvals/pending", headers=_auth(env, token=env.token))
        r_appr = c.post(f"/v1/approvals/{env.approval_id}/approve", headers=_auth(env, token=env.token))
        r_count = c.get("/v1/approvals/count", headers=_auth(env))
    assert r_list.status_code == 404
    assert r_appr.status_code == 404
    assert r_count.status_code == 404


# --- scenario 5: approval token in a query param is NOT parsed → refused --------------------------

def test_s5_token_in_query_is_not_accepted(local):
    with TestClient(local.app) as c:
        r = c.get(f"/v1/approvals/pending?approval_token={local.token}", headers=_auth(local))
    assert r.status_code == 403  # the query position is never read; no valid token present


# --- scenario 6: count route is bearer-only and returns ONLY the integer --------------------------

def test_s6_count_is_bearer_only_and_bare(local):
    with TestClient(local.app) as c:
        r = c.get("/v1/approvals/count", headers=_auth(local))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"pending_count"}
    assert isinstance(body["pending_count"], int)
    assert body["pending_count"] == 1  # the one seeded candidate


# --- scenario 7: forged provenance can't finalize; the stamp is the CONFIGURED transport ----------

def test_s7_foreign_provenance_row_is_not_finalizable(local):
    # Simulate a row stamped by a channel this box does not run (a stale telegram stamp on a
    # local_cli box). The HTTP approve path must refuse it — the finalizer/redrive gate trusts only
    # the configured transport's provenance, and HTTP never lets a caller assert approved_via/by.
    with sqlite3.connect(local.config.outbox_db_path) as conn:
        conn.execute(
            "UPDATE approval_queue SET status='approved', approved_via='telegram', approved_by='telegram:999' "
            "WHERE id=?",
            (local.approval_id,),
        )
    with TestClient(local.app) as c:
        r = c.post(f"/v1/approvals/{local.approval_id}/approve", headers=_auth(local, token=local.token))
    assert r.status_code == 409  # not an awaiting/retriable candidate for THIS transport
    assert local.adapter.rows == []  # nothing was finalized durably


def test_s7_happy_path_stamps_configured_provenance(local):
    with TestClient(local.app) as c:
        r = c.post(f"/v1/approvals/{local.approval_id}/approve", headers=_auth(local, token=local.token))
    assert r.status_code == 200
    # The durable provenance is the CONFIGURED transport (local_cli), never caller-supplied.
    assert local.adapter.rows[-1]["metadata"]["provenance"]["capture"] == "approved-via-local_cli"


# --- scenario 8: happy path — pending → approve → durable + readable + audit chain intact ----------

def test_s8_approve_finalizes_and_is_readable(local):
    with TestClient(local.app) as c:
        appr = c.post(f"/v1/approvals/{local.approval_id}/approve", headers=_auth(local, token=local.token))
        assert appr.status_code == 200
        body = appr.json()
        assert body["approved"] is True and body["finalized"] is True
        assert body["durable_id"]
        # readable via the HTTP search route (real end-to-end read; /v1/memory/search is a POST)
        read = c.post("/v1/memory/search", json={"query": "governed memories"}, headers=_auth(local))
        assert read.status_code == 200
        assert any("governed memories" in str(h).lower() for h in read.json().get("hits", []))
    assert audit_verify_chain(local.config.audit_db_path) is True


# --- scenario 9: reject aligns with MCP TOOL_REJECT — status rejected, candidate not durable -------

def test_s9_reject_marks_rejected_and_not_durable(local):
    with TestClient(local.app) as c:
        r = c.post(f"/v1/approvals/{local.approval_id}/reject", headers=_auth(local, token=local.token))
    assert r.status_code == 200
    assert r.json() == {"rejected": True, "approval_id": local.approval_id}
    with sqlite3.connect(local.config.outbox_db_path) as conn:
        (status,) = conn.execute(
            "SELECT status FROM approval_queue WHERE id=?", (local.approval_id,)
        ).fetchone()
    assert status == "rejected"
    assert local.adapter.rows == []  # never finalized durably


# --- boot-gate smoke: no bearer at all → 401 on every /v1/approvals/ route (auth-protected) --------

def test_boot_routes_require_auth(local):
    with TestClient(local.app) as c:
        for path, method in [
            ("/v1/approvals/pending", "get"),
            ("/v1/approvals/count", "get"),
            (f"/v1/approvals/{local.approval_id}/approve", "post"),
            (f"/v1/approvals/{local.approval_id}/reject", "post"),
        ]:
            r = getattr(c, method)(path)
            assert r.status_code == 401, f"{method} {path} must require a bearer"


# --- P1 regression: no auth oracle — "bad token" and "valid token, not an approver" are identical --

def test_no_auth_oracle_uniform_refusal(tmp_path, monkeypatch):
    # authorized_approvers=[] but a VALID token file: verify_presser succeeds (canonical
    # local_cli:owner) yet is not an approver. The client must not be able to tell this apart from a
    # simply-wrong token — both refuse with the identical 403 body (the specific reason lives only in
    # the server audit log).
    env = _build(tmp_path, monkeypatch, transport="local_cli", approvers="[]")
    with TestClient(env.app) as c:
        wrong = c.get("/v1/approvals/pending", headers=_auth(env, token="totally-wrong"))
        valid_not_approver = c.get("/v1/approvals/pending", headers=_auth(env, token=env.token))
    assert wrong.status_code == valid_not_approver.status_code == 403
    assert wrong.json()["code"] == valid_not_approver.json()["code"] == "approval_refused"
    assert wrong.json()["message"] == valid_not_approver.json()["message"]  # no oracle
    # but the server audit DID capture the distinct reasons (service-level refusal records)
    with sqlite3.connect(env.config.audit_db_path) as conn:
        reasons = {
            row[0]
            for row in conn.execute(
                "SELECT details_json FROM audit_log WHERE op='approval_refused'"
            ).fetchall()
        }
    joined = " ".join(reasons)
    assert "credential rejected" in joined and "not an authorized approver" in joined


# --- P1 regression: a state refusal (409) is audited (a security-relevant attempt/state change) ----

def test_state_refusal_is_audited(local):
    # A valid approver hitting a non-existent id → 409 state refusal; it MUST be audited.
    with TestClient(local.app) as c:
        r = c.post("/v1/approvals/999999/approve", headers=_auth(local, token=local.token))
    assert r.status_code == 409
    with sqlite3.connect(local.config.audit_db_path) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE op='approval_refused' AND details_json LIKE '%approval row not found%'"
        ).fetchone()
    assert count >= 1


# --- GH bot P1: approval routes require the OWNER bearer, not merely a valid one -------------------

def test_non_owner_bearer_refused(local):
    # A valid but NON-owner bearer + a valid approval token must STILL be refused: approval routes are
    # the owner's surface (like /mcp). The operator approval token alone is not enough to reach them.
    agent_bearer = local.app.state.token_store.mint(
        principal_id="agent:probe@t_owner_personal", label="agent-probe", tenant_scope=("t_owner_personal",)
    )
    hdr = {"Authorization": f"Bearer {agent_bearer}", "X-Approval-Token": local.token}
    with TestClient(local.app) as c:
        r_list = c.get("/v1/approvals/pending", headers=hdr)
        r_appr = c.post(f"/v1/approvals/{local.approval_id}/approve", headers=hdr)
        r_rej = c.post(f"/v1/approvals/{local.approval_id}/reject", headers=hdr)
        r_count = c.get("/v1/approvals/count", headers={"Authorization": f"Bearer {agent_bearer}"})
    assert r_list.status_code == 403
    assert r_appr.status_code == 403
    assert r_rej.status_code == 403
    assert r_count.status_code == 403
    assert local.adapter.rows == []  # nothing approved by a non-owner


# --- audit-invariant activation gate (lifespan): legacy unreceipted rows gate serving --------------


def _make_legacy_unreceipted(env, *, finalizing: bool) -> None:
    """Turn the seeded candidate into a pre-invariant row: approved + provenanced, NO receipt
    (the shape a pre-upgrade box hands the activation migration)."""
    from panella.client_raw import approve_queued_candidate

    approve_queued_candidate(env.config.outbox_db_path, env.approval_id)
    extra = (
        ", finalizer_state='finalizing', finalizer_worker_id='dead-worker',"
        " finalizer_claimed_at='2020-01-01T00:00:00+00:00'"
        if finalizing
        else ""
    )
    with sqlite3.connect(env.config.outbox_db_path) as conn:
        conn.execute(
            "UPDATE approval_queue SET approved_via='local_cli', approved_by='local_cli:owner'"
            f"{extra} WHERE id=?",
            (env.approval_id,),
        )


def test_activation_gate_refuses_box_when_backfill_undecidable(tmp_path, monkeypatch):
    """A crashed-mid-finalize legacy row + an unreachable store at boot: the lifespan must refuse
    the gated routes (503, reason names the activation) while /v1/health stays reachable."""
    env = _build(tmp_path, monkeypatch)
    _make_legacy_unreceipted(env, finalizing=True)

    class BrokenLookupAdapter(RecordingAdapter):
        def find_active_hash_by_marker(self, marker, tenant_id):
            raise RuntimeError("store unreachable")

    env.app.state.memory_adapter = BrokenLookupAdapter()  # lifespan reads this for the inspection
    with TestClient(env.app) as c:
        count = c.get("/v1/approvals/count", headers=_auth(env))
        health = c.get("/v1/health")
    assert count.status_code == 503
    assert "activation" in count.text
    assert health.status_code == 200  # live-but-refusing, never dark
    with sqlite3.connect(env.config.outbox_db_path) as conn:
        (seq,) = conn.execute(
            "SELECT audit_receipt_seq FROM approval_queue WHERE id=?", (env.approval_id,)
        ).fetchone()
    assert seq is None  # never attested blind


def test_activation_gate_backfills_then_serves(tmp_path, monkeypatch):
    """The happy upgrade: a plain legacy approved row is backfilled during the lifespan and the box
    serves normally, with the backfill receipt stamped and the chain intact."""
    env = _build(tmp_path, monkeypatch)
    _make_legacy_unreceipted(env, finalizing=False)
    with TestClient(env.app) as c:
        count = c.get("/v1/approvals/count", headers=_auth(env))
    assert count.status_code == 200
    with sqlite3.connect(env.config.outbox_db_path) as conn:
        (seq,) = conn.execute(
            "SELECT audit_receipt_seq FROM approval_queue WHERE id=?", (env.approval_id,)
        ).fetchone()
    assert seq is not None
    assert audit_verify_chain(env.config.audit_db_path) is True
