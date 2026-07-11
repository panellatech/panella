"""``panella approvals`` — operate the local double-factor approval queue."""

from __future__ import annotations

import argparse

from panella.cli import _http as cli_http


def register(subparsers: argparse._SubParsersAction) -> None:
    approvals = subparsers.add_parser("approvals", help="List, approve, or reject pending memories.")
    approval_subparsers = approvals.add_subparsers(dest="approvals_command", required=True)

    list_cmd = approval_subparsers.add_parser("list", help="List pending approval candidates.")
    list_cmd.add_argument("--limit", type=int, default=20, help="Maximum candidates to list.")
    cli_http.add_approval_token_file_arg(list_cmd)
    cli_http.add_http_args(list_cmd)
    cli_http.add_json_arg(list_cmd)
    list_cmd.set_defaults(func=_approvals_list)

    approve_cmd = approval_subparsers.add_parser("approve", help="Approve and finalize a candidate.")
    approve_cmd.add_argument("approval_id", type=int, help="Approval id to approve.")
    cli_http.add_approval_token_file_arg(approve_cmd)
    cli_http.add_http_args(approve_cmd)
    cli_http.add_json_arg(approve_cmd)
    approve_cmd.set_defaults(func=_approvals_approve)

    reject_cmd = approval_subparsers.add_parser("reject", help="Reject a pending candidate.")
    reject_cmd.add_argument("approval_id", type=int, help="Approval id to reject.")
    cli_http.add_approval_token_file_arg(reject_cmd)
    cli_http.add_http_args(reject_cmd)
    cli_http.add_json_arg(reject_cmd)
    reject_cmd.set_defaults(func=_approvals_reject)


def _approvals_list(args: argparse.Namespace) -> int:
    try:
        approval_token = cli_http.read_approval_token(args.approval_token_file)
        with cli_http.http_client(args) as client:
            payload = client.approvals_pending(args.limit, approval_token=approval_token)
    except cli_http.CliUsageError as exc:
        return cli_http.handle_usage_error(exc)
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json(payload)
    else:
        _print_pending(payload.get("pending", []))
    return 0


def _approvals_approve(args: argparse.Namespace) -> int:
    try:
        approval_token = cli_http.read_approval_token(args.approval_token_file)
        with cli_http.http_client(args) as client:
            payload = client.approve(args.approval_id, approval_token=approval_token)
    except cli_http.CliUsageError as exc:
        return cli_http.handle_usage_error(exc)
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json(payload)
    else:
        durable_id = payload.get("durable_id") or "-"
        print(f"approved {args.approval_id} durable_id={durable_id}")
    return 0


def _approvals_reject(args: argparse.Namespace) -> int:
    try:
        approval_token = cli_http.read_approval_token(args.approval_token_file)
        with cli_http.http_client(args) as client:
            payload = client.reject(args.approval_id, approval_token=approval_token)
    except cli_http.CliUsageError as exc:
        return cli_http.handle_usage_error(exc)
    except Exception as exc:
        return cli_http.handle_client_exception(exc)
    if args.json:
        cli_http.print_json(payload)
    else:
        print(f"rejected {payload.get('approval_id', args.approval_id)}")
    return 0


def _print_pending(rows: list[dict]) -> None:
    if not rows:
        print("No pending approvals.")
        return
    # BY = the server-stamped proposing profile ("-" for legacy/hand-inserted rows) — the approver
    # sees who is asking in the DEFAULT view, same as the console and the JSON API.
    print("ID\tBY\tWING\tROOM\tTYPE\tCREATED\tPREVIEW")
    for row in rows:
        print(
            "\t".join(
                [
                    str(row.get("approval_id", "")),
                    str(row.get("proposed_by") or "-"),
                    str(row.get("wing") or "-"),
                    str(row.get("room") or "-"),
                    str(row.get("memory_type") or "-"),
                    str(row.get("created_at") or "-"),
                    _snippet(str(row.get("content_preview") or "")),
                ]
            )
        )


def _snippet(value: str, limit: int = 96) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
