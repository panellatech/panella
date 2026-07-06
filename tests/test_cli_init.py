from __future__ import annotations

import json
import re
import sqlite3
import stat
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from panella.cli import init as init_cli
from panella.cli import main
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.http.app import create_app
from panella.http.config import MemoryHttpConfig
from panella.http.tokens import TokenStore
from panella.mcp_tools import McpToolContext, build_transport_if_approvable, list_tools
from panella.principal import root_principal
from panella.profile import AgentProfile

APPROVAL_TOKEN_PATH = Path(".panella/approval-token")
OVERLAY_PATH = Path(".panella/governance.yaml")
EXPECTED_OVERLAY = (
    "schema_version: 1\n"
    "approval:\n"
    '  authorized_approvers: ["local_cli:owner"]\n'
    "  transport:\n"
    '    kind: "local_cli"\n'
    "    config:\n"
    '      token_file: "/app/local/approval-token"\n'
    '      token_mode: "0600"\n'
)


class RecordingAdapter:
    def __init__(self):
        self.rows = []

    def add_memory(self, wing, room, content, metadata, conversation_id=None):
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append({
            "id": mid,
            "content": content,
            "wing": wing,
            "room": room,
            "tenant_id": metadata.get("tenant_id"),
            "metadata": metadata,
            "score": 1.0,
            "tags": ["status:active"],
        })
        return mid

    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        hits = [row for row in self.rows if query.lower() in str(row["content"]).lower()]
        if tenant_ids is not None:
            hits = [row for row in hits if row.get("tenant_id") in set(tenant_ids)]
        return hits[:k]

    def find_active_hash_by_marker(self, marker, tenant_id):
        return None


@pytest.fixture(autouse=True)
def _reset_governance():
    reset_governance_cache()
    yield
    reset_governance_cache()


def test_init_happy_path_idempotency_and_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    token_db = tmp_path / "tokens.db"
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(token_db))

    rc = main(["init"])
    captured = capsys.readouterr()
    owner_bearer = _first_bearer(captured.out)
    approval_token = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip()

    assert rc == 0
    assert TokenStore(token_db).resolve(owner_bearer, touch=False).principal_id == root_principal().id
    assert len(approval_token) == 64
    assert stat.S_IMODE(APPROVAL_TOKEN_PATH.stat().st_mode) == 0o600
    assert OVERLAY_PATH.read_text(encoding="utf-8") == EXPECTED_OVERLAY
    assert "operator secret \u2014 never paste into agent config" in captured.out
    assert "not recoverable" in captured.err

    rc = main(["init"])
    captured = capsys.readouterr()
    second_bearer = _first_bearer(captured.out)
    assert rc == 2
    assert TokenStore(token_db).resolve(second_bearer, touch=False) is not None
    assert "already exists" in captured.err
    assert APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip() == approval_token
    assert OVERLAY_PATH.read_text(encoding="utf-8") == EXPECTED_OVERLAY

    rc = main(["init", "--force"])
    captured = capsys.readouterr()
    forced_bearer = _first_bearer(captured.out)
    assert rc == 0
    assert TokenStore(token_db).resolve(forced_bearer, touch=False) is not None
    assert APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip() != approval_token
    assert stat.S_IMODE(APPROVAL_TOKEN_PATH.stat().st_mode) == 0o600
    assert OVERLAY_PATH.read_text(encoding="utf-8") == EXPECTED_OVERLAY
    assert "--force" in captured.err


def test_init_never_prints_approval_token_and_connect_never_reads_it(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "tokens.db"))

    assert main(["init"]) == 0
    captured = capsys.readouterr()
    owner_bearer = _first_bearer(captured.out)
    approval_value = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    all_init_output = captured.out + captured.err
    assert approval_value not in all_init_output

    for client in ("claude-code", "claude-desktop", "cursor"):
        assert main(["connect", "--print", client, "--token", owner_bearer]) == 0
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert owner_bearer in output
        assert approval_value not in output
        assert str(APPROVAL_TOKEN_PATH) not in output
        assert str((tmp_path / APPROVAL_TOKEN_PATH).resolve()) not in output


