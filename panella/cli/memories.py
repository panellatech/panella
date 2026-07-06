"""``panella memories`` — search and inspect governed memories."""

from __future__ import annotations

import argparse

from panella.cli import _http as cli_http


def register(subparsers: argparse._SubParsersAction) -> None:
    memories = subparsers.add_parser("memories", help="Search and inspect memories.")
    memory_subparsers = memories.add_subparsers(dest="memories_command", required=True)

    search = memory_subparsers.add_parser("search", help="Search memories.")
    search.add_argument("query", help="Search query.")
    search.add_argument("--k", type=int, default=5, help="Maximum hits to return.")
    cli_http.add_http_args(search)
    cli_http.add_json_arg(search)
    search.set_defaults(func=_memories_search)

    show = memory_subparsers.add_parser("show", help="Show one memory by id.")
    show.add_argument("memory_id", help="Memory id to read.")
    cli_http.add_http_args(show)
    cli_http.add_json_arg(show)
    show.set_defaults(func=_memories_show)


def _memories_search(args: argparse.Namespace) -> int:
    try:
        with cli_http.http_client(args) as client:
            hits = client.search(args.query, k=args.k)
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json({"hits": hits})
    else:
        _print_hits(hits)
    return 0


def _memories_show(args: argparse.Namespace) -> int:
    try:
        with cli_http.http_client(args) as client:
            hit = client.get_memory(args.memory_id)
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json(hit)
    else:
        _print_memory(hit)
    return 0


def _print_hits(hits: list[dict]) -> None:
    if not hits:
        print("No memories found.")
        return
    for hit in hits:
        memory_id = hit.get("id") or hit.get("drawer_id") or "-"
        wing = hit.get("wing") or "-"
        room = hit.get("room") or "-"
        print(f"{memory_id}\t{wing}/{room}\t{_snippet(str(hit.get('content') or ''))}")


def _print_memory(hit: dict) -> None:
    memory_id = hit.get("id") or hit.get("drawer_id") or "-"
    print(f"id: {memory_id}")
    print(f"wing: {hit.get('wing') or '-'}")
    print(f"room: {hit.get('room') or '-'}")
    print(f"tenant: {hit.get('tenant_id') or '-'}")
    print(f"content: {hit.get('content') or ''}")


def _snippet(value: str, limit: int = 120) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
