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
    init.set_defaults(func=_init)


def _init(args: argparse.Namespace) -> int:
    if args.verify:
        return _verify(args.base_url)
    return _provision(force=args.force)


def _provision(*, force: bool) -> int:
    from panella.governance import GovernanceConfigError

    try:
        root = _load_root_principal()
        token = _mint_owner_bearer(root.id)
    except (GovernanceConfigError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"panella init: {exc}", file=sys.stderr)
        return 2

    print(token)
    print("Store this owner bearer token now; it is not recoverable.", file=sys.stderr)

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

    operator_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if force and approval_token_path.exists():
        print("Regenerating local_cli approval token because --force was supplied.", file=sys.stderr)
    _write_approval_token(approval_token_path)
    if force and overlay_path.exists():
        print("Replacing governance overlay because --force was supplied.", file=sys.stderr)
    _write_governance_overlay(overlay_path, approver_id=_local_cli_approver_id(root.id))

    print(f"approval token file: {_display_path(approval_token_path)}")
    print("operator secret \u2014 never paste into agent config")
    print(f"governance overlay: {_display_path(overlay_path)}")
    print()
    print("Next steps:")
    print(f"  export PANELLA_GOVERNANCE_OVERLAY={APP_LOCAL_DIR / GOVERNANCE_OVERLAY_NAME}")
    print("  export PANELLA_MCP_PROFILE=mcp-write")
    print("  docker compose up -d")
    print("  panella init --verify")
    return 0


def _load_root_principal():
    with _host_overlay_env_if_needed():
        from panella.principal import root_principal

        return root_principal()


def _mint_owner_bearer(principal_id: str) -> str:
    compose_file = Path.cwd() / "docker-compose.yml"
    if compose_file.exists():
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


def _mint_in_running_compose() -> str:
    if shutil.which("docker") is None:
        raise subprocess.SubprocessError("docker command not found; run panella init after docker compose up --wait")
    try:
        ps = subprocess.run(
            ["docker", "compose", "ps", "--services", "--filter", "status=running"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise subprocess.SubprocessError(f"docker compose status check failed: {exc}") from exc
    if ps.returncode != 0:
        detail = (ps.stderr or ps.stdout).strip() or "docker compose ps failed"
        raise subprocess.SubprocessError(detail)
    if "panella-http" not in {line.strip() for line in ps.stdout.splitlines()}:
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
    token = secrets.token_hex(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, APPROVAL_TOKEN_MODE)
    try:
        os.write(fd, f"{token}\n".encode("ascii"))
    finally:
        os.close(fd)
    os.chmod(path, APPROVAL_TOKEN_MODE)


def _write_governance_overlay(path: Path, *, approver_id: str) -> None:
    body = (
        "schema_version: 1\n"
        "approval:\n"
        f'  authorized_approvers: ["{approver_id}"]\n'
        "  transport:\n"
        '    kind: "local_cli"\n'
        "    config:\n"
        f'      token_file: "{APP_LOCAL_DIR / APPROVAL_TOKEN_NAME}"\n'
        '      token_mode: "0600"\n'
    )
    path.write_text(body, encoding="utf-8")


def _verify(base_url: str) -> int:
    checks = [
        _check_health(base_url),
        _check_mcp_mount(base_url),
        _check_approval_transport(),
        _check_approval_token_file(),
    ]
    for passed, line in checks:
        print(f"{'PASS' if passed else 'FAIL'} {line}")
    return 0 if all(passed for passed, _ in checks) else 2


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
            from panella.principal import root_principal

            governance = current_governance()
            transport = build_transport_if_approvable(governance)
            expected_approver = _local_cli_approver_id(root_principal().id)
    except GovernanceConfigError as exc:
        return False, f"approval transport config could not load: {exc}"
    finally:
        reset_governance_cache()
    if transport is None:
        return False, "approval transport is not local_cli-approvable; check PANELLA_GOVERNANCE_OVERLAY"
    if expected_approver not in governance.approval.authorized_approvers:
        return False, (
            f"approval transport is local_cli but {expected_approver!r} is not authorized; "
            "run panella init and point PANELLA_GOVERNANCE_OVERLAY at the generated overlay"
        )
    return True, f"approval transport is local_cli-approvable for {expected_approver}"


def _check_approval_token_file() -> tuple[bool, str]:
    token_path = _host_path_for_operator_file(APPROVAL_TOKEN_NAME)
    if not token_path.exists():
        return False, f"approval token file missing at {_display_path(token_path)}"
    mode = stat.S_IMODE(token_path.stat().st_mode)
    if mode != APPROVAL_TOKEN_MODE:
        return False, f"approval token file mode is {mode:04o}; expected 0600 at {_display_path(token_path)}"
    return True, f"approval token file exists with 0600 at {_display_path(token_path)}"


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


def _local_cli_approver_id(root_principal_id: str) -> str:
    owner_id = root_principal_id.split(":", 1)[-1]
    return f"local_cli:{owner_id}"


def _default_token_label() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"owner-{stamp}"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)
