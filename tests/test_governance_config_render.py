from __future__ import annotations

from pathlib import Path

import yaml

from panella.config_render import render_distribution_config
from panella.governance import load_governance
from panella.principal import default_subject_id, default_tenant_id, root_principal


def test_governance_defaults_and_config_copies_match():
    repo = Path(__file__).resolve().parents[1]
    assert (repo / "config" / "governance.yaml").read_text() == (repo / "panella" / "governance.yaml").read_text()
    g = load_governance()
    assert default_tenant_id() == g.identity.default_tenant_id
    assert default_subject_id() == g.identity.default_subject_id
    assert root_principal().id == "human:owner"


def test_render_distribution_profiles(tmp_path):
    written = render_distribution_config(load_governance(), tmp_path)
    assert written["wings"] == tmp_path / "wings.yaml"
    mcp_write = yaml.safe_load((tmp_path / "agents" / "mcp-write.yaml").read_text())
    serving = yaml.safe_load((tmp_path / "agents" / "serving.yaml").read_text())
    assert mcp_write["approval_required_for"] == ["*"]
    assert mcp_write["write_default"]["wing"] == "owner"
    assert serving["tenant_scope"] == ["t_owner_personal"]
