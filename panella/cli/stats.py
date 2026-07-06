"""``panella stats`` — show corpus aggregate counts."""

from __future__ import annotations

import argparse

from panella.cli import _http as cli_http


def register(subparsers: argparse._SubParsersAction) -> None:
    stats = subparsers.add_parser("stats", help="Show corpus aggregate stats.")
    cli_http.add_http_args(stats)
    cli_http.add_json_arg(stats)
    stats.set_defaults(func=_stats)


def _stats(args: argparse.Namespace) -> int:
    try:
        with cli_http.http_client(args) as client:
            payload = client.stats()
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json(payload)
    else:
        print(f"total_drawers: {payload.get('total_drawers', 0)}")
        for row in payload.get("wing_breakdown", []):
            print(f"{row.get('wing')}: {row.get('drawer_count', 0)}")
    return 0
