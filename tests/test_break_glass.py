from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from panella.audit import audit_verify_chain
from panella.break_glass import break_glass
from panella.client import MemoryClient
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.principal import (
    BreakGlassToken,
    Principal,
    default_subject_id,
    default_tenant_id,
    root_principal,
)
from panella.profile import AgentProfile


class _EmptyAdapter:
    def search_memories(self, *args, **kwargs):
        return []


def _render_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(tmp_path / "dist-config"))
    reset_governance_cache()
    render_distribution_config(current_governance(), tmp_path / "dist-config")


def test_break_glass_opens_yields_elevated_and_chains(tmp_path):
    db = tmp_path / "audit.sqlite"
    with break_glass("debug tenant", caller=root_principal(), audit_db_path=db, watchdog_config_path=tmp_path / "no-watchdog.conf") as principal:
        assert principal.tenant_id == "*"
        assert principal.root_flag is True
        assert principal.break_glass_token is not None
    # the open + close audit rows must leave a verifiable hash chain
    assert audit_verify_chain(db) is True


def test_nested_break_glass_keeps_chain_verifiable(tmp_path):
    db = tmp_path / "audit.sqlite"
    # nested by design (inner caller=outer) — cannot be a single combined `with`
    with break_glass("outer", caller=root_principal(), audit_db_path=db, watchdog_config_path=tmp_path / "no-watchdog.conf") as outer:  # noqa: SIM117
        with break_glass("inner", caller=outer, audit_db_path=db, watchdog_config_path=tmp_path / "no-watchdog.conf"):
            pass
    assert audit_verify_chain(db) is True


def test_break_glass_rejects_non_root_caller(tmp_path):
    non_root = Principal(
        id=f"agent:probe@{default_tenant_id()}",
        tenant_id=default_tenant_id(),
        subject_id=default_subject_id(),
        actor_kind="agent",
        scopes=frozenset({"memory.read"}),
        roles=frozenset({"agent_default"}),
    )
    with pytest.raises(PermissionError), break_glass("nope", caller=non_root, audit_db_path=tmp_path / "audit.sqlite"):
        pass


def test_client_rejects_expired_break_glass_token(tmp_path, monkeypatch):
    _render_profiles(tmp_path, monkeypatch)
    root = root_principal()
    expired = BreakGlassToken(
        reason="expired",
        issued_at=datetime.now(UTC) - timedelta(seconds=20),
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        audit_chain_prev_hash="0" * 64,
    )
    principal = Principal(
        id=root.id,
        tenant_id="*",
        subject_id=root.subject_id,
        actor_kind="human",
        scopes=frozenset({"*"}),
        roles=root.roles,
        root_flag=True,
        break_glass_token=expired,
    )
    client = MemoryClient(
        AgentProfile.load("serving"),
        principal,
        adapter=_EmptyAdapter(),
        audit_db_path=tmp_path / "audit.sqlite",
    )
    with pytest.raises(PermissionError, match="expired"):
        client.search("anything")
