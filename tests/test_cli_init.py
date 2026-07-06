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
# The overlay is emitted via yaml.safe_dump (not string interpolation), so assert on the PARSED
# structure, not exact bytes. The approver is the FIXED canonical literal the transport stamps.
EXPECTED_OVERLAY_DOC = {
    "schema_version": 1,
    "approval": {
        "authorized_approvers": ["local_cli:owner"],
        "transport": {
            "kind": "local_cli",
            "config": {"token_file": "/app/local/approval-token", "token_mode": "0600"},
        },
    },
}


def _overlay_doc(path: Path = OVERLAY_PATH) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


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
    assert _overlay_doc() == EXPECTED_OVERLAY_DOC
    assert "operator secret \u2014 never paste into agent config" in captured.out
    assert "not recoverable" in captured.err

    rc = main(["init"])
    captured = capsys.readouterr()
    assert rc == 2
    # The refused re-run has ZERO side effects (P2 fix — idempotency checked BEFORE mint): no bearer
    # is minted or printed, so the token DB does not accumulate orphan owner-scoped tokens and the
    # "already exists" message is not contradicted by a live token on stdout.
    assert re.search(r"^m2_[^\s]+$", captured.out, flags=re.MULTILINE) is None
    assert "already exists" in captured.err
    assert APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip() == approval_token
    assert _overlay_doc() == EXPECTED_OVERLAY_DOC

    rc = main(["init", "--force"])
    captured = capsys.readouterr()
    forced_bearer = _first_bearer(captured.out)
    assert rc == 0
    assert TokenStore(token_db).resolve(forced_bearer, touch=False) is not None
    assert APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip() != approval_token
    assert stat.S_IMODE(APPROVAL_TOKEN_PATH.stat().st_mode) == 0o600
    assert _overlay_doc() == EXPECTED_OVERLAY_DOC
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
    # The transport check now proves the token is actually loadable + stamps an authorized approver.
    assert "PASS approval transport is local_cli-approvable and stamps an authorized local_cli:owner" in captured.out
    assert "PASS approval token file exists with mode 0600" in captured.out
    assert "FAIL" not in captured.out


def test_init_verify_fails_without_overlay_actionably(tmp_path, monkeypatch, capsys):
    env = _build_mcp_app(tmp_path, monkeypatch, capsys)
    with _verify_http_from_test_client(env.app, monkeypatch) as base_url:
        monkeypatch.delenv("PANELLA_GOVERNANCE_OVERLAY")
        reset_governance_cache()
        rc = main(["init", "--verify", "--base-url", base_url])
    captured = capsys.readouterr()
    assert rc == 2
    # Without the operator's overlay the approval loop cannot work, and --verify must say so with an
    # actionable pointer (whichever branch fires: no local_cli transport, or a transport whose token
    # the effective server identity cannot load).
    assert "FAIL approval transport" in captured.out
    assert "PANELLA_GOVERNANCE_OVERLAY" in captured.out or "SELF_HOST" in captured.out


def test_init_written_approver_matches_transport_stamp_exactly(tmp_path, monkeypatch):
    # Drift-lock: the literal init writes into authorized_approvers MUST equal what the runtime
    # transport actually stamps for a valid presser. If either side ever changes, this fails loudly
    # — the exact divergence (GH-bot P2 / code-reviewer P1) that made a customized-identity box
    # inert while --verify false-passed.
    from panella.approval_transport import LocalCliApprovalTransport
    from panella.cli.init import LOCAL_CLI_APPROVER

    token_path = tmp_path / "tok"
    token_path.write_text("s3cret-token\n", encoding="utf-8")
    token_path.chmod(0o600)
    transport = LocalCliApprovalTransport(token_file=str(token_path), token_mode=0o600)
    assert transport.verify_presser("s3cret-token") == LOCAL_CLI_APPROVER


def test_init_custom_root_identity_still_produces_approvable_box(tmp_path, monkeypatch, capsys):
    # A box whose root_principal.id is customized (the documented advanced-onboarding path) must
    # STILL get a working approval loop: init writes the fixed canonical approver, not a derived
    # one, so the transport's stamp is authorized. Previously init derived local_cli:<custom>
    # (e.g. local_cli:alice) → inert-closed box + false-PASS verify.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "tokens.db"))
    # Customize the root identity via a base governance overlay (id lives in governance, not an env).
    base_overlay = tmp_path / "custom-identity.yaml"
    base_overlay.write_text(
        "identity:\n  root_principal:\n    id: \"human:alice\"\n    subject_id: \"u_alice\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(base_overlay))
    reset_governance_cache()
    assert root_principal().id == "human:alice"  # precondition: the custom id is in effect

    assert main(["init"]) == 0
    capsys.readouterr()
    doc = _overlay_doc()
    # The approver is the fixed literal regardless of the custom root id (NOT local_cli:alice).
    assert doc["approval"]["authorized_approvers"] == ["local_cli:owner"]

    # And the token the SERVER loads would be accepted: the transport stamps local_cli:owner, which
    # is in the set init wrote.
    from panella.approval_transport import LocalCliApprovalTransport

    token = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    transport = LocalCliApprovalTransport(token_file=str(tmp_path / APPROVAL_TOKEN_PATH), token_mode=0o600)
    assert transport.verify_presser(token) in doc["approval"]["authorized_approvers"]


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
