from __future__ import annotations

import json

import pytest

from panella.approval_audit import ApprovalAuditContext
from panella.approval_transport import build_transport
from panella.client import MemoryClient
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.mcp_tools import TOOL_APPROVE, TOOL_LIST_PENDING, TOOL_SEARCH, TOOL_SUBMIT, McpToolContext, dispatch
from panella.principal import default_tenant_id, principal_default_for_profile, root_principal
from panella.profile import AgentProfile


class RecordingAdapter:
    def __init__(self):
        self.rows = []

    def add_memory(self, wing, room, content, metadata, conversation_id=None):
        mid = f"mem-{len(self.rows) + 1}"
        row = {"id": mid, "content": content, "wing": wing, "room": room, "tenant_id": metadata.get("tenant_id"), "metadata": metadata, "score": 1.0, "tags": ["status:active"]}
        self.rows.append(row)
        return mid

    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        hits = [r for r in self.rows if query.lower() in str(r["content"]).lower()]
        if tenant_ids is not None:
            hits = [r for r in hits if r.get("tenant_id") in set(tenant_ids)]
        return hits[:k]


def _payload(items):
    return json.loads(items[0].text)


@pytest.mark.asyncio
async def test_mcp_submit_approve_read_local_cli(tmp_path, monkeypatch):
    token_file = tmp_path / "approval.token"
    token_file.write_text("operator-secret")
    token_file.chmod(0o600)
    overlay = tmp_path / "governance.yaml"
    overlay.write_text(
        "approval:\n"
        "  authorized_approvers: [\"local_cli:owner\"]\n"
        "  transport:\n"
        "    kind: \"local_cli\"\n"
        "    config:\n"
        f"      token_file: \"{token_file}\"\n"
        "      token_mode: \"0600\"\n"
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(tmp_path / "dist-config"))
    reset_governance_cache()
    render_distribution_config(current_governance(), tmp_path / "dist-config")
    profile = AgentProfile.load("mcp-write")
    adapter = RecordingAdapter()
    ctx = McpToolContext(
        client=MemoryClient(profile, principal_default_for_profile(profile), adapter=adapter, outbox_db_path=tmp_path / "outbox.db", audit_db_path=tmp_path / "audit.db"),
        outbox_db_path=tmp_path / "outbox.db",
        profile=profile,
        governance=current_governance(),
        transport=build_transport("local_cli", {"token_file": str(token_file), "token_mode": "0600"}),
        finalizer_adapter_factory=lambda: adapter,
        approval_audit=ApprovalAuditContext(
            db_path=tmp_path / "audit.db",
            principal=root_principal(),
            tenant_accessed=default_tenant_id(),
            source="mcp",
        ),
    )
    submitted = _payload(await dispatch(ctx, TOOL_SUBMIT, {"content": "Panella keeps governed memories.", "room": "preferences", "memory_type": "owner_preference"}))
    pending = _payload(await dispatch(ctx, TOOL_LIST_PENDING, {"credential": "operator-secret"}))
    approved = _payload(await dispatch(ctx, TOOL_APPROVE, {"approval_id": submitted["approval_id"], "credential": "operator-secret"}))
    read = _payload(await dispatch(ctx, TOOL_SEARCH, {"query": "governed memories", "k": 3}))
    assert submitted["queued"] is True
    assert pending["pending"][0]["approval_id"] == submitted["approval_id"]
    assert approved["approved"] is True
    assert approved["durable_id"] == "mem-1"
    assert read["hits"][0]["id"] == "mem-1"
