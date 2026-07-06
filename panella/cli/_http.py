"""Shared HTTP CLI helpers for operator commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_APPROVAL_TOKEN_FILE = Path(".panella/approval-token")


class CliUsageError(Exception):
    pass


def add_http_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token",
        default=None,
        help="Owner bearer token (default: PANELLA_BEARER; missing bearer is surfaced by the server).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"Facade base URL (default: PANELLA_BASE_URL or {DEFAULT_BASE_URL}).",
    )


def add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Print raw response JSON.")


def add_approval_token_file_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--approval-token-file",
        type=Path,
        default=DEFAULT_APPROVAL_TOKEN_FILE,
        help=f"Approval token file (default: {DEFAULT_APPROVAL_TOKEN_FILE}).",
    )


def base_url(args: argparse.Namespace) -> str:
    return str(args.base_url or os.environ.get("PANELLA_BASE_URL") or DEFAULT_BASE_URL)


def bearer(args: argparse.Namespace) -> str | None:
    value = args.token if args.token is not None else os.environ.get("PANELLA_BEARER")
    return value.strip() if isinstance(value, str) and value.strip() else None


def make_client(args: argparse.Namespace) -> Any:
    from panella.http_client import MemoryHttpClient

    return MemoryHttpClient(base_url=base_url(args), token=bearer(args))


@contextmanager
def http_client(args: argparse.Namespace) -> Iterator[Any]:
    client = make_client(args)
    try:
        yield client
    finally:
        client.close()


def read_approval_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise CliUsageError(
            f"approval token file not found: {path} — run `panella init` or pass --approval-token-file."
        ) from exc
    except OSError as exc:
        raise CliUsageError(f"cannot read approval token file: {path} — {exc.strerror or exc}") from exc
    if not token:
        raise CliUsageError(f"approval token file is empty: {path}")
    return token


def print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def handle_usage_error(exc: CliUsageError) -> int:
    print(str(exc), file=sys.stderr)
    return 2


def handle_http_error(exc: Any) -> int:
    print(_response_message(exc.response), file=sys.stderr)
    return 1


def handle_request_error(exc: Any) -> int:
    print(f"request failed: {exc}", file=sys.stderr)
    return 1


def handle_client_exception(exc: Exception) -> int:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return handle_http_error(exc)
    if isinstance(exc, httpx.RequestError):
        return handle_request_error(exc)
    raise exc


def _response_message(response: Any) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        message = payload.get("message")
        if message:
            return str(message)
        code = payload.get("code")
        if code:
            return str(code)
    return f"HTTP {response.status_code}"
