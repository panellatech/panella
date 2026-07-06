"""``panella audit`` — inspect the HTTP audit trail."""

from __future__ import annotations

import argparse

from panella.cli import _http as cli_http


def register(subparsers: argparse._SubParsersAction) -> None:
    audit = subparsers.add_parser("audit", help="Inspect audit events.")
    audit_subparsers = audit.add_subparsers(dest="audit_command", required=True)

    tail = audit_subparsers.add_parser("tail", help="Show recent audit entries.")
    tail.add_argument("--limit", type=int, default=20, help="Maximum entries to show.")
    cli_http.add_http_args(tail)
    cli_http.add_json_arg(tail)
    tail.set_defaults(func=_audit_tail)


def _audit_tail(args: argparse.Namespace) -> int:
    try:
        with cli_http.http_client(args) as client:
            entries = client.audit_tail(limit=args.limit)
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json({"entries": entries})
    else:
        _print_entries(entries)
    return 0


def _print_entries(entries: list[dict]) -> None:
    if not entries:
        print("No audit entries.")
        return
    print("SEQ\tTS\tOP\tTENANT\tTARGET")
    for entry in entries:
        print(
            "\t".join(
                [
                    str(entry.get("seq", "")),
                    str(entry.get("ts_iso") or "-"),
                    str(entry.get("op") or "-"),
                    str(entry.get("tenant_accessed") or "-"),
                    str(entry.get("target_id") or "-"),
                ]
            )
        )
