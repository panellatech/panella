"""``panella connect`` - print MCP client configuration snippets."""

from __future__ import annotations

import argparse
import json

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TOKEN_PLACEHOLDER = "PANELLA_BEARER_HERE"
CLIENTS = ("claude-code", "claude-desktop", "cursor")


def register(subparsers: argparse._SubParsersAction) -> None:
    connect = subparsers.add_parser("connect", help="Print MCP client connection snippets.")
    connect.add_argument(
        "--print",
        dest="client",
        choices=CLIENTS,
        required=True,
        help="MCP client snippet to print.",
    )
    connect.add_argument(
        "--token",
        default=DEFAULT_TOKEN_PLACEHOLDER,
        help=f"Owner bearer token to inline (default: {DEFAULT_TOKEN_PLACEHOLDER}).",
    )
    connect.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Facade base URL or /mcp URL (default: {DEFAULT_BASE_URL}).",
    )
    connect.set_defaults(func=_connect_print)


def _connect_print(args: argparse.Namespace) -> int:
    print(render_client(args.client, token=args.token, base_url=args.base_url))
    return 0


def render_client(client: str, *, token: str = DEFAULT_TOKEN_PLACEHOLDER, base_url: str = DEFAULT_BASE_URL) -> str:
    url = _mcp_url(base_url)
    auth = f"Bearer {token}"
    if client == "claude-code":
        return f'claude mcp add --transport http panella {url} --header "Authorization: {auth}"'
    if client == "claude-desktop":
        return _json(
            {
                "mcpServers": {
                    "panella": {
                        "type": "http",
                        "url": url,
                        "headers": {"Authorization": auth},
                    }
                }
            }
        )
    if client == "cursor":
        return _json(
            {
                "mcpServers": {
                    "panella": {
                        "url": url,
                        "headers": {"Authorization": auth},
                    }
                }
            }
        )
    raise ValueError(f"unknown client: {client}")


def _mcp_url(base_url: str) -> str:
    trimmed = str(base_url).strip().rstrip("/")
    if trimmed.endswith("/mcp"):
        return trimmed
    return f"{trimmed}/mcp"


def _json(value: object) -> str:
    return json.dumps(value, indent=2)
