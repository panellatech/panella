"""``panella init`` - first-run supply for local self-host onboarding."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
OVERLAY_ENV = "PANELLA_GOVERNANCE_OVERLAY"
OPERATOR_DIR = Path(".panella")
APP_LOCAL_DIR = Path("/app/local")
APPROVAL_TOKEN_NAME = "approval-token"
GOVERNANCE_OVERLAY_NAME = "governance.yaml"
APPROVAL_TOKEN_MODE = 0o600
COMPOSE_SERVICE = "panella-http"

# The canonical approver id the local_cli transport stamps for a valid presser is the FIXED literal
# ``local_cli:owner`` — NOT a value derived from ``root_principal.id``. ``verify_presser`` returns
# ``f"{LOCAL_CLI_TRANSPORT}:owner"`` regardless of the configured root identity (the "owner" here is
# "whoever holds the 0600 token file", a transport-level notion, not the principal id). The docs and
# both example overlays hard-code ``["local_cli:owner"]`` (docs/GOVERNANCE.md, config/governance*.yaml).
# init MUST write this same literal, else a box with a customized ``root_principal.id`` gets an
# authorized_approvers set the transport can never satisfy → every approval refused (inert-closed).
# ``test_cli_init`` locks this against the transport's actual stamp so the two can never drift.
LOCAL_CLI_APPROVER = "local_cli:owner"


def register(subparsers: argparse._SubParsersAction) -> None:
    init = subparsers.add_parser("init", help="Provision first-run owner bearer and local approval files.")
    init.add_argument(
        "--force",
        action="store_true",
        help="Regenerate the local approval token and overwrite the governance overlay.",
    )
    init.add_argument(
        "--verify",
        action="store_true",
        help="Verify the running Day-0 HTTP/MCP setup without writing files.",
    )
    init.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Facade base URL for --verify (default: {DEFAULT_BASE_URL}).",
    )
    init.add_argument(
        "--verify-transport",
        action="store_true",
        help=argparse.SUPPRESS,  # internal: run ONLY the transport+token check from THIS process's
        # vantage (used by --verify to exec the check inside the panella-http container, where the
        # server's real uid/paths apply). Not part of the operator-facing surface.
    )
    init.set_defaults(func=_init)


def _init(args: argparse.Namespace) -> int:
    if args.verify_transport:
        passed, line = _check_approval_transport()
        print(f"{'PASS' if passed else 'FAIL'} {line}")
        return 0 if passed else 2
    if args.verify:
        return _verify(args.base_url)
    return _provision(force=args.force)


def _provision(*, force: bool) -> int:
    from panella.governance import GovernanceConfigError

    # Idempotency check BEFORE any side effect: a refused run (files already present, no --force)
    # must not mint an owner bearer. Minting first would leak a live root-privilege token to stdout
    # on every re-run while the message says "keeping existing", and accumulate orphan bearers in
    # the token DB (code-reviewer B1 P2).
    operator_dir = Path.cwd() / OPERATOR_DIR
    approval_token_path = operator_dir / APPROVAL_TOKEN_NAME
    overlay_path = operator_dir / GOVERNANCE_OVERLAY_NAME
    if not force:
        blockers = []
        if approval_token_path.exists():
            blockers.append(
                f"{_display_path(approval_token_path)} already exists; keeping existing operator secret "
                "(rerun with --force to regenerate)"
            )
        if overlay_path.exists():
            blockers.append(
                f"{_display_path(overlay_path)} already exists; refusing to merge or overwrite "
                "(rerun with --force to replace it)"
            )
        if blockers:
            for blocker in blockers:
                print(f"panella init: {blocker}", file=sys.stderr)
            return 2

    compose_present = (Path.cwd() / "docker-compose.yml").exists()
    try:
        root = _load_root_principal()
        token = _mint_owner_bearer(root.id, compose_present=compose_present)
    except (GovernanceConfigError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"panella init: {exc}", file=sys.stderr)
        return 2

    print(token)
    print("Store this owner bearer token now; it is not recoverable.", file=sys.stderr)

    operator_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The server reads the token from the path baked into the overlay, so bake the path that server
    # can actually read: the container mount (/app/local/...) on the Docker path, the absolute host
    # path on a native/dev box. --verify (and the real server) then read exactly what init wrote \u2014
    # no host\u2194container path guessing, and no remap that could pass a check the server would fail.
    if compose_present:
        overlay_token_file = str(APP_LOCAL_DIR / APPROVAL_TOKEN_NAME)
        overlay_pointer = str(APP_LOCAL_DIR / GOVERNANCE_OVERLAY_NAME)
    else:
        overlay_token_file = str(approval_token_path.resolve())
        overlay_pointer = str(overlay_path.resolve())

    if force and approval_token_path.exists():
        print("Regenerating local_cli approval token because --force was supplied.", file=sys.stderr)
    _write_approval_token(approval_token_path)
    if force and overlay_path.exists():
        print("Replacing governance overlay because --force was supplied.", file=sys.stderr)
    _write_governance_overlay(overlay_path, token_file=overlay_token_file, root=root)

    print(f"approval token file: {_display_path(approval_token_path)}")
    print("operator secret \u2014 never paste into agent config")
    print(f"governance overlay: {_display_path(overlay_path)}")
    print()
    print("Next steps:")
    print(f"  export PANELLA_GOVERNANCE_OVERLAY={overlay_pointer}")
    if compose_present:
        print("  export PANELLA_MCP_PROFILE=mcp-write")
        print("  docker compose up -d")
    print("  panella init --verify")
    return 0


def _load_root_principal():
    with _host_overlay_env_if_needed():
        from panella.principal import root_principal

        return root_principal()


def _mint_owner_bearer(principal_id: str, *, compose_present: bool) -> str:
    if compose_present:
        return _mint_in_running_compose()

    from panella.http.config import load_config
    from panella.http.tokens import TokenStore

    label = _default_token_label()
    token_db_path = load_config(None).token_db_path
    try:
        return TokenStore(token_db_path).mint(principal_id=principal_id, label=label)
    except sqlite3.IntegrityError as exc:
        raise sqlite3.IntegrityError(
            f"token label {label!r} already exists in {token_db_path}; rerun panella init"
        ) from exc


def _compose_service_running(service: str) -> bool:
    try:
        ps = subprocess.run(
            ["docker", "compose", "ps", "--services", "--filter", "status=running"],
            cwd=Path.cwd(), capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if ps.returncode != 0:
        return False
    return service in {line.strip() for line in ps.stdout.splitlines()}


def _mint_in_running_compose() -> str:
    if shutil.which("docker") is None:
        raise subprocess.SubprocessError("docker command not found; run panella init after docker compose up --wait")
    if not _compose_service_running(COMPOSE_SERVICE):
        raise subprocess.SubprocessError("panella-http is not running; run docker compose up --wait first")
    minted = subprocess.run(
        ["docker", "compose", "exec", "-T", "panella-http", "panella", "tokens", "mint"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if minted.returncode != 0:
        detail = (minted.stderr or minted.stdout).strip() or "docker compose exec panella-http panella tokens mint failed"
        raise subprocess.SubprocessError(detail)
    token_lines = [line.strip() for line in minted.stdout.splitlines() if line.strip()]
    if len(token_lines) != 1:
        raise subprocess.SubprocessError("docker compose token mint returned an unexpected response")
    return token_lines[0]


def _write_approval_token(path: Path) -> None:
    # Write to a FRESH 0600 temp then atomically rename into place. An O_TRUNC directly over an
    # existing loose-mode file would hold the new secret at the OLD (possibly world-readable) mode
    # during the write and chmod only afterward — a window where the secret is exposed. Creating a
    # new temp (0600 from birth, umask-proofed by an explicit chmod) and renaming means the secret is
    # never visible at a wider mode (code-reviewer / Codex P2).
    token = secrets.token_hex(32)
    tmp_path = path.with_name(path.name + ".new")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, APPROVAL_TOKEN_MODE)
    try:
        os.write(fd, f"{token}\n".encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(tmp_path, APPROVAL_TOKEN_MODE)
    os.replace(tmp_path, path)


def _write_governance_overlay(path: Path, *, token_file: str, root) -> None:
    import yaml

    # Emit via a YAML dumper, not string interpolation: authorized_approvers is the finalizer
    # keystone, and building it by f-string concat is a latent injection/corruption surface (a
    # ``"`` or newline anywhere in an interpolated value would rewrite the doc). The approver is the
    # FIXED canonical literal (see LOCAL_CLI_APPROVER). token_mode is quoted so YAML keeps it a string
    # ("0600"), matching build_transport's octal parse — an unquoted 0600 would parse as int 600.
    #
    # Identity is PRESERVED in the same overlay: governance is the generic base deep-merged with the
    # SINGLE overlay slot (overlay wins per key), and init's next-steps repoint PANELLA_GOVERNANCE_
    # OVERLAY at THIS file. Writing approval-only would drop a customized root_principal on restart —
    # the owner bearer minted for e.g. human:alice would stop being owner-gated for /mcp + approvals
    # (Codex B1 P1). Restating the loaded identity keeps a custom box owner-consistent; for the
    # generic default it just re-affirms the base (harmless).
    overlay = {
        "schema_version": 1,
        "identity": {
            "root_principal": {
                "id": root.id,
                "subject_id": root.subject_id,
                "roles": sorted(root.roles),
            }
        },
        "approval": {
            "authorized_approvers": [LOCAL_CLI_APPROVER],
            "transport": {
                "kind": "local_cli",
                "config": {
                    "token_file": token_file,
                    "token_mode": "0600",
                },
            },
        },
    }
    path.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")


def _verify(base_url: str) -> int:
    checks = [
        _check_health(base_url),
        _check_mcp_mount(base_url),
        _check_transport_effective(),
        _check_approval_token_file(),
    ]
    for passed, line in checks:
        print(f"{'PASS' if passed else 'FAIL'} {line}")
    return 0 if all(passed for passed, _ in checks) else 2


def _check_transport_effective() -> tuple[bool, str]:
    """Verify the approval transport can actually load its token FROM THE SERVER'S VANTAGE.

    On the documented Docker path the server is the ``panella-http`` container (uid 10001) reading
    the bind-mounted ``/app/local/approval-token`` — a host-side stat can pass while the container
    (different uid) cannot read the 0600 file, silently breaking every approval. So when compose is
    up we exec the transport check INSIDE the container (``panella init --verify-transport``), where
    the real uid and container paths apply; the check reads the token through the transport exactly
    as the finalizer will.

    A compose deployment MUST be verified from the container's vantage: if ``docker-compose.yml`` is
    present but we cannot exec (docker CLI missing, daemon down, service not up), we FAIL LOUD rather
    than fall back to a host-side check — the fallback would recreate the exact uid false-pass this
    exists to prevent (Codex P2). Off the compose path (native/dev) the overlay carries host paths, so
    the local check reads the same file the server does."""
    if (Path.cwd() / "docker-compose.yml").exists():
        if shutil.which("docker") is None:
            return False, "docker-compose.yml present but the docker CLI is unavailable; start the stack, then re-run --verify"
        if not _compose_service_running(COMPOSE_SERVICE):
            return False, f"docker-compose.yml present but {COMPOSE_SERVICE} is not running; run docker compose up -d, then re-run --verify"
        try:
            proc = subprocess.run(
                ["docker", "compose", "exec", "-T", COMPOSE_SERVICE, "panella", "init", "--verify-transport"],
                cwd=Path.cwd(), capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not run the in-container transport check: {exc}"
        line = (proc.stdout.strip() or proc.stderr.strip() or "no output").splitlines()[-1]
        # The in-container check prints "PASS <line>"/"FAIL <line>"; surface its line, keyed off exit.
        detail = line[5:] if line[:5] in {"PASS ", "FAIL "} else line
        return proc.returncode == 0, f"[container] {detail}"
    return _check_approval_transport()


def _check_health(base_url: str) -> tuple[bool, str]:
    status, body = _request_status(_url(base_url, "/v1/health"))
    if status == 200:
        return True, "/v1/health returned 200"
    return False, f"/v1/health expected 200, got {status}: {body[:160]}"


def _check_mcp_mount(base_url: str) -> tuple[bool, str]:
    status, body = _request_status(_url(base_url, "/mcp"))
    if status in {401, 407}:
        return True, f"/mcp is mounted and refused unauthenticated access with {status}"
    if status == 404:
        return False, "/mcp returned 404; set PANELLA_MCP_PROFILE=mcp-write and restart compose with MCP enabled"
    return False, f"/mcp expected unauthenticated 401/407-class refusal, got {status}: {body[:160]}"


def _check_approval_transport() -> tuple[bool, str]:
    from panella.governance import GovernanceConfigError, current_governance, reset_governance_cache

    try:
        with _host_overlay_env_if_needed():
            from panella.mcp_tools import build_transport_if_approvable

            governance = current_governance()
            transport = build_transport_if_approvable(governance)
            approvers = tuple(governance.approval.authorized_approvers)
            # Read the token exactly as the SERVER will: through the transport, from the effective
            # path, subject to the transport's own mode/readability rules. This is what makes
            # --verify honest — it FAILs when the process running this check cannot actually load the
            # token (wrong mode, unreadable by this uid, empty), instead of trusting a host-side stat.
            # ``verify_presser`` then yields the canonical stamp the finalizer will compare against
            # authorized_approvers (never a value we re-derive here, which is how the old check
            # false-passed for a customized root identity). The token is read at the LITERAL path the
            # overlay configures — the exact path the server reads — with NO host↔container remap: on
            # the Docker path this check runs INSIDE the container (via --verify-transport, real uid +
            # paths); off compose the overlay carries the absolute host path, so the literal read is
            # already correct. A remap here could pass a check the real server would fail (Codex P1).
            expected_token = transport._expected_token() if transport is not None else None
            stamp = transport.verify_presser(expected_token) if (transport is not None and expected_token) else None
    except GovernanceConfigError as exc:
        return False, f"approval transport config could not load: {exc}"
    finally:
        reset_governance_cache()
    if transport is None:
        return False, "approval transport is not local_cli-approvable; check PANELLA_GOVERNANCE_OVERLAY"
    if expected_token is None:
        return False, (
            "approval transport is local_cli but its token file is unreadable or has loose "
            "permissions from this process; under Docker the panella-http container (uid 10001) must "
            "be able to read the mounted token — run the stack as your uid or see docs/SELF_HOST.md"
        )
    if stamp is None or stamp not in approvers:
        return False, (
            f"approval transport stamps {stamp!r} but authorized_approvers={list(approvers)}; "
            "run panella init to write a matching overlay"
        )
    return True, f"approval transport is local_cli-approvable and stamps an authorized {stamp}"


def _check_approval_token_file() -> tuple[bool, str]:
    token_path = _host_path_for_operator_file(APPROVAL_TOKEN_NAME)
    if not token_path.exists():
        return False, f"approval token file missing at {_display_path(token_path)}"
    mode = stat.S_IMODE(token_path.stat().st_mode)
    # Match the transport's own rule (approval_transport.py: mode & ~token_mode): any permissions
    # WITHIN 0600 are fine (a hardened operator's 0400 is accepted by the server), only extra bits
    # are rejected. An exact ``!= 0600`` check would false-FAIL a stricter-than-required file.
    if mode & ~APPROVAL_TOKEN_MODE:
        return False, f"approval token file mode is {mode:04o}; must not exceed 0600 at {_display_path(token_path)}"
    return True, f"approval token file exists with mode {mode:04o} (within 0600) at {_display_path(token_path)}"


def _request_status(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status), response.read(512).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(512).decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)
    except TimeoutError as exc:
        return 0, str(exc)


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


@contextmanager
def _host_overlay_env_if_needed() -> Iterator[None]:
    from panella.governance import reset_governance_cache

    raw = os.environ.get(OVERLAY_ENV)
    mapped = _map_app_local_path(raw) if raw else None
    changed = mapped is not None and mapped.exists()
    if changed:
        os.environ[OVERLAY_ENV] = str(mapped)
    reset_governance_cache()
    try:
        yield
    finally:
        if changed:
            if raw is None:
                os.environ.pop(OVERLAY_ENV, None)
            else:
                os.environ[OVERLAY_ENV] = raw
        reset_governance_cache()


def _map_app_local_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    try:
        relative = path.relative_to(APP_LOCAL_DIR)
    except ValueError:
        return None
    return Path.cwd() / OPERATOR_DIR / relative


def _host_path_for_operator_file(name: str) -> Path:
    raw = os.environ.get(OVERLAY_ENV)
    mapped = _map_app_local_path(str(APP_LOCAL_DIR / name))
    if raw:
        overlay = _map_app_local_path(raw)
        if overlay is not None:
            return overlay.parent / name
    return mapped or (Path.cwd() / OPERATOR_DIR / name)


def _default_token_label() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"owner-{stamp}"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
