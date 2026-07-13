"""``panella up`` - materialize and operate a released self-hosted box."""

from __future__ import annotations

import argparse
import hashlib
import importlib.resources
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

try:  # POSIX-only. The CLI package imports this module eagerly; a Windows install must still be
    # able to run every non-up command, so absence degrades inside up's own preflight (tag_lock idiom).
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from panella.cli.connect import DEFAULT_BASE_URL, render_client
from panella.cli.init import (
    APPROVAL_TOKEN_NAME,
    GOVERNANCE_OVERLAY_NAME,
    MCP_PROFILE_ENV,
    OPERATOR_DIR,
    OVERLAY_ENV,
    OWNER_BEARER_NAME,
    _compose_root,
)

MANAGED_HEADER = (
    "# managed by panella up — release {version} — do not edit "
    "(hand-edits: use the git-clone path; upgrades: docs/UPGRADE.md)"
)
MSG_TIMEOUT_DOCKER_INFO = "docker info timed out; check that the docker daemon is running"
MSG_TIMEOUT_COMPOSE_UP = "compose up timed out"
MSG_TIMEOUT_INIT = "panella init timed out; the box may be partially provisioned; run panella init --verify"

_COMPOSE_NAME = "docker-compose.yml"
# Child init runs THIS interpreter's installation, not a PATH lookup: `panella up` is routinely
# invoked by absolute path (venv script, uvx shim) with the console-script dir absent from PATH.
_CHILD_SHIM = "import sys; from panella.cli import main; sys.exit(main(sys.argv[1:]))"
_COMPOSE_ENV_SCRUB = (
    # COMPOSE_DISABLE_ENV_FILE=1 makes compose ignore the .env this command just generated,
    # breaking ${PANELLA_API_KEY:?}/UID/GID interpolation despite a valid file.
    "COMPOSE_DISABLE_ENV_FILE",
    "COMPOSE_ENV_FILES",
    "COMPOSE_PATH_SEPARATOR",
    "COMPOSE_PROFILES",
    "COMPOSE_PROJECT_DIRECTORY",
)
_CHILD_ENV_SCRUB = (
    "PANELLA_API_KEY",
    "PANELLA_GOVERNANCE_OVERLAY",
    "PANELLA_MCP_PROFILE",
    "PANELLA_UNDER_COMPOSE",
    # Compose gives SHELL values precedence over .env: a stale caller PANELLA_UID/GID would
    # override the generated identity pin and reintroduce the unreadable-mount failure.
    "PANELLA_UID",
    "PANELLA_GID",
)
_API_KEY_RE = re.compile(rb"^\s*(export\s+)?PANELLA_API_KEY\s*=")
_PROJECT_RE = re.compile(rb"^\s*(export\s+)?COMPOSE_PROJECT_NAME\s*=")
_UID_RE = re.compile(rb"^\s*(export\s+)?PANELLA_UID\s*=")
_GID_RE = re.compile(rb"^\s*(export\s+)?PANELLA_GID\s*=")
_HEADER_RE = re.compile(
    re.escape(MANAGED_HEADER).replace(re.escape("{version}"), r"(?P<version>[^\r\n]+)")
)


def register(subparsers: argparse._SubParsersAction) -> None:
    up = subparsers.add_parser("up", help="Bootstrap a released digest-pinned self-hosted box.")
    up.add_argument("--home", help="Directory for this self-hosted box.")
    up.add_argument("--yes", action="store_true", help="Run without an interactive confirmation.")
    up.set_defaults(func=_up)


def _resolve_home(value: str | None) -> tuple[Path, str] | None:
    raw = value if value is not None else os.environ.get("PANELLA_HOME", "~/panella-box")
    if raw == "":
        print("panella up: empty home path", file=sys.stderr)
        return None
    home = Path(raw).expanduser().resolve()
    project = f"panella-box-{hashlib.sha256(str(home).encode('utf-8')).hexdigest()[:8]}"
    return home, project


def _require_consent(*, home: Path, yes: bool) -> int | None:
    if yes:
        return None
    if not sys.stdin.isatty():
        print("stdin is not a TTY; pass --yes to run non-interactively", file=sys.stderr)
        return 2
    sys.stderr.write(
        f"panella up will create {home}, write compose/.env, start containers, and activate write mode. Continue? [Y/n] "
    )
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = "n"
    if answer in {"", "y", "yes"}:
        return None
    print("panella up: aborted by operator", file=sys.stderr)
    return 1


