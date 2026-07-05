from __future__ import annotations

import pytest

from panella.client import MemoryClient, TenantIsolationError
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.principal import principal_default_for_profile
from panella.profile import AgentProfile


class TenantAdapter:
    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        rows = [
            {"id": "foreign", "content": "foreign", "wing": "owner", "room": "preferences", "tenant_id": "t_foreign", "score": 1.0, "tags": ["status:active"]},
            {"id": "owner", "content": "owner", "wing": "owner", "room": "preferences", "tenant_id": "t_owner_personal", "score": 1.0, "tags": ["status:active"]},
        ]
        if tenant_ids is not None:
            rows = [row for row in rows if row["tenant_id"] in set(tenant_ids)]
        return rows[:k]


class LeakyTenantAdapter(TenantAdapter):
    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        return [
            {"id": "foreign", "content": "foreign", "wing": "owner", "room": "preferences", "tenant_id": "t_foreign", "score": 1.0, "tags": ["status:active"]},
        ]


def _render_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(tmp_path / "dist-config"))
    reset_governance_cache()
    render_distribution_config(current_governance(), tmp_path / "dist-config")


def test_search_filters_to_profile_tenant_scope(tmp_path, monkeypatch):
    _render_profiles(tmp_path, monkeypatch)
    profile = AgentProfile.load("serving")
    client = MemoryClient(profile, principal_default_for_profile(profile), adapter=TenantAdapter())
    hits = client.search("anything")
    assert [h["id"] for h in hits] == ["owner"]


def test_tenant_prefilter_failure_is_loud(tmp_path, monkeypatch):
    _render_profiles(tmp_path, monkeypatch)
    profile = AgentProfile.load("serving")
    principal = principal_default_for_profile(profile)
    client = MemoryClient(profile, principal, adapter=LeakyTenantAdapter())
    with pytest.raises(TenantIsolationError):
        client.search("anything")
