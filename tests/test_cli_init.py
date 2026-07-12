from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
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
from panella.approval_audit import ApprovalAuditContext
from panella.mcp_tools import McpToolContext, build_transport_if_approvable, list_tools
from panella.principal import default_tenant_id, root_principal
from panella.profile import AgentProfile

APPROVAL_TOKEN_PATH = Path(".panella/approval-token")
OVERLAY_PATH = Path(".panella/governance.yaml")
OWNER_BEARER_PATH = Path(".panella/owner-bearer")


def _overlay_doc(path: Path = OVERLAY_PATH) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _assert_overlay_shape(tmp_path: Path) -> None:
    # The overlay (yaml.safe_dump, parsed here) fixes the canonical approver and carries the local_cli
    # transport pointed at the token init wrote. Off the compose path the token_file is the absolute
    # host path the server actually reads (no /app/local remap). A FRESH box writes approval-only —
    # identity is supplied by the generic base config at load time — so identity is NOT asserted here;
    # the custom-identity test asserts it is PRESERVED when an existing overlay carried one.
    doc = _overlay_doc()
    assert doc["schema_version"] == 1
    approval = doc["approval"]
    assert approval["authorized_approvers"] == ["local_cli:owner"]
    transport = approval["transport"]
    assert transport["kind"] == "local_cli"
    assert transport["config"]["token_mode"] == "0600"
    assert transport["config"]["token_file"] == str((tmp_path / APPROVAL_TOKEN_PATH).resolve())


def _assert_compose_overlay_shape() -> None:
    doc = _overlay_doc()
    approval = doc["approval"]
    assert approval["authorized_approvers"] == ["local_cli:owner"]
    transport = approval["transport"]
    assert transport["kind"] == "local_cli"
    assert transport["config"]["token_mode"] == "0600"
    assert transport["config"]["token_file"] == "/app/local/approval-token"


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


