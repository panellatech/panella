"""``panella tokens`` — manage HTTP/MCP bearer tokens."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def register(subparsers: argparse._SubParsersAction) -> None:
    tokens = subparsers.add_parser("tokens", help="Manage HTTP/MCP bearer tokens.")
    token_subparsers = tokens.add_subparsers(dest="tokens_command", required=True)

    mint = token_subparsers.add_parser("mint", help="Mint an agent-facing bearer token.")
    mint.add_argument(
        "--principal",
        default=None,
        help="Principal id to bind to the token (default: governance root principal).",
    )
    mint.add_argument(
        "--token-db",
        type=Path,
        default=None,
        help="Token database path (default: configured HTTP token DB).",
    )
    mint.add_argument(
        "--label",
        default=None,
        help="Unique token label (default: generated owner label).",
    )
    mint.set_defaults(func=_tokens_mint)

    revoke = token_subparsers.add_parser(
        "revoke",
        help="Revoke a bearer token by label (idempotent; rejected on every surface afterward).",
    )
    revoke.add_argument("--label", required=True, help="Label of the token to revoke.")
    revoke.add_argument(
        "--token-db",
        type=Path,
        default=None,
        help="Token database path (default: configured HTTP token DB).",
    )
    revoke.set_defaults(func=_tokens_revoke)

    list_tokens = token_subparsers.add_parser(
        "list",
        help="List token labels and lifecycle status (never prints token values).",
    )
    list_tokens.add_argument(
        "--token-db",
        type=Path,
        default=None,
        help="Token database path (default: configured HTTP token DB).",
    )
    list_tokens.set_defaults(func=_tokens_list)


def _tokens_mint(args: argparse.Namespace) -> int:
    import sqlite3

    from panella.http.config import load_config
    from panella.http.tokens import TokenStore
    from panella.principal import root_principal

    root = root_principal()
    principal_id = args.principal or root.id
    token_db_path = args.token_db or load_config(None).token_db_path
    label = args.label or _default_token_label()
    try:
        token = TokenStore(token_db_path).mint(principal_id=principal_id, label=label)
    except sqlite3.IntegrityError:
        # Labels are UNIQUE per token DB — a duplicate must be an actionable one-liner, not the
        # opaque traceback WP3 exists to eliminate.
        print(
            f"token label {label!r} already exists in {token_db_path} — "
            "choose a unique --label.",
            file=sys.stderr,
        )
        return 2
    print(token)
    token_kind = "owner bearer token" if principal_id == root.id else "bearer token"
    print(f"Store this {token_kind} now; it is not recoverable.", file=sys.stderr)
    return 0


def _tokens_revoke(args: argparse.Namespace) -> int:
    from panella.http.config import load_config
    from panella.http.tokens import TokenStore

    # Fail closed: a bare host-side revoke targets the HOST default token DB, NOT the box the
    # container serves from. If a stale host DB holds the same label, revoke would report success
    # while the LIVE container bearer stays valid — the most dangerous false-success on an auth
    # surface. Refuse and give the exact in-container form.
    if args.token_db is None and (msg := _compose_defer_message("revoke", f" --label {args.label}")):
        print(msg, file=sys.stderr)
        return 2

    token_db_path = args.token_db or load_config(None).token_db_path
    if not Path(token_db_path).exists():
        # Never MATERIALIZE a phantom host DB on a mutating command — that would report a misleading
        # "no token with label" against a freshly-created empty DB rather than the real store.
        print(f"no token database at {token_db_path}", file=sys.stderr)
        return 2
    # revoke() is idempotent: it stamps revoked_at via COALESCE, so re-revoking keeps the original
    # timestamp and still reports success. rowcount>0 (True) means the label existed; False means
    # no such label. Enforcement is NOT added here — the shared resolve_bearer() (panella/http/auth.py)
    # already rejects any token whose revoked_at is set, on BOTH the /v1 REST surface and the /mcp
    # mount; this command just sets the column that resolver reads.
    revoked = TokenStore(token_db_path).revoke(args.label)
    if not revoked:
        print(f"no token with label {args.label!r} in {token_db_path}", file=sys.stderr)
        return 2
    print(f"revoked {args.label}")
    print(
        "The bearer is now rejected on every surface (HTTP /v1 and /mcp). "
        "Under docker compose, run this inside the container "
        "(docker compose exec -T panella-http panella tokens revoke ...) to hit the box's token DB.",
        file=sys.stderr,
    )
    return 0


def _tokens_list(args: argparse.Namespace) -> int:
    from panella.http.config import load_config
    from panella.http.tokens import TokenStore

    # Same host-vs-container hazard as revoke: a bare host list can create/read the WRONG DB and print
    # "No tokens" or a stale status, misleading an operator about the live container bearer.
    if args.token_db is None and (msg := _compose_defer_message("list")):
        print(msg, file=sys.stderr)
        return 2

    token_db_path = args.token_db or load_config(None).token_db_path
    if not Path(token_db_path).exists():
        print(f"no token database at {token_db_path}", file=sys.stderr)
        return 2
    records = TokenStore(token_db_path).list()
    if not records:
        print("No tokens.")
        return 0
    # NEVER print the token value or its sha256 digest — only the operator-facing handle (label),
    # the bound principal, and lifecycle timestamps. The raw token is unrecoverable after mint by
    # design; the digest is a secret-adjacent identifier and stays out of operator output too.
    print(f"{'LABEL':<28} {'PRINCIPAL':<24} {'CREATED':<20} {'LAST_USED':<20} STATUS")
    for record in records:
        print(
            f"{record.label:<28} {record.principal_id:<24} "
            f"{_fmt_ts(record.created_at):<20} {_fmt_ts(record.last_used_at):<20} "
            f"{_token_status(record)}"
        )
    return 0


def _token_status(record: Any) -> str:
    # Mirror the resolver's EXACT precedence (resolve_bearer: revoked_at <= now, THEN expired). A
    # future revoked_at is a rotate() grace window (still valid), but only until expiry — so an
    # already-expired token must read "expired", not "rotating", to match what auth actually does.
    now = datetime.now(UTC)
    if record.revoked_at is not None and record.revoked_at <= now:
        return f"revoked@{_fmt_ts(record.revoked_at)}"
    if record.expired:
        return f"expired@{_fmt_ts(record.expires_at)}"
    if record.revoked_at is not None:
        return f"rotating_until@{_fmt_ts(record.revoked_at)}"  # future revoked_at, not yet expired
    return "active"


def _fmt_ts(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else "-"


def _compose_defer_message(subcommand: str, extra: str = "") -> str | None:
    """A fail-closed message when a bare (no --token-db) command would hit the HOST default token DB
    while panella-http serves from the container's DB; None = proceed. Locates the compose project by
    walking parents (docker compose itself walks up, so an exact-cwd check under-detects from a
    subdir); inside the app container there is no docker CLI, so the running-check is False and the
    command proceeds against the container DB as intended."""
    if _compose_root() is None:
        return None
    from panella.cli.init import COMPOSE_SERVICE, _compose_service_running

    if not _compose_service_running(COMPOSE_SERVICE):
        return None
    return (
        "refusing to run against the host token DB while panella-http is running (the box serves "
        "from the container's DB, and a stale host DB could report false success while the live "
        "bearer stays valid). Run it inside the container:\n"
        f"  docker compose exec -T panella-http panella tokens {subcommand}{extra}\n"
        "or pass --token-db explicitly to target a specific database."
    )


# The Compose CLI's standard project-file names (precedence order per the Compose spec). A guard
# that only checked docker-compose.yml would skip the fail-closed path for a deployment using the
# modern compose.yaml, re-opening the stale-host false-success this guard prevents (GH-bot P2).
_COMPOSE_FILENAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def _compose_root() -> Path | None:
    import os

    # An explicit COMPOSE_FILE (equivalent to `-f`) points docker compose at a project regardless of
    # cwd; honor it as "compose is configured" and let _compose_service_running (which itself honors
    # COMPOSE_FILE) decide whether panella-http is actually up.
    if os.environ.get("COMPOSE_FILE"):
        return Path.cwd()
    cwd = Path.cwd()
    for directory in (cwd, *cwd.parents):
        if any((directory / name).exists() for name in _COMPOSE_FILENAMES):
            return directory
    return None


def _default_token_label() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"owner-{stamp}"
