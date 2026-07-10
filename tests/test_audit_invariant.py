"""Audit-invariant PR1 failure-injection suite.

THE CLAIM under test: every durable finalization of an approval-required memory candidate,
initiated after invariant activation, is preceded by a committed, chain-verified approval receipt
bound to the exact approved bytes. These tests attack each link — the fail-closed pre-decision
append, the atomic receipt-on-stamp, the finalizer's receipt gate (chain / semantics /
fingerprint), the redrive path, the migration backfill (incl. the three finalizing-row
inspections), idempotent legacy short-circuits, concurrency, and the import surface.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from panella import approval_service
from panella.approval_audit import ApprovalAuditContext
from panella.approval_finalizer import finalize_approved_candidate, redrive_pending_finalizations
from panella.approval_service import (
    ApprovalAuthError,
    ApprovalNotFinalized,
    ApprovalStateError,
)
from panella.approval_transport import build_transport
from panella.audit import (
    AuditChainError,
    _row_hash,
    audit_connect,
    audit_row_hash,
    audit_verify_chain,
    audit_verify_through,
    audit_write,
)
from panella.client import MemoryClient
from panella.client_raw import (
    _backfill_one,
    approve_queued_candidate,
    candidate_fingerprint,
    migrate_audit_receipts,
)
from panella.config_render import render_distribution_config
from panella.governance import current_governance, reset_governance_cache
from panella.mcp_tools import (
    TOOL_APPROVE,
    TOOL_LIST_PENDING,
    TOOL_REJECT,
    TOOL_SEARCH,
    TOOL_SUBMIT,
    McpToolContext,
    dispatch,
    list_tools,
)
from panella.principal import default_tenant_id, principal_default_for_profile, root_principal
from panella.profile import AgentProfile

APPROVER = "local_cli:owner"
TOKEN = "operator-secret"


class RecordingAdapter:
    """Fake durable store: records writes, supports the finalizer's marker lookup."""

    def __init__(self):
        self.rows = []

    def add_memory(self, wing, room, content, metadata, conversation_id=None):
        mid = f"mem-{len(self.rows) + 1}"
        self.rows.append(
            {
                "id": mid,
                "content": content,
                "wing": wing,
                "room": room,
                "tenant_id": metadata.get("tenant_id"),
                "metadata": metadata,
                "score": 1.0,
                "tags": list(metadata.get("tags") or []),
            }
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
    """Simulates a worker dying right after the durable write (before _record)."""

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
            profile,
            principal_default_for_profile(profile),
            adapter=adapter,
            outbox_db_path=self.outbox,
            audit_db_path=self.audit,
        )

    def audit_ctx(self, source="mcp", db_path=None):
        return ApprovalAuditContext(
            db_path=db_path if db_path is not None else self.audit,
            principal=root_principal(),
            tenant_accessed=default_tenant_id(),
            source=source,
        )

    def seed(self, text="Panella keeps governed memories.") -> int:
        result = self.client.write(text, room="preferences", memory_type="owner_preference")
        assert result.queued_for_approval and result.approval_id
        return int(result.approval_id)

    def approve(self, approval_id, *, credential=TOKEN, source="mcp", audit_db=None):
        return approval_service.approve(
            self.outbox,
            self.transport,
            self.governance,
            credential,
            approval_id,
            audit=self.audit_ctx(source, db_path=audit_db),
            finalizer_adapter_factory=lambda: self.adapter,
        )

    def finalize(self, approval_id, *, adapter=None, audit_db=None):
        return finalize_approved_candidate(
            approval_id,
            authorized_approvers={APPROVER},
            expected_approved_via="local_cli",
            db_path=self.outbox,
            adapter_factory=lambda: (adapter if adapter is not None else self.adapter),
            audit_db_path=audit_db if audit_db is not None else self.audit,
        )

    def row(self, approval_id) -> sqlite3.Row:
        with sqlite3.connect(self.outbox) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM approval_queue WHERE id=?", (approval_id,)).fetchone()

    def set_row(self, approval_id, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        with sqlite3.connect(self.outbox) as conn:
            conn.execute(f"UPDATE approval_queue SET {sets} WHERE id=?", (*cols.values(), approval_id))

    def audit_ops(self) -> list[tuple[str, str | None]]:
        with sqlite3.connect(self.audit) as conn:
            return [
                (str(r[0]), r[1])
                for r in conn.execute("SELECT op, target_id FROM audit_log ORDER BY seq").fetchall()
            ]


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


# --- 1. audit-fail-before-mutation ⇒ no approved row + no durable write ---------------------------


def test_1_audit_append_failure_aborts_before_any_mutation(env, tmp_path):
    approval_id = env.seed()
    unwritable = tmp_path / "not-a-db-dir"
    unwritable.mkdir()  # a directory: sqlite cannot open it as a database → append fails
    with pytest.raises(Exception):
        env.approve(approval_id, audit_db=unwritable)
    row = env.row(approval_id)
    assert row["status"] == "pending_approval"  # never flipped
    assert row["memory_event_id"] is None  # no event emitted
    assert row["audit_receipt_seq"] is None
    assert env.adapter.rows == []  # nothing durable


# --- 2. crash after durable write ⇒ the receipt PRECEDES the write (chain already carries it) -----


def test_2_crash_after_write_receipt_already_committed(env):
    approval_id = env.seed()
    crash_adapter = CrashAfterWriteAdapter()
    with pytest.raises(ApprovalNotFinalized):
        approval_service.approve(
            env.outbox, env.transport, env.governance, TOKEN, approval_id,
            audit=env.audit_ctx(), finalizer_adapter_factory=lambda: crash_adapter,
        )
    # The durable write DID happen before the "crash"...
    assert len(crash_adapter.rows) == 1
    # ...and the committed, chain-verified receipt PRECEDES it (the invariant claim).
    row = env.row(approval_id)
    assert row["audit_receipt_seq"] is not None
    receipt = audit_verify_through(int(row["audit_receipt_seq"]), db_path=env.audit)
    assert receipt["details"]["decision"] == "approve"
    assert receipt["details"]["candidate_sha256"] == row["candidate_sha256"]
    # Recovery: a redrive re-enters through the gate and completes.
    outcome = env.approve(approval_id)
    assert outcome.finalized and outcome.retried


# --- 3. finalizer refuses a NULL receipt ----------------------------------------------------------


def test_3_finalizer_refuses_null_receipt(env):
    approval_id = env.seed()
    # Legacy-shaped row: approved + provenanced via the raw primitive + hand-stamp, NO receipt.
    approve_queued_candidate(env.outbox, approval_id)
    env.set_row(approval_id, approved_via="local_cli", approved_by=APPROVER)
    assert env.finalize(approval_id) is None
    row = env.row(approval_id)
    assert row["finalizer_state"] == "failed"
    assert "audit receipt gate" in (row["finalizer_last_error"] or "")
    assert env.adapter.rows == []


# --- 4. finalizer refuses FORGED / wrong receipts -------------------------------------------------


def _stamped_unfinalized(env) -> int:
    """A row that passed the real approve path (receipt stored) but whose finalize crashed —
    the canonical target for gate-tampering tests."""
    approval_id = env.seed()
    with pytest.raises(ApprovalNotFinalized):
        approval_service.approve(
            env.outbox, env.transport, env.governance, TOKEN, approval_id,
            audit=env.audit_ctx(), finalizer_adapter_factory=CrashAfterWriteAdapter,
        )
    return approval_id


def test_4a_gate_refuses_receipt_for_another_approval(env):
    a = _stamped_unfinalized(env)
    b = _stamped_unfinalized(env)
    row_b = env.row(b)
    # Point A's receipt at B's (valid chain row, wrong target).
    env.set_row(a, audit_receipt_seq=row_b["audit_receipt_seq"], audit_receipt_hash=row_b["audit_receipt_hash"])
    assert env.finalize(a) is None
    assert "receipt target mismatch" in (env.row(a)["finalizer_last_error"] or "")


def test_4b_gate_refuses_reject_decision_receipt(env):
    a = _stamped_unfinalized(env)
    row = env.row(a)
    seq = audit_write(
        principal=root_principal(), tenant_accessed=default_tenant_id(), op="approval_decision",
        target_id=str(a),
        details={"phase": "authorized_intent", "decision": "reject", "approval_id": a,
                 "approved_by": row["approved_by"], "approved_via": row["approved_via"],
                 "candidate_sha256": row["candidate_sha256"]},
        db_path=env.audit,
    )
    env.set_row(a, audit_receipt_seq=seq, audit_receipt_hash=audit_row_hash(seq, db_path=env.audit))
    assert env.finalize(a) is None
    assert "receipt decision mismatch" in (env.row(a)["finalizer_last_error"] or "")


def test_4c_gate_refuses_chain_broken_below_receipt(env):
    a = _stamped_unfinalized(env)
    # Corrupt an EARLIER row (seq 1 < receipt seq): the bounded genesis→seq walk must fail.
    with sqlite3.connect(env.audit) as conn:
        conn.execute("UPDATE audit_log SET details_json='tampered' WHERE seq=1")
    assert env.finalize(a) is None
    assert "receipt unverifiable" in (env.row(a)["finalizer_last_error"] or "")


def test_4d_gate_refuses_recreated_audit_db(env):
    a = _stamped_unfinalized(env)
    env.audit.unlink()  # attacker replaces the audit DB wholesale
    for i in range(int(env.row(a)["audit_receipt_seq"])):
        audit_write(principal=root_principal(), tenant_accessed=default_tenant_id(),
                    op="noise", target_id=str(i), details={"i": i}, db_path=env.audit)
    assert env.finalize(a) is None
    err = env.row(a)["finalizer_last_error"] or ""
    assert "receipt hash mismatch" in err or "receipt unverifiable" in err
    assert env.adapter.rows == []


def test_4e_gate_refuses_altered_candidate_bytes(env):
    a = _stamped_unfinalized(env)
    original = env.row(a)["candidate_json"]
    altered = original.replace("governed memories", "ATTACKER PAYLOAD")
    assert altered != original
    env.set_row(a, candidate_json=altered)
    assert env.finalize(a) is None
    assert "candidate bytes altered" in (env.row(a)["finalizer_last_error"] or "")
    assert env.adapter.rows == []


# --- 5. the redrive sweep hits the same gate ------------------------------------------------------


def test_5_redrive_sweep_hits_gate(env):
    approval_id = env.seed()
    approve_queued_candidate(env.outbox, approval_id)
    env.set_row(approval_id, approved_via="local_cli", approved_by=APPROVER)  # legacy, no receipt
    finalized = redrive_pending_finalizations(
        authorized_approvers={APPROVER}, db_path=env.outbox,
        adapter_factory=lambda: env.adapter, expected_approved_via="local_cli",
        audit_db_path=env.audit,
    )
    assert finalized == 0
    assert env.adapter.rows == []
    assert "audit receipt gate" in (env.row(approval_id)["finalizer_last_error"] or "")


# --- 6. reject needs a prior record ---------------------------------------------------------------


def test_6_reject_requires_committed_record(env, tmp_path):
    approval_id = env.seed()
    unwritable = tmp_path / "reject-not-a-db"
    unwritable.mkdir()
    with pytest.raises(Exception):
        approval_service.reject(
            env.outbox, env.transport, env.governance, TOKEN, approval_id,
            audit=env.audit_ctx(db_path=unwritable),
        )
    assert env.row(approval_id)["status"] == "pending_approval"  # reject did NOT happen
    # With a writable audit DB the same reject lands, pre-recorded.
    approval_service.reject(
        env.outbox, env.transport, env.governance, TOKEN, approval_id, audit=env.audit_ctx()
    )
    assert env.row(approval_id)["status"] == "rejected"
    ops = env.audit_ops()
    assert ("approval_decision", str(approval_id)) in ops


# --- 7. missing approval_audit ⇒ approval tools are not even advertised ---------------------------


@pytest.mark.asyncio
async def test_7_missing_audit_context_means_search_only_surface(env):
    ctx = McpToolContext(
        client=env.client,
        outbox_db_path=env.outbox,
        profile=AgentProfile.load("mcp-write"),
        governance=env.governance,
        transport=env.transport,
        approval_audit=None,  # explicit: no audit sink wired
    )
    names = {tool.name for tool in list_tools(ctx)}
    assert TOOL_SEARCH in names and TOOL_SUBMIT in names
    assert not names & {TOOL_APPROVE, TOOL_REJECT, TOOL_LIST_PENDING}
    refused = json.loads((await dispatch(ctx, TOOL_APPROVE, {"approval_id": 1, "credential": TOKEN}))[0].text)
    assert refused["error"]["code"] == "unknown_tool"


# --- 8. concurrent approvals: one durable row; attempts ≠ outcomes; chain valid --------------------


def test_8_concurrent_approvals_single_outcome_chain_valid(env):
    approval_id = env.seed()
    errors: list[BaseException] = []

    def worker():
        # A busy-lock timeout is a test-environment artifact, not a loser outcome — retry it so
        # both ATTEMPTS always reach the audit chain (the assertion this test exists for).
        for attempt in range(3):
            try:
                env.approve(approval_id)
                return
            except (ApprovalNotFinalized, ApprovalStateError) as exc:
                errors.append(exc)  # acceptable loser outcomes
                return
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly one durable row, whatever the interleaving.
    assert len(env.adapter.rows) == 1
    assert env.row(approval_id)["finalizer_state"] == "finalized"
    assert audit_verify_chain(env.audit) is True
    ops = env.audit_ops()
    decisions = [op for op, target in ops if op == "approval_decision" and target == str(approval_id)]
    outcomes = [op for op, _ in ops if op == "approval_finalized"]
    assert len(decisions) == 2  # both ATTEMPTS recorded
    assert len(outcomes) == 1  # exactly one OUTCOME


# --- 9. migration backfill ------------------------------------------------------------------------


def _legacy_approved(env, *, finalizer_state=None) -> int:
    approval_id = env.seed()
    approve_queued_candidate(env.outbox, approval_id)
    env.set_row(approval_id, approved_via="local_cli", approved_by=APPROVER)
    if finalizer_state is not None:
        env.set_row(approval_id, finalizer_state=finalizer_state, finalizer_worker_id="dead-worker",
                    finalizer_claimed_at="2020-01-01T00:00:00+00:00")
    return approval_id


def test_9a_backfill_makes_legacy_rows_finalizable(env):
    approval_id = _legacy_approved(env)
    result = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(), tenant_accessed=default_tenant_id(),
        adapter=env.adapter,
    )
    assert (result.eligible, result.backfilled, result.remaining) == (1, 1, 0)
    row = env.row(approval_id)
    receipt = audit_verify_through(int(row["audit_receipt_seq"]), db_path=env.audit)
    assert receipt["op"] == "approval_decision_backfill"
    assert receipt["details"]["candidate_sha256"] == candidate_fingerprint(row["candidate_json"])
    # …and the row now finalizes through the gate.
    assert env.finalize(approval_id) is not None
    assert len(env.adapter.rows) == 1