def _is_regular_owned(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        print(f"panella up: refusing unsafe path {path}", file=sys.stderr)
        raise ValueError(path)
    return info


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.unlink(missing_ok=True)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            view = memoryview(content)
            while view:  # POSIX write may be short; publishing a partial buffer would forge a managed file
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(f"short write to {temporary}")
                view = view[written:]
        finally:
            os.close(fd)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _embedded_compose() -> bytes | None:
    try:
        asset = importlib.resources.files("panella_selfhost._assets").joinpath("compose.pinned.yml")
        return asset.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def _materialize_compose(path: Path, asset: bytes) -> bool:
    try:
        existing_info = _is_regular_owned(path)
    except ValueError:
        return False
    if existing_info is None:
        _atomic_write(path, asset, 0o600)
        return True
    current = path.read_bytes()
    if current == asset:
        return True
    first_line = current.splitlines()[:1]
    match = _HEADER_RE.fullmatch(first_line[0].decode("utf-8", "replace")) if first_line else None
    if match:
        asset_header = asset.splitlines()[:1]
        asset_match = _HEADER_RE.fullmatch(asset_header[0].decode("utf-8", "replace")) if asset_header else None
        if asset_match and match.group("version") == asset_match.group("version"):
            print("panella up: managed compose drift detected; restore it or use the git-clone path", file=sys.stderr)
        else:
            print("panella up: a release upgrade is available; follow docs/UPGRADE.md", file=sys.stderr)
    else:
        print("panella up: existing docker-compose.yml is unmanaged; use the git-clone path", file=sys.stderr)
    return False


def _upsert_env(path: Path, project: str) -> bool:
    try:
        existing_info = _is_regular_owned(path)
    except ValueError:
        return False
    if existing_info is None:
        # PANELLA_UID/GID pin the container identity to the invoking user; without them the compose
        # default 10001:0 cannot read the operator's 0700/0600 bind-mounted .panella on native Linux
        # (macOS Docker Desktop masks this via its uid mapping).
        content = (
            f"PANELLA_API_KEY={secrets.token_hex(32)}\n"
            f"PANELLA_UID={os.getuid()}\n"
            f"PANELLA_GID={os.getgid()}\n"
            f"COMPOSE_PROJECT_NAME={project}\n"
        )
        _atomic_write(path, content.encode(), 0o600)
        return True
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    keys = [line for line in lines if _API_KEY_RE.match(line)]
    # A missing key line is appendable; only an empty value or a duplicated key is ambiguous.
    if len(keys) > 1 or (keys and not keys[0].split(b"=", 1)[1].strip()):
        print("ambiguous PANELLA_API_KEY in .env — fix it manually", file=sys.stderr)
        return False
    if existing_info.st_mode & 0o077:
        print(f"WARNING: {path} is group/other-readable; consider chmod 600", file=sys.stderr)
    rewritten = list(lines)
    appended = False
    secret_added = False

    def _append(line: str) -> None:
        nonlocal appended
        if rewritten and not rewritten[-1].endswith(b"\n"):
            rewritten[-1] += b"\n"
        rewritten.append(line.encode())
        appended = True

    if not keys:
        _append(f"PANELLA_API_KEY={secrets.token_hex(32)}\n")
        secret_added = True
    if not any(_UID_RE.match(line) for line in rewritten):
        _append(f"PANELLA_UID={os.getuid()}\n")
    if not any(_GID_RE.match(line) for line in rewritten):
        _append(f"PANELLA_GID={os.getgid()}\n")
    if not any(_PROJECT_RE.match(line) for line in rewritten):
        _append(f"COMPOSE_PROJECT_NAME={project}\n")
    if appended:
        # A freshly GENERATED secret must never land group/other-readable — tighten (never loosen),
        # mirroring the operator-dir rule. Non-secret companion lines preserve the original mode;
        # a pre-existing exposed key only warrants the WARN above (its exposure predates us).
        mode = 0o600 if secret_added else stat.S_IMODE(existing_info.st_mode)
        _atomic_write(path, b"".join(rewritten), mode)
    return True


def _ensure_operator_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        return True
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        print(f"panella up: refusing unsafe operator directory {path}", file=sys.stderr)
        return False
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return True


def _operator_state(operator_dir: Path) -> str:
    # Do not open or construct the approval-token path: the command's secret boundary explicitly
    # excludes that credential. Directory entries are enough for init's three-file predicate.
    with os.scandir(operator_dir) as entries:
        names = {entry.name for entry in entries}
    approval = APPROVAL_TOKEN_NAME in names
    overlay = GOVERNANCE_OVERLAY_NAME in names
    bearer = OWNER_BEARER_NAME in names
    if approval and overlay:
        return "provisioned"
    if not approval and not overlay and not bearer:
        return "fresh"
    return "partial"


def _run_capture(command: list[str], *, timeout: int, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, env=env, cwd=cwd)


def _has_recovery_resources(project: str) -> bool | None:
    # IDs-only (-aq/-q) output: table mode honors the user's psFormat/volumesFormat config, which
    # can drop the header line and make a single surviving container/volume count as "none" —
    # silently bypassing the recovery refusal this gate exists for. Quiet mode is format-immune:
    # any non-empty line IS a resource.
    commands = (
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"],
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
    )
    for command in commands:
        try:
            result = _run_capture(command, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"panella up: could not inspect recovery resources: {exc}", file=sys.stderr)
            return None
        if result.returncode != 0:
            print("panella up: could not inspect recovery resources", file=sys.stderr)
            return None
        if any(line.strip() for line in result.stdout.splitlines()):
            return True
    return False


def _print_recovery(home: Path, compose_path: Path, project: str) -> None:
    if compose_path.exists():
        print(
            f"restore .panella from backup, or reset: docker compose -p {project} -f {compose_path} down -v",
            file=sys.stderr,
        )
        return
    print(f"# WARNING: this DELETES the box data for project {project}", file=sys.stderr)
    print(f"docker ps -a  --filter label=com.docker.compose.project={project}", file=sys.stderr)
    print(f"docker volume ls --filter label=com.docker.compose.project={project}", file=sys.stderr)
    print(f'ids=$(docker ps -aq --filter label=com.docker.compose.project={project}); [ -n "$ids" ] && docker rm -f $ids', file=sys.stderr)
    print(f'vols=$(docker volume ls -q --filter label=com.docker.compose.project={project}); [ -n "$vols" ] && docker volume rm $vols', file=sys.stderr)


def _child_env(*, project: str, compose_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in (*_COMPOSE_ENV_SCRUB, *_CHILD_ENV_SCRUB):
        env.pop(key, None)
    env["COMPOSE_PROJECT_NAME"] = project
    env["COMPOSE_FILE"] = str(compose_path)
    return env


def _compose_env(*, activated: bool) -> dict[str, str]:
    env = os.environ.copy()
    for key in (*_COMPOSE_ENV_SCRUB, *_CHILD_ENV_SCRUB):
        env.pop(key, None)
    env[MCP_PROFILE_ENV] = "mcp-write" if activated else "mcp-read"
    env[OVERLAY_ENV] = "/app/local/governance.yaml" if activated else ""
    return env


def _compose_up(*, home: Path, compose_path: Path, project: str, activated: bool) -> int:
    command = ["docker", "compose", "-p", project, "-f", str(compose_path), "up", "-d", "--wait"]
    try:
        result = _run_capture(command, timeout=300, env=_compose_env(activated=activated), cwd=home)
    except subprocess.TimeoutExpired:
        logs = f"docker compose -p {project} -f {shlex.quote(str(compose_path))} logs"
        print(f"{MSG_TIMEOUT_COMPOSE_UP}; inspect logs with: {logs}", file=sys.stderr)
        return 3
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"panella up: docker compose up failed: {exc}", file=sys.stderr)
        return 3
    if result.returncode == 0:
        return 0
    detail = (result.stderr or result.stdout).strip()
    print(f"panella up: docker compose up failed{': ' + detail if detail else ''}", file=sys.stderr)
    return 3


def _preflight() -> int:
    if fcntl is None:
        print("panella up requires a POSIX host (docker compose + flock); see docs/SELF_HOST.md", file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("panella up: docker command not found", file=sys.stderr)
        return 2
    try:
        result = _run_capture(["docker", "info"], timeout=10)
    except subprocess.TimeoutExpired:
        print(MSG_TIMEOUT_DOCKER_INFO, file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"panella up: docker info failed: {exc}", file=sys.stderr)
        return 2
    if result.returncode != 0:
        print("panella up: docker daemon is unavailable", file=sys.stderr)
        return 2
    return 0


def _up(args: argparse.Namespace) -> int:
    resolved = _resolve_home(args.home)
    if resolved is None:
        return 2
    home, project = resolved
    consent = _require_consent(home=home, yes=args.yes)
    if consent is not None:
        return consent
    # This observes the caller environment before child-specific COMPOSE_FILE is ever constructed.
    # Refuse only a DEFAULT home while standing in a checkout; an explicitly designated home
    # (--home or PANELLA_HOME — the release drill's own shape) is the sanctioned escape, and the
    # empty-string forms already failed fast in _resolve_home.
    explicit_home = args.home is not None or os.environ.get("PANELLA_HOME") is not None
    if not explicit_home and _compose_root() is not None:
        print("panella up: a compose checkout is detected; use panella init there or pass --home", file=sys.stderr)
        return 2
    preflight = _preflight()
    if preflight:
        return preflight
    # Only home.mkdir + the lock-file open run before the lock. Both are race-safe (exist_ok /
    # O_CREAT without O_EXCL); everything that lstat/mkdir-races — creating .panella, state — must
    # happen INSIDE the lock, else two concurrent fresh `up`s hit .panella's lstat→mkdir window and
    # the loser raises FileExistsError instead of the documented per-home flock refusal (§14).
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = home / ".up.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("panella up: another panella up is already operating this home", file=sys.stderr)
            return 2
        operator_dir = home / OPERATOR_DIR
        if not _ensure_operator_dir(operator_dir):
            return 2
        compose_path = home / _COMPOSE_NAME
        state = _operator_state(operator_dir)
        if state == "partial":
            print("panella up: partial .panella state; no changes made", file=sys.stderr)
            return 2
        if state == "fresh":
            recovery = _has_recovery_resources(project)
            if recovery is None:
                return 2
            if recovery:
                _print_recovery(home, compose_path, project)
                return 2
        asset = _embedded_compose()
        if asset is None:
            print("panella up: install a released version; developers: use `panella init` in a checkout", file=sys.stderr)
            return 2
        if not _materialize_compose(compose_path, asset) or not _upsert_env(home / ".env", project):
            return 2
        compose_rc = _compose_up(
            home=home, compose_path=compose_path, project=project, activated=(state == "provisioned")
        )
        if compose_rc:
            return compose_rc
        try:
            child = _run_capture(
                [sys.executable, "-c", _CHILD_SHIM, "init", "--yes"],
                timeout=600,
                env=_child_env(project=project, compose_path=compose_path),
                cwd=home,
            )
        except subprocess.TimeoutExpired:
            print(MSG_TIMEOUT_INIT, file=sys.stderr)
            return 4
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"panella up: panella init failed: {exc}", file=sys.stderr)
            return 4
        if child.returncode != 0:
            # stderr ONLY: init prints the minted owner bearer to stdout, so a stdout fallback is
            # one init refactor away from echoing a live credential into terminal scrollback/logs.
            detail = (child.stderr or "").strip()
            print(f"panella up: panella init failed{': ' + detail if detail else ''}; run panella init --verify", file=sys.stderr)
            return 4
        bearer_path = operator_dir / OWNER_BEARER_NAME
        try:
            bearer = bearer_path.read_text(encoding="utf-8").strip()
        except OSError:
            bearer = ""
        if not bearer:
            print("panella up: owner bearer is missing; run panella init --force or mint and save it with mode 0600", file=sys.stderr)
            return 4
        print("Claude Code")
        print(render_client("claude-code", token=bearer, base_url=DEFAULT_BASE_URL))
        print(f"Other clients — run from {home}: `panella connect --print claude-desktop` or `panella connect --print cursor`")
        print("Next steps: keep the operator approval token outside agent configuration.")
        return 0
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
