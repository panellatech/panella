"""``panella tokens revoke`` / ``list`` — CLI wiring + end-to-end enforcement.

The revocation ENFORCEMENT already existed and is single-sourced: ``resolve_bearer``
(panella/http/auth.py) rejects any token whose ``revoked_at`` is set, and BOTH the /v1 REST
middleware and the /mcp mount call that one resolver. These tests prove (a) that shared resolver
rejects a revoked token, (b) the new CLI actually reaches it end-to-end on /v1 AND /mcp, and
(c) ``list`` never leaks a token value.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from panella.cli import main
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.http.app import create_app
from panella.http.auth import resolve_bearer
from panella.http.config import MemoryHttpConfig
from panella.http.errors import ApiError
from panella.http.tokens import TokenStore
from panella.principal import root_principal


def _write_serving_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT)")
    conn.execute("INSERT INTO memories VALUES ('seed','seed','status:active,tenant:t_owner_personal','{}',NULL)")
    conn.commit()
    conn.close()


def _serving_app(tmp_path, monkeypatch) -> SimpleNamespace:
    config_dir = tmp_path / "dist-config"
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(config_dir))
    reset_governance_cache()
    render_distribution_config(current_governance(), config_dir)

    store_path = tmp_path / "sqlite_vec.db"
    _write_serving_store(store_path)
    token_db = tmp_path / "app-tokens.db"
    config = MemoryHttpConfig(
        token_db_path=token_db,
        audit_db_path=tmp_path / "audit.db",
        outbox_db_path=tmp_path / "outbox.db",
        profile_name="serving",
        store_path=store_path,
        mcp_enabled=True,
        mcp_profile="mcp-write",
    )
    app = create_app(config, memory_adapter=_RecordingAdapter())
    bearer = app.state.token_store.mint(principal_id=root_principal().id, label="owner-e2e")
    return SimpleNamespace(app=app, token_db=token_db, bearer=bearer)


class _RecordingAdapter:
    """Minimal adapter — auth is enforced in middleware BEFORE any route touches the adapter, so a
    revoked-token 403 fires without the adapter ever being called."""

    def search(self, *a, **k):
        return []

    def health(self):
        return {"status": "ok"}


def _auth(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


# --- the single-sourced enforcement both surfaces share ---------------------------------------

def test_shared_resolver_rejects_revoked_bearer(tmp_path):
    store = TokenStore(tmp_path / "t.db")
    token = store.mint(principal_id=root_principal().id, label="victim")
    assert resolve_bearer(store, f"Bearer {token}") is not None  # valid before revoke

    assert store.revoke("victim") is True

    with pytest.raises(ApiError) as exc_info:
        resolve_bearer(store, f"Bearer {token}")
    assert exc_info.value.status_code == 403
    assert exc_info.value.code == "revoked_token"


# --- end-to-end: CLI revoke reaches the enforcement on BOTH surfaces ---------------------------

def test_cli_revoke_then_v1_rejects(tmp_path, monkeypatch, capsys):
    env = _serving_app(tmp_path, monkeypatch)
    with TestClient(env.app, base_url="http://127.0.0.1") as client:
        before = client.get("/v1/approvals/count", headers=_auth(env.bearer))
        # Strong precondition: the live bearer doesn't just pass auth, it reaches the route and
        # serves 200 — so the after-403 is proven to be the revoke, not a pre-existing gate.
        assert before.status_code == 200

        rc = main(["tokens", "revoke", "--label", "owner-e2e", "--token-db", str(env.token_db)])
        assert rc == 0
        assert "revoked owner-e2e" in capsys.readouterr().out

        after = client.get("/v1/approvals/count", headers=_auth(env.bearer))
    assert after.status_code == 403
    assert after.json()["code"] == "revoked_token"


def test_cli_revoke_then_mcp_rejects(tmp_path, monkeypatch):
    env = _serving_app(tmp_path, monkeypatch)
    with TestClient(env.app, base_url="http://127.0.0.1") as client:
        before = client.get("/mcp", headers=_auth(env.bearer))
        # The live owner bearer clears the /mcp auth+serving gates (not 401/403/503) before revoke.
        assert before.status_code not in (401, 403, 503)

        rc = main(["tokens", "revoke", "--label", "owner-e2e", "--token-db", str(env.token_db)])
        assert rc == 0

        after = client.get("/mcp", headers=_auth(env.bearer))
    assert after.status_code == 403
    assert after.json()["code"] == "revoked_token"


def test_cli_revoke_overrides_rotate_grace_window(tmp_path, monkeypatch):
    """A token in a rotate() grace window (future revoked_at) must be IMMEDIATELY killed by an
    operator revoke — not left valid until the grace expires. The COALESCE form silently preserved
    the future timestamp and reported false success (Codex P1)."""
    env = _serving_app(tmp_path, monkeypatch)
    # Put the owner bearer into a 5-min grace window: still valid now, auto-revokes later.
    TokenStore(env.token_db).rotate("owner-e2e", grace_seconds=300)
    with TestClient(env.app, base_url="http://127.0.0.1") as client:
        during_grace = client.get("/v1/approvals/count", headers=_auth(env.bearer))
        assert during_grace.status_code == 200  # grace window: the old bearer is STILL valid

        assert main(["tokens", "revoke", "--label", "owner-e2e", "--token-db", str(env.token_db)]) == 0

        after_v1 = client.get("/v1/approvals/count", headers=_auth(env.bearer))
        after_mcp = client.get("/mcp", headers=_auth(env.bearer))
    assert after_v1.status_code == 403 and after_v1.json()["code"] == "revoked_token"
    assert after_mcp.status_code == 403 and after_mcp.json()["code"] == "revoked_token"


def test_cli_revoke_defers_to_container_when_compose_up(tmp_path, monkeypatch, capsys):
    """A bare host revoke (no --token-db) while panella-http is running must REFUSE rather than
    mutate the host DB and falsely report success while the container bearer stays live (Codex P2)."""
    from panella.cli import tokens as tokens_cli

    monkeypatch.setattr(tokens_cli, "_compose_http_running", lambda: True)
    rc = main(["tokens", "revoke", "--label", "teammate-x"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to revoke against the host token DB" in err
    assert "docker compose exec -T panella-http panella tokens revoke --label teammate-x" in err


# --- CLI behavior contracts --------------------------------------------------------------------

def test_cli_revoke_is_idempotent(tmp_path, capsys):
    token_db = tmp_path / "t.db"
    TokenStore(token_db).mint(principal_id=root_principal().id, label="dup")

    def _revoked_at():
        return next(r for r in TokenStore(token_db).list() if r.label == "dup").revoked_at

    assert main(["tokens", "revoke", "--label", "dup", "--token-db", str(token_db)]) == 0
    first_revoked_at = _revoked_at()
    assert first_revoked_at is not None

    capsys.readouterr()
    assert main(["tokens", "revoke", "--label", "dup", "--token-db", str(token_db)]) == 0  # again
    # COALESCE keeps the original timestamp — re-revoking is a no-op on the value, still exit 0.
    assert _revoked_at() == first_revoked_at


def test_cli_revoke_unknown_label_exits_2(tmp_path, capsys):
    token_db = tmp_path / "t.db"
    TokenStore(token_db).mint(principal_id=root_principal().id, label="present")
    rc = main(["tokens", "revoke", "--label", "absent", "--token-db", str(token_db)])
    assert rc == 2
    assert "no token with label 'absent'" in capsys.readouterr().err


def test_cli_list_never_prints_token_values_and_shows_status(tmp_path, capsys):
    token_db = tmp_path / "t.db"
    store = TokenStore(token_db)
    tok_a = store.mint(principal_id=root_principal().id, label="alpha")
    tok_b = store.mint(principal_id=root_principal().id, label="beta")
    from panella.http.tokens import token_sha256

    assert main(["tokens", "list", "--token-db", str(token_db)]) == 0
    out = capsys.readouterr().out
    # the operator-facing handles are present...
    assert "alpha" in out and "beta" in out and "active" in out
    # ...but NO token value or digest is ever printed.
    assert tok_a not in out and tok_b not in out
    assert token_sha256(tok_a) not in out and token_sha256(tok_b) not in out

    main(["tokens", "revoke", "--label", "alpha", "--token-db", str(token_db)])
    capsys.readouterr()
    assert main(["tokens", "list", "--token-db", str(token_db)]) == 0
    out2 = capsys.readouterr().out
    assert "revoked@" in out2  # alpha now shows a revoked status