def test_9b_crash_between_append_and_stamp_reruns_clean(env):
    approval_id = _legacy_approved(env)
    row = env.row(approval_id)
    # Simulate a migrator that died AFTER appending the backfill record but BEFORE stamping.
    audit_write(
        principal=root_principal(), tenant_accessed=default_tenant_id(),
        op="approval_decision_backfill", target_id=str(approval_id),
        details={"phase": "backfill", "decision": "approve", "approval_id": approval_id,
                 "approved_by": APPROVER, "approved_via": "local_cli",
                 "candidate_sha256": candidate_fingerprint(row["candidate_json"])},
        db_path=env.audit,
    )
    result = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(), tenant_accessed=default_tenant_id(),
        adapter=env.adapter,
    )
    assert result.remaining == 0 and result.backfilled == 1
    assert audit_verify_chain(env.audit) is True  # the orphan append stays; chain intact
    assert env.finalize(approval_id) is not None


def test_9c_finalizing_inspection_found_and_absent(env):
    found_id = _legacy_approved(env, finalizer_state="finalizing")
    absent_id = _legacy_approved(env, finalizer_state="finalizing")
    # The crashed worker DID write the durable row for found_id (marker present).
    env.adapter.rows.append(
        {"id": "mem-orphan", "content": "x", "wing": "w", "room": "r", "tenant_id": default_tenant_id(),
         "metadata": {}, "score": 1.0, "tags": [f"approval_ref:{found_id}"]}
    )
    result = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(), tenant_accessed=default_tenant_id(),
        adapter=env.adapter,
    )
    assert result.remaining == 0 and result.backfilled == 2
    found_receipt = audit_verify_through(int(env.row(found_id)["audit_receipt_seq"]), db_path=env.audit)
    absent_receipt = audit_verify_through(int(env.row(absent_id)["audit_receipt_seq"]), db_path=env.audit)
    assert found_receipt["details"]["finalizing_inspection"] == "marker_found"
    assert absent_receipt["details"]["finalizing_inspection"] == "marker_absent"


