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


def _pretend_compose_up(monkeypatch, tmp_path):
    from panella.cli import init as init_cli

    # _compose_root now lives with the other compose helpers in panella.cli.init; the tokens defer
    # message imports it from there, so patch it on init_cli (where it is defined and looked up).
    monkeypatch.setattr(init_cli, "_compose_root", lambda: tmp_path)
    monkeypatch.setattr(init_cli, "_compose_service_running", lambda svc: True)


def test_cli_revoke_defers_to_container_when_compose_up(tmp_path, monkeypatch, capsys):
    """A bare host revoke (no --token-db) while panella-http is running must REFUSE rather than
    mutate the host DB and falsely report success while the container bearer stays live (Codex P2)."""
    _pretend_compose_up(monkeypatch, tmp_path)
    rc = main(["tokens", "revoke", "--label", "teammate-x"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to run against the host token DB" in err
    assert "docker compose exec -T panella-http panella tokens revoke --label=teammate-x" in err


def test_cli_mint_defers_to_container_when_compose_up(tmp_path, monkeypatch, capsys):
    """A bare host mint must not create a token in the non-serving host database, and the in-container
    remediation must PRESERVE the operator's --principal/--label (GH-bot P2) — following a command
    that dropped them would mint a root token with an auto label, leaving the requested one absent."""
    _pretend_compose_up(monkeypatch, tmp_path)
    host_token_db = tmp_path / "host-tokens.db"
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(host_token_db))

    rc = main(["tokens", "mint", "--principal", "human:alice", "--label", "teammate-x"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to run against the host token DB" in err
    # the exact requested options survive into the copy-paste remediation, not a bare `mint`
    assert (
        "docker compose exec -T panella-http panella tokens mint "
        "--principal=human:alice --label=teammate-x" in err
    )
    assert not host_token_db.exists()


def test_cli_mint_defer_without_options_stays_bare(tmp_path, monkeypatch, capsys):
    """With no explicit --principal/--label, the remediation is a bare `mint` (no injected auto
    defaults the operator never asked for)."""
    _pretend_compose_up(monkeypatch, tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "host-tokens.db"))

    rc = main(["tokens", "mint"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "panella tokens mint\n" in err  # bare, no trailing --principal/--label
    assert "--principal" not in err and "--label" not in err


def test_cli_mint_defer_shell_quotes_options(tmp_path, monkeypatch, capsys):
    """A label with spaces/metachars must be shell-quoted in the remediation command so the printed
    `docker compose exec … mint` is copy-paste-safe and not injectable-on-paste (terra/GH-bot P2)."""
    _pretend_compose_up(monkeypatch, tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "host-tokens.db"))

    rc = main(["tokens", "mint", "--label", "team x; rm -rf /"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--label='team x; rm -rf /'" in err  # equals-form + quoted as a single shell word
    assert "--label team x; rm -rf /" not in err  # never the raw, injectable interpolation


def test_cli_mint_defer_equals_form_survives_leading_dash(tmp_path, monkeypatch, capsys):
    """A valid label/principal beginning with '-' must bind via '=' so the pasted remediation parses
    (space-form would let argparse read -canary as another option) — GH-bot P3, completes the
    preserve-options fix for ALL free-text values."""
    _pretend_compose_up(monkeypatch, tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "host-tokens.db"))

    # the input itself must use '=' — argparse rejects `--label -canary` (the very misparse we guard)
    rc = main(["tokens", "mint", "--label=-canary"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "panella tokens mint --label=-canary" in err  # '=' binds the leading-dash value
    assert "--label -canary" not in err  # not the space-form that argparse would misparse


def test_cli_list_defers_to_container_when_compose_up(tmp_path, monkeypatch, capsys):
    """list is read-only but a bare host list can create/read the WRONG DB and mislead — same defer
    (Codex r2 P2)."""
    _pretend_compose_up(monkeypatch, tmp_path)
    rc = main(["tokens", "list"])
    assert rc == 2
    assert "docker compose exec -T panella-http panella tokens list" in capsys.readouterr().err


def test_cli_revoke_missing_db_does_not_materialize_it(tmp_path, capsys):
    token_db = tmp_path / "nonexistent.db"
    rc = main(["tokens", "revoke", "--label", "x", "--token-db", str(token_db)])
    assert rc == 2
    assert "no token database" in capsys.readouterr().err
    assert not token_db.exists()  # never created a phantom DB


def test_token_status_expired_beats_future_rotating():
    """A token with a past expires_at AND a future revoked_at (rotate grace) is rejected by the
    resolver as expired — list must say 'expired', not 'rotating' (Codex r2 P2)."""
    from datetime import UTC, datetime, timedelta

    from panella.cli.tokens import _token_status

    now = datetime.now(UTC)
    record = SimpleNamespace(
        revoked_at=now + timedelta(hours=1), expires_at=now - timedelta(hours=1), expired=True
    )
    assert _token_status(record).startswith("expired@")


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


def test_compose_root_matches_all_standard_filenames(tmp_path, monkeypatch):
    """The guard must find any of Compose's standard project files (not just docker-compose.yml) and
    honor COMPOSE_FILE, else a compose.yaml deployment bypasses the fail-closed path (GH-bot P2)."""
    from panella.cli.init import _compose_root

    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        d = tmp_path / name.replace(".", "_")
        d.mkdir()
        (d / name).write_text("services: {}\n")
        monkeypatch.chdir(d)
        assert _compose_root() == d, name

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    assert _compose_root() is None  # no compose file, no COMPOSE_FILE -> proceed
    monkeypatch.setenv("COMPOSE_FILE", "/some/explicit/compose.yaml")
    assert _compose_root() == empty  # explicit COMPOSE_FILE -> compose is configured
