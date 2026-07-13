from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from panella.cli import main
from panella.cli import up as up_cli


def _asset(version: str = "0.2.0") -> bytes:
    return (up_cli.MANAGED_HEADER.format(version=version) + "\nservices: {}\n").encode()


@pytest.fixture
def harness(monkeypatch):
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(up_cli, "_embedded_compose", _asset)
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: "/usr/bin/docker")

    def run(command, *, timeout, env=None, cwd=None):
        calls.append((command, {"timeout": timeout, "env": env, "cwd": cwd}))
        if command == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "ps", "-aq"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "volume", "ls"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "compose", "-p"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == [sys.executable, "-c"] and command[-2:] == ["init", "--yes"]:
            operator = Path(cwd) / ".panella"
            (operator / "approval-token").write_text(f"approval-secret-{len(calls)}\n", encoding="utf-8")
            (operator / "governance.yaml").write_text("schema_version: 1\n", encoding="utf-8")
            (operator / "owner-bearer").write_text("owner-bearer-value\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(up_cli, "_run_capture", run)
    return calls


def test_t1_home_resolution_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    cases = [
        (str(tmp_path / "a"), str(tmp_path / "b"), (tmp_path / "a").resolve()),
        (None, str(tmp_path / "b"), (tmp_path / "b").resolve()),
        (None, None, (tmp_path / "panella-box").resolve()),
        ("rel/dir", None, (tmp_path / "rel/dir").resolve()),
        ("~/box", None, (tmp_path / "box").resolve()),
        (str(link), None, real.resolve()),
    ]
    for flag, env, expected in cases:
        if env is None:
            monkeypatch.delenv("PANELLA_HOME", raising=False)
        else:
            monkeypatch.setenv("PANELLA_HOME", env)
        resolved = up_cli._resolve_home(flag)
        assert resolved is not None
        assert resolved[0] == expected
        assert resolved[1] == "panella-box-" + hashlib.sha256(str(expected).encode()).hexdigest()[:8]
    assert up_cli._resolve_home("") is None
    monkeypatch.setenv("PANELLA_HOME", "")
    assert up_cli._resolve_home(None) is None
    # CLI-level: empty home exits 2 with ZERO side effects (nothing created anywhere under cwd/home)
    before = sorted(tmp_path.iterdir())
    assert main(["up", "--home", "", "--yes"]) == 2
    assert main(["up", "--yes"]) == 2  # PANELLA_HOME="" from above, no flag
    assert sorted(tmp_path.iterdir()) == before


def test_t2_compose_four_way_lstat_and_atomic_cleanup(tmp_path, monkeypatch, capsys):
    path = tmp_path / "docker-compose.yml"
    asset = _asset()
    assert up_cli._materialize_compose(path, asset)
    assert path.read_bytes() == asset
    assert up_cli._materialize_compose(path, asset)
    path.write_bytes(asset + b"# drift\n")
    assert not up_cli._materialize_compose(path, asset)
    assert "drift" in capsys.readouterr().err
    path.write_bytes(_asset("0.1.0"))  # managed header, older release → upgrade wording
    assert not up_cli._materialize_compose(path, _asset("0.2.0"))
    assert "UPGRADE.md" in capsys.readouterr().err
    path.write_text("services: {}\n", encoding="utf-8")
    assert not up_cli._materialize_compose(path, asset)
    assert "unmanaged" in capsys.readouterr().err
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    assert not up_cli._materialize_compose(path, asset)
    path.unlink()
    path.mkdir()
    assert not up_cli._materialize_compose(path, asset)
    path.rmdir()
    fifo = tmp_path / "compose-fifo"
    os.mkfifo(fifo)
    assert not up_cli._materialize_compose(fifo, asset)
    fifo.unlink()
    path.write_bytes(asset)
    uid = os.getuid()
    owner_patch = pytest.MonkeyPatch()
    owner_patch.setattr(up_cli.os, "getuid", lambda: uid + 1)
    try:
        assert not up_cli._materialize_compose(path, asset)
    finally:
        owner_patch.undo()
    path.unlink()
    monkeypatch.setattr(up_cli.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        up_cli._atomic_write(path, asset, 0o600)
    assert not path.exists() and not path.with_name(path.name + ".new").exists()


def test_t3_env_states_permissions_and_nodes(tmp_path, capsys):
    path = tmp_path / ".env"
    assert up_cli._upsert_env(path, "project")
    created = path.read_bytes()
    assert b"PANELLA_API_KEY=" in created
    assert f"PANELLA_UID={os.getuid()}\n".encode() in created  # container identity pinned to caller
    assert f"PANELLA_GID={os.getgid()}\n".encode() in created
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert up_cli._upsert_env(path, "project")
    assert path.read_bytes() == created
    path.write_text("PANELLA_API_KEY=\n", encoding="utf-8")
    assert not up_cli._upsert_env(path, "project")
    path.write_text("PANELLA_API_KEY=a\nPANELLA_API_KEY=b\n", encoding="utf-8")
    assert not up_cli._upsert_env(path, "project")
    path.write_text("OTHER=1", encoding="utf-8")  # key line missing, no trailing newline
    os.chmod(path, 0o600)
    assert up_cli._upsert_env(path, "project")
    appended = path.read_bytes()
    assert appended.startswith(b"OTHER=1\n")
    assert re.search(rb"(?m)^PANELLA_API_KEY=[0-9a-f]{64}$", appended)
    assert f"PANELLA_UID={os.getuid()}\n".encode() in appended
    assert f"PANELLA_GID={os.getgid()}\n".encode() in appended
    assert b"COMPOSE_PROJECT_NAME=project\n" in appended
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.write_text("OTHER=2\n", encoding="utf-8")  # loose mode + missing key: generated secret must not land loose
    os.chmod(path, 0o644)
    assert up_cli._upsert_env(path, "project")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # tightened, never loosened
    path.write_text("PANELLA_API_KEY=a\n", encoding="utf-8")  # companion-only append: no new secret
    os.chmod(path, 0o644)
    assert up_cli._upsert_env(path, "project")
    companion = path.read_bytes()
    assert f"PANELLA_UID={os.getuid()}\n".encode() in companion
    assert f"PANELLA_GID={os.getgid()}\n".encode() in companion
    assert b"COMPOSE_PROJECT_NAME=project\n" in companion
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # pre-existing exposure: WARN-only, mode preserved
    path.unlink()
    path.symlink_to(tmp_path / "target")
    assert not up_cli._upsert_env(path, "project")
    path.unlink()
    path.mkdir()
    assert not up_cli._upsert_env(path, "project")
    path.rmdir()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    assert not up_cli._upsert_env(fifo, "project")
    fifo.unlink()
    path.write_text("PANELLA_API_KEY=a\n", encoding="utf-8")
    os.chmod(path, 0o644)
    assert up_cli._upsert_env(path, "project")
    assert "group/other-readable" in capsys.readouterr().err
    uid = os.getuid()
    patch = pytest.MonkeyPatch()
    patch.setattr(up_cli.os, "getuid", lambda: uid + 1)
    try:
        assert not up_cli._upsert_env(path, "project")
    finally:
        patch.undo()


def test_t4_clone_detection_uses_caller_environment_before_child_env(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PANELLA_HOME", raising=False)
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    assert main(["up", "--yes"]) == 2
    assert "compose checkout" in capsys.readouterr().err
    # An explicitly designated home (env form, the release drill's shape) escapes the clone guard
    # and proceeds to docker preflight.
    monkeypatch.setenv("PANELLA_HOME", str(tmp_path / "elsewhere-box"))
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: None)
    assert main(["up", "--yes"]) == 2
    err = capsys.readouterr().err
    assert "compose checkout" not in err
    assert "docker command not found" in err
    monkeypatch.delenv("PANELLA_HOME", raising=False)
    (tmp_path / "docker-compose.yml").unlink()
    monkeypatch.setenv("COMPOSE_FILE", "/tmp/elsewhere.yml")
    assert main(["up", "--yes"]) == 2
    assert "compose checkout" in capsys.readouterr().err


def test_t5_missing_embedded_asset_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(up_cli, "_embedded_compose", lambda: None)
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(up_cli, "_run_capture", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert main(["up", "--home", str(tmp_path / "box"), "--yes"]) == 2
    assert "install a released version" in capsys.readouterr().err


def test_t6_never_opens_approval_token_and_reads_bearer_after_init_once(tmp_path, harness, monkeypatch, capsys):
    read_positions: list[int] = []
    original_text = Path.read_text
    original_bytes = Path.read_bytes

    def read_text(path, *args, **kwargs):
        if path.name == "approval-token":
            raise AssertionError("approval token must not be read")
        if path.name == "owner-bearer":
            read_positions.append(len(harness))  # how many subprocess calls had happened at read time
        return original_text(path, *args, **kwargs)

    def read_bytes(path):
        if path.name == "approval-token":
            raise AssertionError("approval token must not be read")
        return original_bytes(path)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert main(["up", "--home", str(tmp_path / "box"), "--yes"]) == 0
    captured = capsys.readouterr()
    assert "approval-secret" not in captured.out and "approval-secret" not in captured.err
    assert len(read_positions) == 1  # bearer read exactly once…
    init_indices = [i for i, call in enumerate(harness) if call[0][-2:] == ["init", "--yes"]]
    assert init_indices and init_indices[-1] < read_positions[0]  # …and only after child init succeeded


def test_t7_exit_code_table(tmp_path, monkeypatch, capsys):
    posix_patch = pytest.MonkeyPatch()  # non-POSIX install: up refuses, CLI import already survived
    posix_patch.setattr(up_cli, "fcntl", None)
    try:
        assert main(["up", "--home", str(tmp_path / "no-posix"), "--yes"]) == 2
        assert "requires a POSIX host" in capsys.readouterr().err
    finally:
        posix_patch.undo()
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: None)
    assert main(["up", "--home", str(tmp_path / "no-docker"), "--yes"]) == 2
    calls = []
    monkeypatch.setattr(up_cli, "_embedded_compose", _asset)
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: "/usr/bin/docker")

    def compose_failure(command, **kwargs):
        calls.append(command)
        if command == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] in (["docker", "ps", "-aq"], ["docker", "volume", "ls"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "compose", "-p"]:
            return SimpleNamespace(returncode=1, stdout="bad", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr(up_cli, "_run_capture", compose_failure)
    assert main(["up", "--home", str(tmp_path / "compose-fail"), "--yes"]) == 3

    # exit 1: interactive decline, before ANY side effect (home dir never created)
    monkeypatch.setattr(up_cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert main(["up", "--home", str(tmp_path / "declined")]) == 1
    assert not (tmp_path / "declined").exists()

    # exit 4: child init fails (exit 0 full-flow is locked by T6/T8 harness runs)
    def init_failure(command, **kwargs):
        if command == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] in (["docker", "ps", "-aq"], ["docker", "volume", "ls"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "compose", "-p"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-2:] == ["init", "--yes"]:
            return SimpleNamespace(returncode=2, stdout="", stderr="init exploded")
        raise AssertionError(command)

    monkeypatch.setattr(up_cli, "_run_capture", init_failure)
    assert main(["up", "--home", str(tmp_path / "init-fail"), "--yes"]) == 4


def test_t8_all_state_combinations_recovery_and_provisioned_activation(tmp_path, harness, monkeypatch, capsys):
    operator = tmp_path / ".panella"
    operator.mkdir()
    for mask in range(8):
        for file in operator.iterdir():
            file.unlink()
        for bit, name in enumerate(("approval-token", "governance.yaml", "owner-bearer")):
            if mask & (1 << bit):
                (operator / name).write_text("x", encoding="utf-8")
        expected = "provisioned" if mask in {3, 7} else "fresh" if mask == 0 else "partial"
        assert up_cli._operator_state(operator) == expected
    for file in operator.iterdir():
        file.unlink()
    (tmp_path / "docker-compose.yml").write_bytes(_asset())
    monkeypatch.setattr(up_cli, "_has_recovery_resources", lambda _: True)
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 2
    assert "restore .panella" in capsys.readouterr().err
    (tmp_path / "docker-compose.yml").unlink()  # compose-absent branch prints the POSIX snippet
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 2
    err = capsys.readouterr().err
    project = up_cli._resolve_home(str(tmp_path))[1]
    assert f"# WARNING: this DELETES the box data for project {project}" in err
    assert f"docker ps -a  --filter label=com.docker.compose.project={project}" in err
    assert f"docker volume ls --filter label=com.docker.compose.project={project}" in err
    assert f'ids=$(docker ps -aq --filter label=com.docker.compose.project={project}); [ -n "$ids" ] && docker rm -f $ids' in err
    assert f'vols=$(docker volume ls -q --filter label=com.docker.compose.project={project}); [ -n "$vols" ] && docker volume rm $vols' in err
    monkeypatch.setattr(up_cli, "_has_recovery_resources", lambda _: False)
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 0  # fresh + zero resources: no recovery wording
    err = capsys.readouterr().err
    assert "restore .panella" not in err and "DELETES" not in err
    for file in operator.iterdir():
        file.unlink()
    for name in ("approval-token", "governance.yaml"):
        (operator / name).write_text("x", encoding="utf-8")
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 0
    compose_calls = [call for call in harness if call[0][:3] == ["docker", "compose", "-p"]]
    assert compose_calls[-1][1]["env"]["PANELLA_MCP_PROFILE"] == "mcp-write"
    init_calls = [call for call in harness if call[0][-2:] == ["init", "--yes"]]
    assert init_calls, "child init was never spawned"
    for command, kwargs in init_calls:  # argv/env/cwd triple: PATH-independent shim, pinned box context
        assert command[:2] == [sys.executable, "-c"] and command[2] == up_cli._CHILD_SHIM
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"]["COMPOSE_FILE"] == str(tmp_path / "docker-compose.yml")
        assert kwargs["env"]["COMPOSE_PROJECT_NAME"] == up_cli._resolve_home(str(tmp_path))[1]
        assert "PANELLA_UNDER_COMPOSE" not in kwargs["env"]
    # partial state: exit 2 with ZERO compose side effects (only the docker-info preflight may run)
    for file in operator.iterdir():
        file.unlink()
    (operator / "approval-token").write_text("x", encoding="utf-8")
    before = len(harness)
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 2
    assert "partial" in capsys.readouterr().err
    assert not any(call[0][:2] == ["docker", "compose"] for call in harness[before:])


def test_t9_child_environment_scrubs_hostile_values_and_sets_compose_context(tmp_path, harness, monkeypatch):
    for key in (*up_cli._COMPOSE_ENV_SCRUB, *up_cli._CHILD_ENV_SCRUB):
        monkeypatch.setenv(key, "hostile")
    env = up_cli._child_env(project="project", compose_path=tmp_path / "docker-compose.yml")
    for key in (*up_cli._COMPOSE_ENV_SCRUB, *up_cli._CHILD_ENV_SCRUB):
        assert key not in env
    assert env["COMPOSE_PROJECT_NAME"] == "project"
    assert env["COMPOSE_FILE"] == str(tmp_path / "docker-compose.yml")
    assert "PANELLA_UID" in up_cli._CHILD_ENV_SCRUB and "PANELLA_GID" in up_cli._CHILD_ENV_SCRUB
    # up's own compose path scrubs the same set, then sets STAGE-INTENT values (§7) — the hostile
    # values must be gone either way; identity/key/vantage vars must be fully absent.
    compose_env = up_cli._compose_env(activated=False)
    for key in (*up_cli._COMPOSE_ENV_SCRUB, "PANELLA_API_KEY", "PANELLA_UNDER_COMPOSE", "PANELLA_UID", "PANELLA_GID"):
        assert key not in compose_env
    assert compose_env[up_cli.OVERLAY_ENV] == ""  # fresh start: empty overlay, not the hostile value
    assert compose_env[up_cli.MCP_PROFILE_ENV] == "mcp-read"
    # Full hostile-env flow: up succeeds AND never mutates its own os.environ (child copies only).
    snapshot = dict(os.environ)
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 0
    assert dict(os.environ) == snapshot


def test_t10_second_process_flock_is_refused(tmp_path, harness):
    lock = tmp_path / ".up.lock"
    locker = (
        "import fcntl, os, sys, time; "
        "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600); "
        "fcntl.flock(fd, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", locker, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "locked"
        assert main(["up", "--home", str(tmp_path), "--yes"]) == 2
        # The refusal happens at the lock, BEFORE .panella is created — the loser must not race the
        # lstat→mkdir window and raise FileExistsError (bot P2). Lock serializes operator-dir setup.
        assert not (tmp_path / ".panella").exists()
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_t11_provisioned_rerun_uses_activation_without_recreate(tmp_path, harness):
    box = tmp_path / "box"
    operator = box / ".panella"
    operator.mkdir(parents=True)
    (operator / "approval-token").write_text("x", encoding="utf-8")
    (operator / "governance.yaml").write_text("x", encoding="utf-8")
    (operator / "owner-bearer").write_text("owner-bearer-value\n", encoding="utf-8")
    assert main(["up", "--home", str(box), "--yes"]) == 0
    assert main(["up", "--home", str(box), "--yes"]) == 0
    compose = [entry for entry in harness if entry[0][:3] == ["docker", "compose", "-p"]]
    assert len(compose) == 2
    assert all(entry[1]["env"]["PANELLA_MCP_PROFILE"] == "mcp-write" for entry in compose)


@pytest.mark.parametrize(
    # locked_substring/remedy are INDEPENDENT literals (the §4 table's promise), deliberately not
    # derived from the constants — a drive-by rewrite of both constant and message must still fail.
    ("phase", "message", "locked_substring", "remedy", "expected"),
    [
        ("info", up_cli.MSG_TIMEOUT_DOCKER_INFO, "docker info timed out", "docker daemon", 2),
        ("compose", up_cli.MSG_TIMEOUT_COMPOSE_UP, "compose up timed out", " logs", 3),
        ("init", up_cli.MSG_TIMEOUT_INIT, "panella init timed out", "panella init --verify", 4),
    ],
)
def test_t12_timeouts_have_locked_messages_and_remedies(tmp_path, monkeypatch, capsys, phase, message, locked_substring, remedy, expected):
    monkeypatch.setattr(up_cli, "_embedded_compose", _asset)
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: "/usr/bin/docker")

    def timeout(command, **kwargs):
        if phase == "info" and command == ["docker", "info"]:
            raise subprocess.TimeoutExpired(command, 10)
        if command == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] in (["docker", "ps", "-aq"], ["docker", "volume", "ls"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if phase == "compose" and command[:3] == ["docker", "compose", "-p"]:
            raise subprocess.TimeoutExpired(command, 300)
        if command[:3] == ["docker", "compose", "-p"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if phase == "init" and command[:2] == [sys.executable, "-c"] and command[-2:] == ["init", "--yes"]:
            raise subprocess.TimeoutExpired(command, 600)
        raise AssertionError(command)

    monkeypatch.setattr(up_cli, "_run_capture", timeout)
    home = tmp_path / "space home"
    assert main(["up", "--home", str(home), "--yes"]) == expected
    error = capsys.readouterr().err
    assert locked_substring in message  # the constant carries the §4-locked promise
    assert message in error  # and the CLI emits the constant
    assert remedy in error  # remedy semantics present, asserted as an independent literal
    if phase == "compose":
        assert shlex.quote(str(home / "docker-compose.yml")) in error

def test_recovery_detection_is_quiet_mode_and_counts_single_resources(monkeypatch):
    # -aq/-q quiet output is format-immune (a user psFormat config drops the table header, which
    # made ONE surviving container/volume read as "none" under line counting). Any non-empty line
    # is a resource; the single-leftover case is exactly the bearer-DB recovery this gate guards.
    outputs: dict[str, str] = {}

    def run(command, *, timeout, env=None, cwd=None):
        assert command[:3] in (["docker", "ps", "-aq"], ["docker", "volume", "ls"])
        if command[:3] == ["docker", "volume", "ls"]:
            assert command[3] == "-q"
        key = "ps" if command[1] == "ps" else "volume"
        return SimpleNamespace(returncode=0, stdout=outputs.get(key, ""), stderr="")

    monkeypatch.setattr(up_cli, "_run_capture", run)
    outputs.update(ps="", volume="")
    assert up_cli._has_recovery_resources("proj") is False
    outputs.update(ps="abc123\n", volume="")
    assert up_cli._has_recovery_resources("proj") is True  # exactly one container
    outputs.update(ps="", volume="proj_panella-http-data\n")
    assert up_cli._has_recovery_resources("proj") is True  # exactly one volume
    monkeypatch.setattr(
        up_cli, "_run_capture", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="")
    )
    assert up_cli._has_recovery_resources("proj") is None  # inspection failure is fail-closed upstream


def test_bearer_missing_after_successful_init_exits_4(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(up_cli, "_embedded_compose", _asset)
    monkeypatch.setattr(up_cli.shutil, "which", lambda _: "/usr/bin/docker")

    def run(command, *, timeout, env=None, cwd=None):
        if command == ["docker", "info"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] in (["docker", "ps", "-aq"], ["docker", "volume", "ls"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:3] == ["docker", "compose", "-p"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-2:] == ["init", "--yes"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # succeeds but writes NO bearer
        raise AssertionError(command)

    monkeypatch.setattr(up_cli, "_run_capture", run)
    assert main(["up", "--home", str(tmp_path), "--yes"]) == 4
    err = capsys.readouterr().err
    assert "owner bearer is missing" in err and "panella init --force" in err
