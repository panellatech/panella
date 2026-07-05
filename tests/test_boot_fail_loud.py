from __future__ import annotations

import pytest

from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.http.app import PanellaBootConfigError, create_app
from panella.http.config import load_config


@pytest.fixture(autouse=True)
def _reset_governance():
    reset_governance_cache()
    yield
    reset_governance_cache()


def _render_profiles(tmp_path, monkeypatch):
    config_dir = tmp_path / "dist-config"
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(config_dir))
    render_distribution_config(current_governance(), config_dir)
    return config_dir


def test_create_app_fails_loud_when_config_dir_unrendered(tmp_path, monkeypatch):
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(tmp_path / "empty-config"))
    with pytest.raises(PanellaBootConfigError, match="config not rendered: run `panella-render-config --out"):
        create_app({"store_path": tmp_path / "store.db"})


def test_create_app_fails_loud_when_http_profile_missing(tmp_path, monkeypatch):
    _render_profiles(tmp_path, monkeypatch)
    with pytest.raises(PanellaBootConfigError) as exc_info:
        create_app({"profile_name": "missing-profile", "store_path": tmp_path / "store.db"})
    message = str(exc_info.value)
    assert "missing-profile" in message
    assert "valid:" in message
    assert "panella-render-config --out" in message


def test_create_app_fails_loud_when_mcp_profile_missing(tmp_path, monkeypatch):
    _render_profiles(tmp_path, monkeypatch)
    with pytest.raises(PanellaBootConfigError) as exc_info:
        create_app(
            {
                "profile_name": "serving",
                "mcp_enabled": True,
                "mcp_profile": "missing-mcp",
                "store_path": tmp_path / "store.db",
            }
        )
    message = str(exc_info.value)
    assert "missing-mcp" in message
    assert "valid:" in message
    assert "panella-render-config --out" in message


def test_create_app_fails_loud_when_governance_overlay_missing(tmp_path, monkeypatch):
    missing = tmp_path / "missing-governance.yaml"
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(missing))
    with pytest.raises(PanellaBootConfigError) as exc_info:
        create_app({"store_path": tmp_path / "store.db"})
    message = str(exc_info.value)
    assert "governance config error" in message
    assert str(missing) in message


def test_create_app_fails_loud_when_governance_overlay_invalid(tmp_path, monkeypatch):
    invalid = tmp_path / "invalid-governance.yaml"
    invalid.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(invalid))
    with pytest.raises(PanellaBootConfigError) as exc_info:
        create_app({"store_path": tmp_path / "store.db"})
    message = str(exc_info.value)
    assert "governance config error" in message
    assert str(invalid) in message


def test_create_app_passes_after_rendered_config(tmp_path, monkeypatch):
    _render_profiles(tmp_path, monkeypatch)
    app = create_app(
        {"profile_name": "serving", "store_path": tmp_path / "store.db"},
        memory_adapter=object(),
    )
    assert app.state.config.profile_name == "serving"


def test_boot_fail_loud_on_malformed_profile_yaml(tmp_path, monkeypatch):
    """A malformed rendered profile YAML (yaml.YAMLError) must fail loud + actionable at boot, not
    an opaque traceback (GH bot #5 round-2: preflight must catch more than ValueError)."""
    _render_profiles(tmp_path, monkeypatch)
    serving_yaml = next((tmp_path / "dist-config").rglob("serving.yaml"))
    serving_yaml.write_text("broken: [unterminated\n:::not yaml", encoding="utf-8")
    with pytest.raises(PanellaBootConfigError) as exc:
        create_app({"profile_name": "serving", "store_path": tmp_path / "store.db"}, memory_adapter=object())
    assert "could not be loaded" in str(exc.value)


def test_default_http_profile_is_serving_for_documented_local_path(tmp_path, monkeypatch):
    """The documented local path (render config + PANELLA_CONFIG_DIR, no explicit PANELLA_HTTP_PROFILE)
    must boot: an unset profile resolves to the rendered 'serving' profile, not a phantom 'default'
    that the fail-loud preflight would abort on (GH bot #5 regression)."""
    _render_profiles(tmp_path, monkeypatch)
    monkeypatch.delenv("PANELLA_HTTP_PROFILE", raising=False)
    assert load_config().profile_name == "serving"
    app = create_app({"store_path": tmp_path / "store.db"}, memory_adapter=object())
    assert app.state.config.profile_name == "serving"