def test_9d_finalizing_lookup_failure_refuses_activation(env):
    approval_id = _legacy_approved(env, finalizer_state="finalizing")

    class BrokenLookupAdapter(RecordingAdapter):
        def find_active_hash_by_marker(self, marker, tenant_id):
            raise RuntimeError("store unreachable")

    result = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(), tenant_accessed=default_tenant_id(),
        adapter=BrokenLookupAdapter(),
    )
    assert result.backfilled == 0
    assert result.remaining == 1  # caller MUST refuse activation
    assert env.row(approval_id)["audit_receipt_seq"] is None
    # No-adapter passes are equally indeterminate for finalizing rows → same refusal.
    result2 = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(), tenant_accessed=default_tenant_id(),
        adapter=None,
    )
    assert result2.remaining == 1


# --- 10. already-finalized legacy row (NULL receipt) is idempotent, never re-written --------------


def test_10_already_finalized_null_receipt_idempotent(env):
    approval_id = _legacy_approved(env)
    env.set_row(approval_id, finalizer_state="finalized", durable_memory_id="mem-legacy")
    assert env.finalize(approval_id) == "mem-legacy"
    assert env.adapter.rows == []  # attest, don't double-write
    # And the migrator does not consider it eligible (nothing to backfill).
    result = migrate_audit_receipts(
        env.outbox, env.audit, principal=root_principal(), tenant_accessed=default_tenant_id(),
        adapter=env.adapter,
    )
    assert (result.eligible, result.remaining) == (0, 0)