def test_init_verify_passes_against_running_mcp_app(tmp_path, monkeypatch, capsys):
    env = _build_mcp_app(tmp_path, monkeypatch, capsys)
    with _verify_http_from_test_client(env.app, monkeypatch) as base_url:
        rc = main(["init", "--verify", "--base-url", base_url])
    captured = capsys.readouterr()
    assert rc == 0
    assert "PASS /v1/health returned 200" in captured.out
    assert "PASS /mcp is mounted" in captured.out
    assert "PASS approval transport is local_cli-approvable" in captured.out
    assert "PASS approval token file exists with 0600" in captured.out
    assert "FAIL" not in captured.out


def test_init_verify_fails_without_overlay_actionably(tmp_path, monkeypatch, capsys):
    env = _build_mcp_app(tmp_path, monkeypatch, capsys)
    with _verify_http_from_test_client(env.app, monkeypatch) as base_url:
        monkeypatch.delenv("PANELLA_GOVERNANCE_OVERLAY")
        reset_governance_cache()
        rc = main(["init", "--verify", "--base-url", base_url])
    captured = capsys.readouterr()
    assert rc == 2
    assert "FAIL approval transport is local_cli but 'local_cli:owner' is not authorized" in captured.out
    assert "point PANELLA_GOVERNANCE_OVERLAY" in captured.out


def test_secret_boundary_http_and_mcp_do_not_exfiltrate_operator_token(tmp_path, monkeypatch, capsys):
    env = _build_mcp_app(tmp_path, monkeypatch, capsys)
    approval_value = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    bearer = env.app.state.token_store.mint(principal_id=root_principal().id, label="boundary-owner")

    with TestClient(env.app) as client:
        headers = {"Authorization": f"Bearer {bearer}"}
        for path in [
            "/.panella/approval-token",
            "/.panella/governance.yaml",
            "/app/local/approval-token",
            "/app/local/governance.yaml",
            "/local/approval-token",
            "/local/governance.yaml",
        ]:
            response = client.get(path, headers=headers)
            assert response.status_code == 404
            assert approval_value not in response.text

    # Boundary proven: the HTTP routes and registered MCP tools cannot serve/read the operator
    # secret. This does not claim to protect against a process with direct host filesystem access.
    governance = current_governance()
    profile = AgentProfile.load("mcp-write")
    ctx = McpToolContext(
        client=object(),
        outbox_db_path=tmp_path / "outbox.db",
        profile=profile,
        governance=governance,
        transport=build_transport_if_approvable(governance),
    )
    tool_payloads = [tool.model_dump(mode="json") for tool in list_tools(ctx)]
    serialized = json.dumps(tool_payloads, sort_keys=True).lower()
    assert "read_file" not in serialized
    assert "filesystem" not in serialized
    assert '"path"' not in serialized


def _build_mcp_app(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "init-tokens.db"))
    assert main(["init"]) == 0
    capsys.readouterr()

    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(tmp_path / OVERLAY_PATH))
    config_dir = tmp_path / "dist-config"
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(config_dir))
    reset_governance_cache()
    render_distribution_config(current_governance(), config_dir)

    store_path = tmp_path / "sqlite_vec.db"
    _write_serving_store(store_path)
    config = MemoryHttpConfig(
        token_db_path=tmp_path / "app-tokens.db",
        audit_db_path=tmp_path / "audit.db",
        outbox_db_path=tmp_path / "outbox.db",
        profile_name="serving",
        store_path=store_path,
        mcp_enabled=True,
        mcp_profile="mcp-write",
    )
    app = create_app(config, memory_adapter=RecordingAdapter())
    return SimpleNamespace(app=app)


def _write_serving_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT)")
    conn.execute("INSERT INTO memories VALUES ('seed','seed','status:active,tenant:t_owner_personal','{}',NULL)")
    conn.commit()
    conn.close()


@contextmanager
def _verify_http_from_test_client(app, monkeypatch) -> Iterator[str]:
    with TestClient(app, base_url="http://127.0.0.1") as client:
        def request_status(url: str) -> tuple[int, str]:
            path = urllib.parse.urlparse(url).path
            response = client.get(path)
            return response.status_code, response.text

        monkeypatch.setattr(init_cli, "_request_status", request_status)
        yield "http://127.0.0.1"


def _first_bearer(output: str) -> str:
    match = re.search(r"^m2_[^\s]+$", output, flags=re.MULTILINE)
    assert match is not None
    return match.group(0)
