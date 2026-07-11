"""PR2 proposal-source — the server-stamped proposer must reach the durable row, the receipt, and
the approver's pending list; nothing caller-influenced may ever become attribution; legacy and
malformed candidates stay honestly unattributed and never block durability."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from panella import approval_service
from panella.approval_audit import ApprovalAuditContext
from panella.approval_service import ApprovalNotFinalized
from panella.approval_transport import build_transport
from panella.audit import audit_verify_through
from panella.client import MemoryClient
from panella.client_raw import (
    approve_queued_candidate,
    migrate_audit_receipts,
    proposed_by_profile,
)
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.principal import default_tenant_id, principal_default_for_profile, root_principal
from panella.profile import AgentProfile

APPROVER = "local_cli:owner"
TOKEN = "operator-secret"


class RecordingAdapter:
    def __init__(self):
        self.rows = []

    def add_memory(self, wing, room, content, metadata, conversation_id=None):
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append(
            {"id": mid, "content": content, "wing": wing, "room": room,
             "tenant_id": metadata.get("tenant_id"), "metadata": metadata,
             "score": 1.0, "tags": list(metadata.get("tags") or [])}
        )
        return mid

    def find_active_hash_by_marker(self, marker, tenant_id):
        for row in self.rows:
            if marker in (row["tags"] or []):
                return row["id"]
        return None

    def search_memories(self, query, k=5, wings_hint=None, retrieval_mode=None, tenant_ids=None):
        return [r for r in self.rows if query.lower() in str(r["content"]).lower()][:k]


class CrashAfterWriteAdapter(RecordingAdapter):
    def add_memory(self, *args, **kwargs):
        super().add_memory(*args, **kwargs)
        raise RuntimeError("simulated crash after durable write")


class Env:
    def __init__(self, tmp_path, adapter):
        self.outbox = tmp_path / "outbox.db"
        self.audit = tmp_path / "audit.db"
        self.adapter = adapter
        self.governance = current_governance()
        self.transport = build_transport("local_cli", self.governance.approval.transport_config)
        profile = AgentProfile.load("mcp-write")
        self.client = MemoryClient(
            profile, principal_default_for_profile(profile), adapter=adapter,
            outbox_db_path=self.outbox, audit_db_path=self.audit,
        )

    def audit_ctx(self, source="mcp"):
        return ApprovalAuditContext(
            db_path=self.audit, principal=root_principal(),
            tenant_accessed=default_tenant_id(), source=source,
        )

    def seed(self, text="Panella keeps governed memories.", **metadata) -> int:
        result = self.client.write(text, room="preferences", memory_type="owner_preference", **metadata)
        assert result.queued_for_approval and result.approval_id
        return int(result.approval_id)

    def approve(self, approval_id, *, adapter=None):
        return approval_service.approve(
            self.outbox, self.transport, self.governance, TOKEN, approval_id,
            audit=self.audit_ctx(),
            finalizer_adapter_factory=lambda: (adapter if adapter is not None else self.adapter),
        )

    def row(self, approval_id) -> sqlite3.Row:
        with sqlite3.connect(self.outbox) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM approval_queue WHERE id=?", (approval_id,)).fetchone()

    def set_candidate(self, approval_id, candidate_json: str) -> None:
        with sqlite3.connect(self.outbox) as conn:
            conn.execute(
                "UPDATE approval_queue SET candidate_json=? WHERE id=?", (candidate_json, approval_id)
            )

    def insert_raw_candidate(self, candidate: dict) -> int:
        """Hand-insert a queue row (legacy/malformed shapes _enqueue_approval would never produce)."""
        with sqlite3.connect(self.outbox) as conn:
            from panella.client_raw import _ensure_outbox_schema

            conn.row_factory = sqlite3.Row
            _ensure_outbox_schema(conn)
            cur = conn.execute(
                "INSERT INTO approval_queue (candidate_json, status, created_at) "
                "VALUES (?, 'pending_approval', ?)",
                (json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                 datetime.now(UTC).isoformat()),
            )
            return int(cur.lastrowid)


@pytest.fixture
def env(tmp_path, monkeypatch):
    token_file = tmp_path / "approval.token"
    token_file.write_text(TOKEN)
    token_file.chmod(0o600)
    overlay = tmp_path / "governance.yaml"
    overlay.write_text(
        "approval:\n"
        f'  authorized_approvers: ["{APPROVER}"]\n'
        "  transport:\n"
        '    kind: "local_cli"\n'
        "    config:\n"
        f'      token_file: "{token_file}"\n'
        '      token_mode: "0600"\n'
    )
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", str(overlay))
    monkeypatch.setenv("PANELLA_CONFIG_DIR", str(tmp_path / "dist-config"))
    reset_governance_cache()
    render_distribution_config(current_governance(), tmp_path / "dist-config")
    yield Env(tmp_path, RecordingAdapter())
    reset_governance_cache()


# --- 1+7. proposer reaches the durable row, the receipt (== parsed top-level), the pending list ----


def test_proposer_attributed_end_to_end(env):
    listed_id = env.seed("who proposed this?")
    pending = approval_service.list_pending(
        env.outbox, env.transport, env.governance, TOKEN, audit=env.audit_ctx()
    )
    assert pending[0]["approval_id"] == listed_id
    assert pending[0]["proposed_by"] == "mcp-write"  # the approver sees who is asking

    outcome = env.approve(listed_id)
    assert outcome.finalized
    durable = env.adapter.rows[-1]["metadata"]
    assert durable["author_agent_id"] == "mcp-write"
    assert durable["provenance"]["proposed_by_profile"] == "mcp-write"
    # WRITER identity invariants untouched (adapter read-side `agent:` semantics).
    assert durable["agent"] == "panella-finalizer"
    assert durable["agent_profile"] == "panella-finalizer"
    # Receipt projection equals the typed parse of the fingerprint-bound candidate bytes.
    qrow = env.row(listed_id)
    receipt = audit_verify_through(int(qrow["audit_receipt_seq"]), db_path=env.audit)
    parsed = proposed_by_profile(json.loads(qrow["candidate_json"]))
    assert receipt["details"]["proposed_by_profile"] == parsed == "mcp-write"


# --- 2. caller-influenced metadata can NEVER become attribution -----------------------------------


def test_forged_metadata_attribution_still_stripped(env):
    approval_id = env.seed(
        "forged provenance attempt",
        author_agent_id="forged-agent",
        source_bridge="forged-bridge",
        session_id="forged-session",
    )
    env.approve(approval_id)
    durable = env.adapter.rows[-1]["metadata"]
    # Attribution comes ONLY from the server-stamped top-level key — the caller's metadata-level
    # claims are stripped exactly as before PR2.
    assert durable["author_agent_id"] == "mcp-write"
    assert durable["source_bridge"] is None
    assert durable["session_id"] is None


# --- 3. legacy candidate without the stamp: honestly unattributed, never blocked ------------------


def test_legacy_candidate_finalizes_unattributed(env):
    approval_id = env.insert_raw_candidate(
        {"text": "legacy fact", "suggested_wing": "raven", "suggested_room": "feedback",
         "memory_type": "owner_feedback", "metadata": {}}
    )
    outcome = env.approve(approval_id)
    assert outcome.finalized
    durable = env.adapter.rows[-1]["metadata"]
    assert durable["author_agent_id"] is None
    assert "proposed_by_profile" not in durable["provenance"]
    qrow = env.row(approval_id)
    receipt = audit_verify_through(int(qrow["audit_receipt_seq"]), db_path=env.audit)
    assert "proposed_by_profile" not in receipt["details"]


# --- 3b. malformed (non-string / whitespace) stamps never become fake attribution -----------------


@pytest.mark.parametrize("bad_stamp", [["not", "a", "string"], {"profile": "x"}, 7, "   ", ""])
def test_malformed_top_level_stamp_is_unknown(env, bad_stamp):
    approval_id = env.insert_raw_candidate(
        {"text": "malformed stamp", "suggested_wing": "raven", "suggested_room": "feedback",
         "memory_type": "owner_feedback", "metadata": {}, "agent_profile": bad_stamp}
    )
    outcome = env.approve(approval_id)
    assert outcome.finalized
    assert env.adapter.rows[-1]["metadata"]["author_agent_id"] is None


# --- 4. tampering the stamp after approval is caught by the existing fingerprint gate -------------


def test_tampered_proposer_refused_by_fingerprint_gate(env):
    approval_id = env.seed("tamper the proposer")
    with pytest.raises(ApprovalNotFinalized):
        env.approve(approval_id, adapter=CrashAfterWriteAdapter())
    original = env.row(approval_id)["candidate_json"]
    tampered = original.replace('"agent_profile": "mcp-write"', '"agent_profile": "root-looking"')
    assert tampered != original
    env.set_candidate(approval_id, tampered)
    fresh = RecordingAdapter()
    with pytest.raises(ApprovalNotFinalized):
        env.approve(approval_id, adapter=fresh)
    assert fresh.rows == []  # gate refused before any durable write
    assert "candidate bytes altered" in (env.row(approval_id)["finalizer_last_error"] or "")


# --- 5. migration backfill receipts carry the same projection -------------------------------------


def test_backfill_receipt_projects_proposer(env):
    approval_id = env.seed("pre-receipt legacy row")
    approve_queued_candidate(env.outbox, approval_id)  # raw approve: no receipt
    with sqlite3.connect(env.outbox) as conn:
        conn.execute(
            "UPDATE approval_queue SET approved_via='local_cli', approved_by=? WHERE id=?",
            (APPROVER, approval_id),
        )
    result = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(),
        tenant_accessed=default_tenant_id(), adapter=env.adapter,
    )
    assert result.backfilled == 1 and result.remaining == 0
    qrow = env.row(approval_id)
    receipt = audit_verify_through(int(qrow["audit_receipt_seq"]), db_path=env.audit)
    assert receipt["op"] == "approval_decision_backfill"
    assert receipt["details"]["proposed_by_profile"] == "mcp-write"