# --- 11. interleaved surfaces (http+mcp ctx) keep ONE valid chain ---------------------------------


def test_11_interleaved_surfaces_chain_valid(env):
    a = env.seed("first governed fact")
    b = env.seed("second governed fact")
    approval_service.list_pending(
        env.outbox, env.transport, env.governance, TOKEN, audit=env.audit_ctx("http")
    )
    assert env.approve(a, source="http").finalized
    with pytest.raises(ApprovalAuthError):
        approval_service.approve(
            env.outbox, env.transport, env.governance, "wrong-token", b,
            audit=env.audit_ctx("mcp"), finalizer_adapter_factory=lambda: env.adapter,
        )
    approval_service.reject(
        env.outbox, env.transport, env.governance, TOKEN, b, audit=env.audit_ctx("mcp")
    )
    assert audit_verify_chain(env.audit) is True
    ops = [op for op, _ in env.audit_ops()]
    for expected in ("approval_list", "approval_decision", "approval_finalized", "approval_refused"):
        assert expected in ops
    # Bounded verify agrees with the full walk for every receipt in the chain.
    row = env.row(a)
    assert audit_verify_through(int(row["audit_receipt_seq"]), db_path=env.audit)["target_id"] == str(a)


# --- 12. import surface: leaf direction holds, no cycle either import order -----------------------


