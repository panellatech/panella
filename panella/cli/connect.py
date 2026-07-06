"""``panella connect`` - print MCP client configuration snippets."""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.parse
from pathlib import Path

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TOKEN_PLACEHOLDER = "PANELLA_BEARER_HERE"
CLIENTS = ("claude-code", "claude-desktop", "cursor")
DEEPLINKS = ("cursor", "vscode")
OWNER_BEARER_PATH = Path(".panella/owner-bearer")


def register(subparsers: argparse._SubParsersAction) -> None:
    connect = subparsers.add_parser("connect", help="Print MCP client connection snippets.")
    output = connect.add_mutually_exclusive_group(required=True)
    output.add_argument(
        "--print",
        dest="client",
        choices=CLIENTS,
        help="MCP client snippet to print.",
    )
    output.add_argument(
        "--deeplink",
        choices=DEEPLINKS,
        help="Local MCP install deeplink to print.",
    )
    connect.add_argument(
        "--token",
        default=None,
        help="Owner bearer token to inline (default: read .panella/owner-bearer when present).",
    )
    connect.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Facade base URL or /mcp URL (default: {DEFAULT_BASE_URL}).",
    )
    connect.set_defaults(func=_connect_print)


def _connect_print(args: argparse.Namespace) -> int:
    token = _resolve_token(args.token)
    if args.deeplink:
        print(render_deeplink(args.deeplink, token=token, base_url=args.base_url))
        if token != DEFAULT_TOKEN_PLACEHOLDER:
            print("deeplink embeds your bearer — treat the URL as a secret", file=sys.stderr)
        return 0
    print(render_client(args.client, token=token, base_url=args.base_url))
    if token != DEFAULT_TOKEN_PLACEHOLDER:
        print("WARNING: output embeds a live credential; treat it as a secret.", file=sys.stderr)
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


def render_deeplink(kind: str, *, token: str = DEFAULT_TOKEN_PLACEHOLDER, base_url: str = DEFAULT_BASE_URL) -> str:
    url = _mcp_url(base_url)
    auth = f"Bearer {token}"
    if kind == "cursor":
        cfg = {"url": url, "headers": {"Authorization": auth}}
        compact = json.dumps(cfg, separators=(",", ":")).encode()
        encoded = urllib.parse.quote(base64.b64encode(compact).decode(), safe="")
        return f"cursor://anysphere.cursor-deeplink/mcp/install?name=panella&config={encoded}"
    if kind == "vscode":
        obj = {
            "name": "panella",
            "type": "http",
            "url": url,
            "headers": {"Authorization": auth},
        }
        encoded = urllib.parse.quote(json.dumps(obj, separators=(",", ":")), safe="")
        return f"vscode:mcp/install?{encoded}"
    raise ValueError(f"unknown deeplink: {kind}")


def _resolve_token(explicit: str | None) -> str:
    if explicit is not None:
        return explicit
    try:
        raw = OWNER_BEARER_PATH.read_text(encoding="utf-8")
    except OSError:
        _warn_placeholder_token()
        return DEFAULT_TOKEN_PLACEHOLDER
    lines = raw.splitlines()
    if len(lines) != 1:
        _warn_placeholder_token()
        return DEFAULT_TOKEN_PLACEHOLDER
    token = lines[0].strip()
    if not token:
        _warn_placeholder_token()
        return DEFAULT_TOKEN_PLACEHOLDER
    return token


def _warn_placeholder_token() -> None:
    print("panella connect: run panella init (or pass --token) to inline an owner bearer", file=sys.stderr)


def _mcp_url(base_url: str) -> str:
    trimmed = str(base_url).strip().rstrip("/")
    if trimmed.endswith("/mcp"):
        return trimmed
    return f"{trimmed}/mcp"


def _json(value: object) -> str:
    return json.dumps(value, indent=2)
