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

    token_db_path = args.token_db or load_config(None).token_db_path
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

    token_db_path = args.token_db or load_config(None).token_db_path
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
    # Precedence: an explicit operator revoke is terminal, so it wins over expiry; a rotated token
    # carries revoked_at (rotate sets it) and therefore reads as revoked here too.
    if record.revoked_at is not None:
        return f"revoked@{_fmt_ts(record.revoked_at)}"
    if record.expired:
        return f"expired@{_fmt_ts(record.expires_at)}"
    return "active"


def _fmt_ts(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else "-"


def _default_token_label() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"owner-{stamp}"