def test_12_import_surface_no_cycle():
    for order in (
        "import panella.approval_service, panella.approval_finalizer, panella.client_raw, panella.audit, panella.approval_audit",
        "import panella.approval_audit, panella.audit, panella.client_raw, panella.approval_finalizer, panella.approval_service",
    ):
        proc = subprocess.run(
            [sys.executable, "-c", order],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert proc.returncode == 0, proc.stderr
    # The leaf modules the receipt depends on must not import the service/finalizer layers.
    import panella.approval_audit as leaf_ctx
    import panella.audit as leaf_audit

    for leaf in (leaf_ctx, leaf_audit):
        source = Path(leaf.__file__).read_text(encoding="utf-8")
        for forbidden in ("approval_service", "approval_finalizer", "client_raw", "mcp_tools", "http"):
            assert f"panella.{forbidden}" not in source


# --- 9e/9f. migration serialization against a live legacy finalizer (Codex terra R1 P1) -----------


def test_9e_backfill_revalidates_under_lock_skips_stale_row(env):
    approval_id = _legacy_approved(env)
    # Between the eligibility snapshot and per-row processing, an "external" finalizer finished the
    # row. _backfill_one must re-validate under its own lock and refuse to stamp a stale premise.
    env.set_row(approval_id, finalizer_state="finalized", durable_memory_id="mem-external")
    with audit_connect(env.audit) as conn:
        before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    stamped = _backfill_one(
        env.outbox, env.audit, approval_id,
        principal=root_principal(), tenant_accessed=default_tenant_id(), adapter=env.adapter,
    )
    assert stamped is False
    assert env.row(approval_id)["audit_receipt_seq"] is None
    with audit_connect(env.audit) as conn:
        after = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before  # no attestation appended for a row it did not stamp


def test_9f_backfill_lock_blocks_concurrent_claim(env):
    """The serialization property itself: while _backfill_one processes a finalizing row (store
    inspection in flight), a legacy finalizer's claim CAS on the same outbox must be REFUSED by the
    held BEGIN IMMEDIATE — the exact interleaving that could otherwise attach a receipt after an
    uninspected durable write."""
    approval_id = _legacy_approved(env, finalizer_state="finalizing")
    inside_lookup = threading.Event()
    release_lookup = threading.Event()

    class BlockingLookupAdapter(RecordingAdapter):
        def find_active_hash_by_marker(self, marker, tenant_id):
            inside_lookup.set()
            assert release_lookup.wait(timeout=10)
            return None  # marker absent

    result: dict[str, object] = {}

    def run_backfill():
        result["stamped"] = _backfill_one(
            env.outbox, env.audit, approval_id,
            principal=root_principal(), tenant_accessed=default_tenant_id(),
            adapter=BlockingLookupAdapter(),
        )

    worker = threading.Thread(target=run_backfill)
    worker.start()
    try:
        assert inside_lookup.wait(timeout=10)
        # Migrator holds BEGIN IMMEDIATE on the outbox → a concurrent (legacy) claim must fail.
        contender = sqlite3.connect(env.outbox, timeout=0.4)
        try:
            with pytest.raises(sqlite3.OperationalError):
                contender.execute("BEGIN IMMEDIATE")
        finally:
            contender.close()
    finally:
        release_lookup.set()
        worker.join(timeout=10)
    assert result["stamped"] is True
    receipt = audit_verify_through(int(env.row(approval_id)["audit_receipt_seq"]), db_path=env.audit)
    assert receipt["details"]["finalizing_inspection"] == "marker_absent"


# --- gate error taxonomy: AuditChainError from a plain bounded verify stays precise ---------------


def test_bounded_verify_rejects_nonexistent_and_gap(env):
    _ = env.seed()
    with pytest.raises(AuditChainError):
        audit_verify_through(10_000, db_path=env.audit)
    with pytest.raises(AuditChainError):
        audit_verify_through(0, db_path=env.audit)


def test_bounded_verify_rejects_seq_gap_with_valid_hashes(env):
    """Codex terra R1 P1: hash links alone cannot see a deleted row whose neighbors re-link. Craft
    a row at seq 3 whose prev_hash correctly links to seq 1 (seq 2 missing) — the bounded walk must
    refuse on contiguity, not accept the re-linked chain."""
    seq1 = audit_write(principal=root_principal(), tenant_accessed=default_tenant_id(),
                       op="noise", target_id="1", details={"n": 1}, db_path=env.audit)
    hash1 = audit_row_hash(seq1, db_path=env.audit)
    forged = {
        "seq": 3,
        "ts_iso": "2026-07-10T00:00:00+00:00",
        "principal_id": "attacker",
        "tenant_accessed": default_tenant_id(),
        "op": "approval_decision",
        "target_id": "1",
        "reason_code": None,
        "details_json": None,
        "prev_hash": hash1,
    }
    forged_hash = _row_hash(hash1, forged)
    with sqlite3.connect(env.audit) as conn:
        conn.execute(
            "INSERT INTO audit_log (seq, ts_iso, principal_id, tenant_accessed, op, target_id,"
            " reason_code, details_json, prev_hash, this_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*forged.values(), forged_hash),
        )
    with pytest.raises(AuditChainError, match="gap"):
        audit_verify_through(3, db_path=env.audit)
    # The FULL walk (backup/CLI validation) must apply the same contiguity rule — "chain verified"
    # means one thing everywhere (Codex R2 residual).
    with pytest.raises(AuditChainError, match="gap"):
        audit_verify_chain(env.audit)