def test_init_native_happy_path_owner_bearer_and_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    token_db = tmp_path / "tokens.db"
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(token_db))

    rc = main(["init", "--yes"])
    captured = capsys.readouterr()
    owner_bearer = _first_bearer(captured.out)
    approval_token = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip()

    assert rc == 0
    assert TokenStore(token_db).resolve(owner_bearer, touch=False).principal_id == root_principal().id
    assert len(approval_token) == 64
    assert stat.S_IMODE(APPROVAL_TOKEN_PATH.stat().st_mode) == 0o600
    assert OWNER_BEARER_PATH.read_text(encoding="utf-8").strip() == owner_bearer
    assert stat.S_IMODE(OWNER_BEARER_PATH.stat().st_mode) == 0o600
    _assert_overlay_shape(tmp_path)
    assert "operator secret \u2014 never paste into agent config" in captured.out
    assert "not recoverable" in captured.err
    assert "saved to .panella/owner-bearer (0600)" in captured.err
    assert not Path(".env").exists()

    rc = main(["init", "--force", "--yes"])
    captured = capsys.readouterr()
    forced_bearer = _first_bearer(captured.out)
    assert rc == 0
    assert forced_bearer != owner_bearer
    assert TokenStore(token_db).resolve(owner_bearer, touch=False) is not None
    assert TokenStore(token_db).resolve(forced_bearer, touch=False) is not None
    assert APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip() != approval_token
    assert OWNER_BEARER_PATH.read_text(encoding="utf-8").strip() == forced_bearer
    assert stat.S_IMODE(APPROVAL_TOKEN_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(OWNER_BEARER_PATH.stat().st_mode) == 0o600
    _assert_overlay_shape(tmp_path)
    assert "previously minted bearers remain valid" in captured.err
    assert "--force" in captured.err


def test_init_compose_fresh_one_shot_writes_env_restarts_and_verifies(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    Path(".env").write_bytes(b"PANELLA_API_KEY=abc\n# keep me\nNO_NEWLINE=1")
    calls = _install_compose_harness(monkeypatch)

    rc = main(["init", "--yes"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.splitlines()[0] == "m2_compose_owner"
    assert calls["mint"] == [root_principal().id]
    assert calls["up"] == [True]
    assert calls["verify"] == [init_cli.DEFAULT_BASE_URL]
    assert stat.S_IMODE(APPROVAL_TOKEN_PATH.stat().st_mode) == 0o600
    assert stat.S_IMODE(OWNER_BEARER_PATH.stat().st_mode) == 0o600
    assert OWNER_BEARER_PATH.read_text(encoding="utf-8").strip() == "m2_compose_owner"
    _assert_compose_overlay_shape()
    assert Path(".env").read_bytes() == (
        b"PANELLA_API_KEY=abc\n# keep me\nNO_NEWLINE=1\n"
        b"PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml\n"
        b"PANELLA_MCP_PROFILE=mcp-write\n"
    )
    assert "panella init: write-mode is active" in captured.out
    assert "panella connect --print claude-code" in captured.out
    assert "approval token is operator-only" in captured.out
    assert "saved to .panella/owner-bearer (0600)" in captured.err


def test_init_compose_converge_never_mints_and_env_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    _write_provisioned_files(owner_bearer="m2_existing_owner")
    Path(".env").write_text(
        "PANELLA_API_KEY=abc\n"
        "export PANELLA_MCP_PROFILE=mcp-read\n"
        "# keep\n"
        "PANELLA_MCP_PROFILE=mcp-read-stale\n",
        encoding="utf-8",
    )
    os.chmod(".env", 0o644)
    approval_before = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8")
    overlay_before = OVERLAY_PATH.read_text(encoding="utf-8")
    os.utime(APPROVAL_TOKEN_PATH, (1234567890, 1234567890))
    os.utime(OVERLAY_PATH, (1234567890, 1234567890))
    approval_stat = APPROVAL_TOKEN_PATH.stat()
    overlay_stat = OVERLAY_PATH.stat()
    calls = _install_compose_harness(monkeypatch, verify_rc=0)

    assert main(["init", "--yes"]) == 0
    first = capsys.readouterr()
    first_env = Path(".env").read_bytes()
    assert main(["init", "--yes"]) == 0
    second = capsys.readouterr()

    assert calls["mint"] == []
    assert calls["up"] == [True, True]
    assert calls["verify"] == [init_cli.DEFAULT_BASE_URL, init_cli.DEFAULT_BASE_URL]
    assert APPROVAL_TOKEN_PATH.read_text(encoding="utf-8") == approval_before
    assert OVERLAY_PATH.read_text(encoding="utf-8") == overlay_before
    assert APPROVAL_TOKEN_PATH.stat().st_mtime == approval_stat.st_mtime
    assert OVERLAY_PATH.stat().st_mtime == overlay_stat.st_mtime
    assert Path(".env").read_bytes() == first_env
    assert stat.S_IMODE(Path(".env").stat().st_mode) == 0o644
    assert first_env == (
        b"PANELLA_API_KEY=abc\n"
        b"PANELLA_MCP_PROFILE=mcp-write\n"
        b"# keep\n"
        b"PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml\n"
    )
    assert "already provisioned \u2014 converged" in first.out
    assert "already provisioned \u2014 converged" in second.out


def test_init_compose_legacy_converge_missing_owner_bearer_warns_without_mint(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    _write_provisioned_files(owner_bearer=None)
    calls = _install_compose_harness(monkeypatch)

    rc = main(["init", "--yes"])
    captured = capsys.readouterr()

    assert rc == 0
    assert calls["mint"] == []
    assert not OWNER_BEARER_PATH.exists()
    assert "panella connect cannot auto-read the bearer" in captured.err
    assert "docker compose exec -T panella-http panella tokens mint --principal <root>" in captured.err
    assert ".panella/owner-bearer" in captured.err
    assert "panella init --force" in captured.err


@pytest.mark.parametrize(
    "present",
    [
        {"approval"},
        {"overlay"},
        {"owner"},
        {"approval", "owner"},
        {"overlay", "owner"},
    ],
)
def test_init_partial_panella_state_refuses_without_side_effects(tmp_path, monkeypatch, capsys, present):
    monkeypatch.chdir(tmp_path)
    Path("docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    Path(".panella").mkdir()
    if "approval" in present:
        APPROVAL_TOKEN_PATH.write_text("approval\n", encoding="utf-8")
    if "overlay" in present:
        OVERLAY_PATH.write_text("schema_version: 1\n", encoding="utf-8")
    if "owner" in present:
        OWNER_BEARER_PATH.write_text("m2_owner\n", encoding="utf-8")
    calls = _install_compose_harness(monkeypatch)

    rc = main(["init", "--yes"])
    captured = capsys.readouterr()

    assert rc == 2
    assert calls["mint"] == []
    assert calls["up"] == []
    assert calls["verify"] == []
    assert not Path(".env").exists()
    assert "partial .panella state" in captured.err
    assert "found:" in captured.err
    assert "missing:" in captured.err
    assert "--force" in captured.err
    assert "manually clean up" in captured.err


def test_init_prompt_matrix_and_stdout_first_line(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "tokens.db"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "")

    assert main(["init"]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("[Y/n]") == 1
    assert captured.out.splitlines()[0].startswith("m2_")

    next_dir = tmp_path / "abort"
    next_dir.mkdir()
    monkeypatch.chdir(next_dir)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(next_dir / "tokens.db"))
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert main(["init"]) == 1
    captured = capsys.readouterr()
    assert captured.err.count("[Y/n]") == 1
    assert "aborted" in captured.err
    assert not Path(".panella").exists()

    non_tty_dir = tmp_path / "non-tty"
    non_tty_dir.mkdir()
    monkeypatch.chdir(non_tty_dir)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["init"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "stdin is not a TTY; pass --yes to run non-interactively\n"
    assert not Path(".panella").exists()

    yes_dir = tmp_path / "yes"
    yes_dir.mkdir()
    monkeypatch.chdir(yes_dir)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(yes_dir / "tokens.db"))
    assert main(["init", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "[Y/n]" not in captured.err
    assert captured.out.splitlines()[0].startswith("m2_")


def test_init_compose_restart_failure_skips_verify(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    calls = _install_compose_harness(monkeypatch, up_ok=False)

    rc = main(["init", "--yes"])
    captured = capsys.readouterr()

    assert rc == 2
    assert calls["mint"] == [root_principal().id]
    assert calls["up"] == [True]
    assert calls["verify"] == []
    assert captured.out.splitlines()[0] == "m2_compose_owner"
    assert "PASS" not in captured.out
    assert "docker compose up -d --wait failed" in captured.err


def test_init_compose_verify_failure_prints_activation_banner(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    _install_compose_harness(monkeypatch, verify_rc=2, verify_lines=["FAIL /v1/health expected 200, got 0"])

    rc = main(["init", "--yes"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "FAIL /v1/health expected 200" in captured.out
    assert "activation applied but verification FAILED; the box may be write-enabled but unusable" in captured.err
    assert "set PANELLA_MCP_PROFILE=mcp-read in .env" in captured.err
    assert "docker compose up -d --wait" in captured.err
    assert "container uid 10001 cannot read a host-owned 0600 token" in captured.err


def test_compose_up_forces_activation_env_over_shell_exports(tmp_path, monkeypatch):
    # Compose gives a caller's shell exports precedence over .env, so a stale
    # `export PANELLA_MCP_PROFILE=mcp-read` (the pre-one-shot QUICKSTART pattern) would override
    # the lines init just persisted and restart the box read-only again (GH-bot P2). The real
    # _run_compose_up_wait must force the activation values into the subprocess env.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_MCP_PROFILE", "mcp-read")
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", "/stale/host/governance.yaml")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(init_cli.subprocess, "run", fake_run)
    assert init_cli._run_compose_up_wait() is True
    assert captured["cmd"] == ["docker", "compose", "up", "-d", "--wait"]
    assert captured["env"] is not None
    assert captured["env"]["PANELLA_MCP_PROFILE"] == "mcp-write"
    assert captured["env"]["PANELLA_GOVERNANCE_OVERLAY"] == "/app/local/governance.yaml"


def test_compose_env_upsert_canonicalizes_duplicates_and_preserves_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_path = Path(".env")
    env_path.write_bytes(
        b"PANELLA_API_KEY=abc\n"
        b"  export PANELLA_GOVERNANCE_OVERLAY = /old\n"
        b"# comment\n"
        b"export PANELLA_MCP_PROFILE=mcp-read\n"
        b"PANELLA_MCP_PROFILE=stale\n"
    )
    os.chmod(env_path, 0o644)

    init_cli._upsert_compose_env(env_path)

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o644
    assert env_path.read_bytes() == (
        b"PANELLA_API_KEY=abc\n"
        b"PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml\n"
        b"# comment\n"
        b"PANELLA_MCP_PROFILE=mcp-write\n"
    )

    missing = tmp_path / "new.env"
    init_cli._upsert_compose_env(missing)
    assert stat.S_IMODE(missing.stat().st_mode) == 0o600
    assert missing.read_bytes() == (
        b"PANELLA_GOVERNANCE_OVERLAY=/app/local/governance.yaml\n"
        b"PANELLA_MCP_PROFILE=mcp-write\n"
    )


def test_init_never_prints_approval_token_and_connect_never_reads_it(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "tokens.db"))

    assert main(["init", "--yes"]) == 0
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
    monkeypatch.setenv("PANELLA_MCP_PROFILE", "mcp-write")  # the write-capability check reads this
    with _verify_http_from_test_client(env.app, monkeypatch) as base_url:
        rc = main(["init", "--verify", "--base-url", base_url])
    captured = capsys.readouterr()
    assert rc == 0
    assert "PASS /v1/health returned 200" in captured.out
    assert "PASS /mcp is mounted" in captured.out
    # The transport check now proves the token is actually loadable + stamps an authorized approver.
    assert "PASS approval transport is local_cli-approvable and stamps an authorized local_cli:owner" in captured.out
    assert "PASS MCP profile 'mcp-write' is write-capable" in captured.out
    assert "PASS approval token file exists with mode 0600" in captured.out
    assert "FAIL" not in captured.out


def test_init_verify_fails_on_read_only_mcp_profile(tmp_path, monkeypatch, capsys):
    # A box left at the compose-default mcp-read profile passes health / /mcp / transport / token
    # checks but cannot advertise memory.submit_candidate — Day-0's write step would silently fail.
    # --verify must catch it (Codex B1 P2).
    env = _build_mcp_app(tmp_path, monkeypatch, capsys)
    monkeypatch.setenv("PANELLA_MCP_PROFILE", "mcp-read")
    with _verify_http_from_test_client(env.app, monkeypatch) as base_url:
        rc = main(["init", "--verify", "--base-url", base_url])
    captured = capsys.readouterr()
    assert rc == 2
    assert "FAIL MCP profile 'mcp-read' is not write-capable" in captured.out
    assert "PANELLA_MCP_PROFILE=mcp-write" in captured.out


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


def test_approval_transport_remediation_distinguishes_native_and_compose(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    operator_dir = tmp_path / ".panella"
    operator_dir.mkdir()
    token_file = operator_dir / "approval-token"
    token_file.write_text("approval-token\n", encoding="utf-8")
    token_file.chmod(0o644)
    overlay = operator_dir / "governance.yaml"
    overlay.write_text(
        "schema_version: 1\n"
        "approval:\n"
        "  authorized_approvers: [local_cli:owner]\n"
        "  transport:\n"
        "    kind: local_cli\n"
        "    config:\n"
        f"      token_file: {token_file}\n"
        '      token_mode: "0600"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))

    # The remediation WORDING follows the caller's known vantage, not a filesystem guess (terra P2):
    # native path → fix the host token file's own permissions.
    native_passed, native_message = init_cli._check_approval_transport(under_compose=False)

    assert native_passed is False
    assert "native token file" in native_message
    assert str(token_file) in native_message
    assert "uid 10001" not in native_message

    # container vantage (dispatched via --verify-transport) → the container uid must read the mount.
    compose_passed, compose_message = init_cli._check_approval_transport(under_compose=True)

    assert compose_passed is False
    assert compose_message != native_message
    assert "under Docker" in compose_message
    assert "uid 10001" in compose_message


def test_check_server_side_routes_modern_compose_yaml_into_container(tmp_path, monkeypatch):
    # GH-bot P2: server-side verification must route a compose deployment into the container for ANY
    # standard compose file (compose.yaml / COMPOSE_FILE), not only docker-compose.yml — else a
    # compose.yaml box silently runs the HOST checks and can uid-false-pass. With the service not
    # reachable it must FAIL LOUD about the compose project, never fall through to the native checks.
    # Before the fix (a bare docker-compose.yml existence check) a lone compose.yaml returned the two
    # native transport/mcp checks instead of this single compose-routing FAIL.
    monkeypatch.chdir(tmp_path)
    Path("compose.yaml").write_text("services: {}\n", encoding="utf-8")  # modern name, NOT docker-compose.yml
    monkeypatch.setattr(init_cli, "_compose_service_running", lambda svc: False)

    results = init_cli._check_server_side()

    assert len(results) == 1  # one compose-routing FAIL, not the 2 native checks
    passed, message = results[0]
    assert passed is False
    assert "compose project is present" in message  # recognized the modern compose file


def test_verify_transport_vantage_follows_dispatch_env_not_the_flag(tmp_path, monkeypatch, capsys):
    # terra P2: --verify-transport is hidden (argparse.SUPPRESS) but argparse still runs it directly
    # on a host. The container vantage must come from the explicit dispatch signal
    # (PANELLA_UNDER_COMPOSE=1, set by `docker compose exec -e …`), NOT from the flag — else a direct
    # native invocation mis-advises an unreadable token as a container-uid problem.
    monkeypatch.chdir(tmp_path)
    operator_dir = tmp_path / ".panella"
    operator_dir.mkdir()
    token_file = operator_dir / "approval-token"
    token_file.write_text("approval-token\n", encoding="utf-8")
    token_file.chmod(0o644)  # loose perms → unreadable-by-policy → FAIL carries the remediation
    overlay = operator_dir / "governance.yaml"
    overlay.write_text(
        "schema_version: 1\n"
        "approval:\n"
        "  authorized_approvers: [local_cli:owner]\n"
        "  transport:\n"
        "    kind: local_cli\n"
        "    config:\n"
        f"      token_file: {token_file}\n"
        '      token_mode: "0600"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    monkeypatch.setattr(init_cli, "_check_mcp_write_capable", lambda: (True, "mcp ok"))  # isolate the transport line

    # direct native invocation — no dispatch env → host wording
    monkeypatch.delenv("PANELLA_UNDER_COMPOSE", raising=False)
    assert main(["init", "--verify-transport"]) == 2
    native_out = capsys.readouterr().out
    assert "native token file" in native_out
    assert "uid 10001" not in native_out

    # dispatched into the container (env set by the exec) → container-uid wording
    monkeypatch.setenv("PANELLA_UNDER_COMPOSE", "1")
    assert main(["init", "--verify-transport"]) == 2
    container_out = capsys.readouterr().out
    assert "under Docker" in container_out and "uid 10001" in container_out


def test_compose_mint_binds_the_resolved_principal(tmp_path, monkeypatch):
    # On the compose path the owner bearer must be minted FOR the principal init resolved (a custom
    # identity from the host overlay), not the container's current default — else it is rejected once
    # /mcp requires the new root after restart (Codex B1 P2). Assert --principal is passed through.
    monkeypatch.setattr(init_cli, "_compose_service_running", lambda service: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="m2_minted_token\n", stderr="")

    monkeypatch.setattr(init_cli.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(init_cli.subprocess, "run", fake_run)
    token = init_cli._mint_in_running_compose("human:alice")
    assert token == "m2_minted_token"
    mint_cmd = calls[-1]
    assert "--principal" in mint_cmd
    assert mint_cmd[mint_cmd.index("--principal") + 1] == "human:alice"


def test_dockerignore_excludes_operator_secrets(tmp_path):
    # The image does `COPY . /app`, so .panella (approval token + overlay) and .env (API key) MUST be
    # excluded from the build context or they bake into image layers (Codex B1 P1). Assert the repo
    # ships a .dockerignore that excludes them.
    repo_root = Path(init_cli.__file__).resolve().parents[2]
    dockerignore = (repo_root / ".dockerignore").read_text(encoding="utf-8")
    assert ".panella/" in dockerignore
    assert ".env" in dockerignore


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
    # Customize identity via a base governance overlay (identity lives in governance, not an env).
    # Include NON-root_principal fields (default_tenant_id, owner_wing) — the sharp part of the P1 is
    # that init must carry forward the WHOLE identity block, not just root_principal, or a restart
    # reverts custom tenant/wing and can refuse a custom-tenant store or finalize under the wrong wing.
    base_overlay = tmp_path / "custom-identity.yaml"
    base_overlay.write_text(
        "identity:\n"
        "  root_principal:\n    id: \"human:alice\"\n    subject_id: \"u_alice\"\n"
        "  default_tenant_id: \"t_alice_custom\"\n"
        "  owner_wing: \"alice\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(base_overlay))
    reset_governance_cache()
    assert root_principal().id == "human:alice"  # precondition: the custom id is in effect

    assert main(["init", "--yes"]) == 0
    capsys.readouterr()
    doc = _overlay_doc()
    # The approver is the fixed literal regardless of the custom root id (NOT local_cli:alice).
    assert doc["approval"]["authorized_approvers"] == ["local_cli:owner"]
    # The WHOLE identity block is PRESERVED in init's overlay (Codex B1 P1): repointing
    # PANELLA_GOVERNANCE_OVERLAY at this file keeps root, tenant, AND wing custom. Writing
    # approval-only would have reverted all of them to the generic defaults on restart.
    assert doc["identity"]["root_principal"]["id"] == "human:alice"
    assert doc["identity"]["root_principal"]["subject_id"] == "u_alice"
    assert doc["identity"]["default_tenant_id"] == "t_alice_custom"
    assert doc["identity"]["owner_wing"] == "alice"

    # Prove it end-to-end: load governance from init's overlay ALONE and confirm root is still alice
    # AND the token would be accepted (transport stamps local_cli:owner, which is authorized).
    from panella.approval_transport import LocalCliApprovalTransport
    from panella.governance import load_governance

    merged = load_governance(overlay_path=str(tmp_path / OVERLAY_PATH))
    assert merged.identity.root_principal.id == "human:alice"
    token = APPROVAL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    transport = LocalCliApprovalTransport(token_file=str(tmp_path / APPROVAL_TOKEN_PATH), token_mode=0o600)
    assert transport.verify_presser(token) in merged.approval.authorized_approvers


def test_init_warns_on_custom_identity_bearer_binding(tmp_path, monkeypatch, capsys):
    # For a custom identity/tenant, init's one-shot mint binds the bearer to the pre-overlay
    # principal/tenant, so init must WARN to re-mint after restart (Codex B1 P2) — it does not
    # pretend to atomically provision across a restart it cannot control.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "tokens.db"))
    base_overlay = tmp_path / "custom-identity.yaml"
    base_overlay.write_text(
        "identity:\n  root_principal:\n    id: \"human:alice\"\n    subject_id: \"u_alice\"\n"
        "  default_tenant_id: \"t_alice_custom\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(base_overlay))
    reset_governance_cache()
    assert main(["init", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "WARNING: a custom identity/tenant is configured" in captured.err
    assert "re-mint it with `panella tokens mint`" in captured.err


def test_init_generic_box_prints_no_custom_identity_warning(tmp_path, monkeypatch, capsys):
    # The launch-bar path (generic root + tenant) must stay quiet — no spurious warning.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "tokens.db"))
    reset_governance_cache()
    assert main(["init", "--yes"]) == 0
    captured = capsys.readouterr()
    assert "custom identity/tenant" not in captured.err


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
        approval_audit=ApprovalAuditContext(
            db_path=tmp_path / "audit.db",
            principal=root_principal(),
            tenant_accessed=default_tenant_id(),
            source="mcp",
        ),
    )
    tool_payloads = [tool.model_dump(mode="json") for tool in list_tools(ctx)]
    serialized = json.dumps(tool_payloads, sort_keys=True).lower()
    assert "read_file" not in serialized
    assert "filesystem" not in serialized
    assert '"path"' not in serialized


def _write_provisioned_files(*, owner_bearer: str | None) -> None:
    Path(".panella").mkdir(exist_ok=True)
    APPROVAL_TOKEN_PATH.write_text("approval-secret\n", encoding="utf-8")
    OVERLAY_PATH.write_text("schema_version: 1\n", encoding="utf-8")
    if owner_bearer is not None:
        OWNER_BEARER_PATH.write_text(f"{owner_bearer}\n", encoding="utf-8")


def _install_compose_harness(monkeypatch, *, verify_rc: int = 0, up_ok: bool = True, verify_lines: list[str] | None = None):
    calls = {"mint": [], "up": [], "verify": []}
    monkeypatch.setattr(init_cli.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(init_cli, "_compose_service_running", lambda service: service == init_cli.COMPOSE_SERVICE)

    def fake_mint(principal_id: str, *, compose_present: bool) -> str:
        assert compose_present
        calls["mint"].append(principal_id)
        return "m2_compose_owner"

    def fake_up() -> bool:
        calls["up"].append(True)
        if not up_ok:
            print("panella init: docker compose up -d --wait failed", file=sys.stderr)
        return up_ok

    def fake_verify(base_url: str) -> int:
        calls["verify"].append(base_url)
        for line in verify_lines or ["PASS /v1/health returned 200"]:
            print(line)
        return verify_rc

    monkeypatch.setattr(init_cli, "_mint_owner_bearer", fake_mint)
    monkeypatch.setattr(init_cli, "_run_compose_up_wait", fake_up)
    monkeypatch.setattr(init_cli, "_verify", fake_verify)
    return calls


def _build_mcp_app(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(tmp_path / "init-tokens.db"))
    assert main(["init", "--yes"]) == 0
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
