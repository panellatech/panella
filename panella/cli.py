"""Top-level ``panella`` CLI."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panella",
        description="Panella operator utilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


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


def _default_token_label() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"owner-{stamp}"


if __name__ == "__main__":
    raise SystemExit(main())
