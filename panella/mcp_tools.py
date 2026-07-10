"""Shared MCP tool surface for Panella store — used by BOTH the stdio server
(``tools/panella_mcp_server.py``) and the network Streamable-HTTP mount
(``panella/http/app.py`` ``/mcp``), so the two transports cannot drift on which tools exist
or how each one gates auth / provenance / approval.

Tool set (capability-gated at registration — a tool a profile cannot safely back is NOT advertised):

- ``memory.search`` — always. Read gated by the profile's read allowlists (existing behavior).
- ``memory.submit_candidate`` — only when the profile is write-capable AND routes EVERY write to the
  approval queue (``"*" in approval_required_for``). Candidates-only by construction: the tool can
  never produce a direct durable write. Caller metadata is sanitized (strict network blocklist —
  ``source_system`` and identity fields are server-derived, never caller-supplied).
- ``memory.list_pending_approvals`` / ``memory.approve_candidate`` / ``memory.reject_candidate`` —
  only when the deployment's approval transport is MCP-approvable (``local_cli``). Telegram is
  excluded: its Stage-2 anti-forge invariant binds an approval to a bot-sent message id, which the
  MCP surface has no equivalent for — telegram boxes keep approving through the bot. Approve/reject
  require an authorized-approver credential verified through the CONFIGURED transport; the finalizer
  keystone (empty approver set) still refuses every finalize.

This module is a governance fence target: it imports only the standard library, ``mcp.types``, and
``panella.*`` — no panella daemon coupling, no legacy adapters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp.types as mcp_types
from mcp.server import Server

from panella import approval_service
from panella.approval_audit import ApprovalAuditContext
from panella.approval_transport import ApprovalTransport, build_transport
from panella.client import QuotaExceeded
from panella.governance import Governance
from panella.write_hygiene import sanitize_network_write_metadata

logger = logging.getLogger("panella_mcp_tools")

# --- Tool names ----------------------------------------------------------------
TOOL_SEARCH = "memory.search"
TOOL_SUBMIT = "memory.submit_candidate"
TOOL_LIST_PENDING = "memory.list_pending_approvals"
TOOL_APPROVE = "memory.approve_candidate"
TOOL_REJECT = "memory.reject_candidate"

# Transports whose approvals are safe to drive from the MCP surface. local_cli's binding equivalent
# is possession of the 0600 approval token file (verified by the transport); telegram's bot-message
# binding has no MCP analogue, so telegram boxes approve through the bot, never over MCP.
MCP_APPROVABLE_TRANSPORTS = frozenset({"local_cli"})

DEFAULT_K = 5
MAX_K = 50  # hard upper bound (profile may further restrict via max_query_k)
SEARCH_TIMEOUT_SEC = float(os.environ.get("PANELLA_MCP_SEARCH_TIMEOUT_SEC", "5.0"))


# --- Context -------------------------------------------------------------------


@dataclass
class McpToolContext:
    """Everything the tool handlers need, resolved once per server build.

    ``profile``/``governance``/``transport`` are None on the fake-client test path (a bare search
    server); in that case only ``memory.search`` is registered. ``serving`` is the startup
    coherence self-check result — a non-serving box refuses EVERY memory tool (never serves blind)."""

    client: Any
    outbox_db_path: Path
    profile: Any | None = None
    governance: Governance | None = None
    transport: ApprovalTransport | None = None
    serving: bool = True
    serving_reason: str = ""
    # Optional finalizer adapter factory — mirrors finalize_approved_candidate's own adapter_factory
    # seam. None (production) → the finalizer builds its real backend adapter from env; tests inject
    # a fake so the approve path is exercised end-to-end without a live store.
    finalizer_adapter_factory: Any | None = None
    # Audit invariant PR1 — where/who the shared approval service audits as (audit DB path + surface
    # principal + canonical tenant + source="mcp"). REQUIRED for the approval tools: registration is
    # predicated on it (`_approval_registered`), so a context built without an audit sink exposes
    # search/submit only — an unauditable approve surface is never advertised (fail-closed).
    approval_audit: ApprovalAuditContext | None = None


def build_transport_if_approvable(governance: Governance) -> ApprovalTransport | None:
    """Build the deployment's approval transport ONLY if it is MCP-approvable; else None (the
    approve/reject/list tools are not registered). Never raises — a mis-built transport (e.g. a
    local_cli config missing its token_file) degrades to None (tools absent), never a half-armed
    approve surface."""
    kind = governance.approval.transport_kind
    if kind not in MCP_APPROVABLE_TRANSPORTS:
        return None
    try:
        return build_transport(kind, governance.approval.transport_config)
    except Exception as exc:  # noqa: BLE001 — any construction failure = no approve surface
        logger.warning("MCP approval transport %r not constructible; approval tools disabled: %s", kind, exc)
        return None


def _write_capable(profile: Any) -> bool:
    """A profile may submit MCP candidates only if it can write AND routes EVERY write to the
    approval queue. ``"*" in approval_required_for`` is the strict all-writes-queue guarantee
    (``client._approval_required`` fnmatches ``"{wing}/{room}"`` against it, and ``*`` matches any
    pair including the slash), so a submitted write can NEVER become a direct durable write."""
    return (
        not getattr(profile, "finalizer_only", False)
        and bool(getattr(profile, "memory_type_allowlist", []))
        and "*" in getattr(profile, "approval_required_for", [])
    )


def _submit_registered(ctx: McpToolContext) -> bool:
    return ctx.profile is not None and _write_capable(ctx.profile)


def _approval_registered(ctx: McpToolContext) -> bool:
    return ctx.transport is not None and ctx.governance is not None and ctx.approval_audit is not None


# --- Tool listing --------------------------------------------------------------


def list_tools(ctx: McpToolContext) -> list[mcp_types.Tool]:
    tools = [_search_tool()]
    if _submit_registered(ctx):
        tools.append(_submit_tool())
    if _approval_registered(ctx):
        tools.extend([_list_pending_tool(), _approve_tool(), _reject_tool()])
    return tools


def build_mcp_server(ctx: McpToolContext) -> Server:
    """Wire an ``mcp.server.Server`` to this context (list_tools + dispatch). No transport-level
    rate limiting here — the mounting transport owns that (the stdio server wraps its own per-process
    limiter; the ``/mcp`` mount rate-limits per bearer token in its gate). Used by the network mount."""
    server = Server("panella")

    @server.list_tools()
    async def _list() -> list[mcp_types.Tool]:
        return list_tools(ctx)

    @server.call_tool()
    async def _call(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
        return await dispatch(ctx, name, arguments)

    return server


def _search_tool() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_SEARCH,
        description=(
            "Search the Panella store memory store. Returns up to k hits with content, wing, room, "
            "score, and metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "k": {
                    "type": "integer",
                    "description": (
                        f"Max hits to return (default {DEFAULT_K}, max {MAX_K}). "
                        "Out-of-range values are clamped server-side."
                    ),
                },
                "wings_hint": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of wing names to bias the search.",
                },
            },
            "required": ["query"],
        },
    )


def _submit_tool() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_SUBMIT,
        description=(
            "Submit a durable-write CANDIDATE to the approval queue. This NEVER writes durably by "
            "itself — an authorized approver must approve it. Provenance/identity are server-"
            "derived; caller-supplied identity metadata is ignored."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The memory content to store."},
                "room": {"type": "string", "description": "Target room within the owner wing."},
                "memory_type": {"type": "string", "description": "Memory type (must be profile-allowed)."},
                "metadata": {
                    "type": "object",
                    "description": (
                        "Optional metadata. Server-authoritative identity keys "
                        "(source_system, principal_id, …) and reserved control tags are stripped."
                    ),
                },
            },
            "required": ["content", "room", "memory_type"],
        },
    )


def _list_pending_tool() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_LIST_PENDING,
        description=(
            "List pending approval-queue candidates (operator-only; requires an approval "
            "credential). Returns id, wing, room, memory_type, created_at, content preview."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "credential": {"type": "string", "description": "The approval transport credential (e.g. the local_cli token)."},
                "limit": {"type": "integer", "description": "Max rows (default 20, max 100)."},
            },
            "required": ["credential"],
        },
    )


def _approve_tool() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_APPROVE,
        description=(
            "Approve a pending candidate and finalize it durably. Requires an authorized-approver "
            "credential verified through the configured transport."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "integer", "description": "The pending approval id."},
                "credential": {"type": "string", "description": "The approval transport credential."},
            },
            "required": ["approval_id", "credential"],
        },
    )


def _reject_tool() -> mcp_types.Tool:
    return mcp_types.Tool(
        name=TOOL_REJECT,
        description=(
            "Reject a pending candidate. Requires an authorized-approver credential verified "
            "through the configured transport."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "approval_id": {"type": "integer", "description": "The pending approval id."},
                "credential": {"type": "string", "description": "The approval transport credential."},
            },
            "required": ["approval_id", "credential"],
        },
    )


# --- Dispatch ------------------------------------------------------------------


def _error_payload(message: str, *, code: str) -> list[mcp_types.TextContent]:
    return [mcp_types.TextContent(type="text", text=json.dumps({"error": {"code": code, "message": message}}))]


def _ok_payload(data: dict[str, Any]) -> list[mcp_types.TextContent]:
    return [mcp_types.TextContent(type="text", text=json.dumps(data))]


async def dispatch(ctx: McpToolContext, name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    """Route a tool call. Refuses every memory tool when the box failed its coherence self-check
    (never serve blind). Unknown / unregistered tools are refused."""
    if not ctx.serving:
        return _error_payload(
            f"memory not serving (governance/corpus incoherence): {ctx.serving_reason}",
            code="memory_not_serving",
        )
    if name == TOOL_SEARCH:
        return await _handle_search(ctx, arguments)
    if name == TOOL_SUBMIT:
        if not _submit_registered(ctx):
            return _error_payload(f"tool not available: {name}", code="unknown_tool")
        return await _handle_submit(ctx, arguments)
    if name in (TOOL_LIST_PENDING, TOOL_APPROVE, TOOL_REJECT):
        if not _approval_registered(ctx):
            return _error_payload(f"tool not available: {name}", code="unknown_tool")
        if name == TOOL_LIST_PENDING:
            return await _handle_list_pending(ctx, arguments)
        if name == TOOL_APPROVE:
            return await _handle_approve(ctx, arguments)
        return await _handle_reject(ctx, arguments)
    return _error_payload(f"unknown tool: {name}", code="unknown_tool")


async def _handle_search(ctx: McpToolContext, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    query = arguments.get("query") or ""
    if not isinstance(query, str) or not query.strip():
        return [mcp_types.TextContent(type="text", text=json.dumps({"hits": []}))]
    k = arguments.get("k", DEFAULT_K)
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        k = DEFAULT_K
    k = min(k, MAX_K)
    wings_hint = arguments.get("wings_hint")
    if wings_hint is not None and not isinstance(wings_hint, list):
        wings_hint = None
    elif isinstance(wings_hint, list):
        wings_hint = [w for w in wings_hint if isinstance(w, str)] or None

    loop = asyncio.get_running_loop()
    try:
        hits = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ctx.client.search(query, k=k, wings_hint=wings_hint)),
            timeout=SEARCH_TIMEOUT_SEC,
        )
    except TimeoutError:
        logger.warning("search timeout query=%r k=%d", query, k)
        return _error_payload(f"upstream Panella store search timed out after {SEARCH_TIMEOUT_SEC:.1f}s", code="internal_error")
    except Exception as exc:  # noqa: BLE001
        logger.exception("search failed query=%r k=%d", query, k)
        return _error_payload(f"upstream Panella store search failed: {exc}", code="internal_error")
    return [mcp_types.TextContent(type="text", text=json.dumps({"hits": hits or []}))]


async def _handle_submit(ctx: McpToolContext, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    content = arguments.get("content")
    room = arguments.get("room")
    memory_type = arguments.get("memory_type")
    if not isinstance(content, str) or not content.strip():
        return _error_payload("content is required", code="invalid_arguments")
    if not isinstance(room, str) or not room.strip():
        return _error_payload("room is required", code="invalid_arguments")
    if not isinstance(memory_type, str) or not memory_type.strip():
        return _error_payload("memory_type is required", code="invalid_arguments")
    raw_meta = arguments.get("metadata")
    if not isinstance(raw_meta, dict):
        raw_meta = {}
    # Strip server-authoritative identity keys (incl. source_system) + reserved control tags. The
    # remaining provenance is stamped server-side by MemoryClient from profile+principal.
    clean = sanitize_network_write_metadata(raw_meta)

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: ctx.client.write(content, room=room, memory_type=memory_type, **clean)
        )
    except PermissionError as exc:
        return _error_payload(str(exc), code="forbidden")
    except QuotaExceeded as exc:
        # A normal per-profile write-quota throttle — a client condition, not a server failure. Report
        # it explicitly (like the HTTP write route) instead of a stack-traced internal_error.
        return _error_payload(str(exc), code="quota_exceeded")
    except ValueError as exc:
        return _error_payload(str(exc), code="invalid_arguments")
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP submit_candidate write failed")
        return _error_payload(f"submit failed: {exc}", code="internal_error")

    # Candidates-only INVARIANT belt: registration guaranteed "*" in approval_required_for, so this
    # write MUST have queued. A non-queued result is a code-invariant violation (an approval bypass),
    # refused loudly — never reported as a successful durable write.
    if not getattr(result, "queued_for_approval", False):
        logger.error("MCP submit produced a NON-queued write (approval-bypass invariant violated): %r", result)
        return _error_payload("submit did not route to the approval queue; refused", code="internal_error")
    return _ok_payload(
        {
            "queued": True,
            "approval_id": result.approval_id,
            "wing": result.wing,
            "room": result.room,
        }
    )


async def _handle_list_pending(ctx: McpToolContext, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    # Shared trust chain (panella.approval_service): verify presser → authorized-approver gate →
    # bounded read. Listing leaks candidate content, so it needs the SAME authz as approve/reject.
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(
            None,
            lambda: approval_service.list_pending(
                ctx.outbox_db_path, ctx.transport, ctx.governance, arguments.get("credential"),
                audit=ctx.approval_audit,
                limit=arguments.get("limit", 20),
            ),
        )
    except approval_service.ApprovalAuthError as exc:
        # Bad credential OR not an authorized approver — one refusal, no oracle (INERT box with an
        # empty approver set exposes nothing queue-related until approvers are configured).
        return _error_payload(str(exc), code="approval_refused")
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_pending_approvals failed")
        return _error_payload(f"list failed: {exc}", code="internal_error")
    return _ok_payload({"pending": rows})


async def _handle_approve(ctx: McpToolContext, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    approval_id = arguments.get("approval_id")
    if not isinstance(approval_id, int) or isinstance(approval_id, bool):
        return _error_payload("approval_id (int) is required", code="invalid_arguments")
    # Shared trust chain: verify presser → authorized-approver gate → stamp/redrive → finalize under
    # the SAME provenance the finalizer independently re-verifies. approved_via/approved_by are
    # derived ONLY from the configured transport, never from the caller.
    loop = asyncio.get_running_loop()
    try:
        outcome = await loop.run_in_executor(
            None,
            lambda: approval_service.approve(
                ctx.outbox_db_path, ctx.transport, ctx.governance, arguments.get("credential"), approval_id,
                audit=ctx.approval_audit,
                finalizer_adapter_factory=ctx.finalizer_adapter_factory,
            ),
        )
    except approval_service.ApprovalAuthError as exc:
        # Bad credential OR not an authorized approver (keystone: empty approver set lands here).
        return _error_payload(str(exc), code="approval_refused")
    except approval_service.ApprovalStateError as exc:
        # Missing row / not an awaiting-or-retriable candidate (decided/foreign/claimed) → refused.
        return _error_payload(str(exc), code="approval_refused")
    except approval_service.ApprovalNotFinalized:
        # Stamped (or re-driven) but finalize did NOT complete — recoverable; the operator retries and
        # re-enters the redrive path (the only recovery on a self-host box with no background sweep).
        return _error_payload(
            f"approval {approval_id} is approved but not yet durable (finalize did not complete — likely a "
            "transient store issue); retry approve_candidate to redrive it",
            code="not_finalized",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP approve_candidate failed id=%s", approval_id)
        return _error_payload(f"approval failed: {exc}", code="internal_error")
    return _ok_payload(
        {"approved": True, "finalized": True, "durable_id": outcome.durable_id, "retried": outcome.retried}
    )


async def _handle_reject(ctx: McpToolContext, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    approval_id = arguments.get("approval_id")
    if not isinstance(approval_id, int) or isinstance(approval_id, bool):
        return _error_payload("approval_id (int) is required", code="invalid_arguments")
    # Shared trust chain: verify presser → authorized-approver gate → non-terminal reject stamp.
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: approval_service.reject(
                ctx.outbox_db_path, ctx.transport, ctx.governance, arguments.get("credential"), approval_id,
                audit=ctx.approval_audit,
            ),
        )
    except approval_service.ApprovalAuthError as exc:
        return _error_payload(str(exc), code="approval_refused")
    except approval_service.ApprovalStateError as exc:
        # No pending row changed (missing id, or already approved/rejected/finalized) — do NOT report
        # a rejection that did not happen.
        return _error_payload(str(exc), code="approval_refused")
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP reject_candidate failed id=%s", approval_id)
        return _error_payload(f"reject failed: {exc}", code="internal_error")
    return _ok_payload({"rejected": True, "approval_id": approval_id})
