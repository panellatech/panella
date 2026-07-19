"""Registry listing metadata stays in sync with the package version.

server.json feeds the MCP registry publish workflow; a version drift between
pyproject and server.json ships a registry entry pointing at a PyPI version
that may not exist (the registry validates the referenced version's README
ownership marker). These tests make drift a CI failure instead of a
publish-time surprise.
"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MCP_NAME = "io.github.panellatech/panella"


def _pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def _server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text())


def test_server_json_versions_match_pyproject():
    data = _server_json()
    version = _pyproject()["project"]["version"]
    assert data["version"] == version
    packages = data["packages"]
    assert len(packages) == 1
    assert packages[0]["version"] == version
    assert packages[0]["identifier"] == "panella"


def test_server_json_name_is_the_org_namespace():
    assert _server_json()["name"] == MCP_NAME


def test_readme_carries_the_ownership_marker():
    # The MCP registry validates this marker in the PyPI long_description of
    # the referenced version; pyproject must ship README.md as the readme and
    # the marker must sit on its own line (boundary rule: no trailing text).
    readme = (ROOT / "README.md").read_text()
    assert re.search(rf"^<!-- mcp-name: {re.escape(MCP_NAME)} -->$", readme, re.M)
    assert _pyproject()["project"].get("readme") == "README.md"
