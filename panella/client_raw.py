"""Small in-process client for Panella daemon memory access."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from panella.audit import audit_row_hash, audit_write
from panella.governance import Governance, current_governance
from panella.principal import Principal, default_tenant_id

logger = logging.getLogger(__name__)

ROOT = Path(os.environ.get("PANELLA_ROOT", Path(__file__).resolve().parents[1]))
CONFIG_PATH = ROOT / "config" / "panella.yaml"
OUTBOX_DB_PATH = ROOT / "data" / "memory_outbox.db"
SCHEMA_PATH = ROOT / "panella" / "memory_outbox_schema.sql"
VALID_WRITER_MODES = {"direct", "outbox", "dual"}
# Stage 2 P0 — the finalizer's claim-lease TTL. Defined here (not in approval_finalizer) so the
# RTBF path in client.py can reference it WITHOUT importing approval_finalizer (which would be a
# circular import). approval_finalizer re-exports it as STALE_TTL_SECONDS.
FINALIZER_STALE_TTL_SECONDS = 300
_WRITER_MODE: str | None = None


def resolve_cron_memory_wing(cron: Any) -> str:
    """Resolve the memory wing used for cron runtime memories."""
    configured_wing = getattr(cron, "wing", None)
    if configured_wing:
        return str(configured_wing)

    cron_name = str(getattr(cron, "name", ""))
    if cron_name.startswith(("fred-", "cn-12x30-", "eth-")):
        return "quant"
    if cron_name.startswith("iris-"):
        return "iris"
    return "default"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _transport_kind() -> str:
    """The configured approval transport name — the default ``decided_by`` stamp and the
    ``approved_via`` vocabulary (``governance approval.transport.kind``)."""
    return current_governance().approval.transport_kind


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def candidate_fingerprint(candidate_json: str) -> str:
    """THE canonical fingerprint of an approval candidate: sha256 over the STORED
    ``candidate_json`` text, utf-8 encoded. Defined ONCE so the approve-time receipt, the
    in-transaction stamp recheck, the migration backfill, and the finalizer gate all hash the
    exact same bytes — the row's stored string IS the canonical form (no re-serialization, which
    could differ by key order / whitespace and open a false-mismatch or false-match window)."""
    return hashlib.sha256(str(candidate_json).encode("utf-8")).hexdigest()


def proposed_by_profile(value: Any) -> str | None:
    """Typed read of a proposer-attribution value (the ``proposed_by_profile`` queue COLUMN, or
    the same-named pre-decision receipt detail): only a non-empty string counts — anything else
    (NULL, list/dict, whitespace) is None = honestly unattributed. The column is written ONLY by
    ``MemoryClient._enqueue_approval`` from the authenticated profile, so a hand-inserted queue
    row (which by definition did not pass the server enqueue path) carries no attribution — it is
    NOT derived from candidate_json, where a hand-crafted row could plant a plausible string
    (Codex terra PR2 P1). Attribution then flows column → receipt (projected into the hash-chained
    ``approval_decision`` details at approve time) → durable metadata (the finalizer reads the
    GATE-VERIFIED receipt, never the mutable column). Trust wording: server-stamped provenance
    within the existing trusted-process boundary — a writer with direct DB access could still
    forge the column pre-approval (same boundary as every other queue field, and the approver sees
    the attribution in the pending list before deciding); NOT cryptographic proposer identity.
    Single definition — the service projection, the migration backfill projection, the pending
    list, and the finalizer's receipt read all call THIS."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


_SHA256_HEX_LEN = 64


def _require_receipt(seq: Any, this_hash: Any, sha256_hex: Any) -> tuple[int, str, str]:
    """Validate an approval receipt triple (fail-closed). The stamp path REQUIRES a well-formed
    receipt; a malformed one must abort before any queue mutation."""
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise ValueError(f"invalid audit receipt seq: {seq!r}")
    for label, value in (("hash", this_hash), ("candidate_sha256", sha256_hex)):
        text = str(value or "")
        if len(text) != _SHA256_HEX_LEN or any(c not in "0123456789abcdef" for c in text):
            raise ValueError(f"invalid audit receipt {label}: {value!r}")
    return seq, str(this_hash), str(sha256_hex)


def _deterministic_memory_id(wing: str, room: str, content: str) -> str:
    return f"drawer_{wing}_{room}_{_content_hash(content)[:16]}"


def _load_writer_mode(config_path: Path = CONFIG_PATH) -> str:
    global _WRITER_MODE
    if _WRITER_MODE is not None:
        return _WRITER_MODE

    mode = "direct"
    try:
        if config_path.exists():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            configured = str(raw.get("writer_mode", "direct")).strip().lower()
            if configured in VALID_WRITER_MODES:
                mode = configured
            else:
                logger.warning("Invalid memory writer_mode=%r; using direct", configured)
    except Exception as exc:
        logger.warning("Failed to read %s; using direct memory writer_mode: %s", config_path, exc)

    _WRITER_MODE = mode
    return mode


def _ensure_outbox_schema(conn: sqlite3.Connection) -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_events)").fetchall()}
    event_migrations = {
        "processing_started_at": "ALTER TABLE memory_events ADD COLUMN processing_started_at TEXT",
        "next_attempt_at": "ALTER TABLE memory_events ADD COLUMN next_attempt_at TEXT",
        "completed_memory_id": "ALTER TABLE memory_events ADD COLUMN completed_memory_id TEXT",
        "shadow": "ALTER TABLE memory_events ADD COLUMN shadow INTEGER NOT NULL DEFAULT 0",
        "tenant_id": "ALTER TABLE memory_events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 't_owner_personal'",
        "principal_id": "ALTER TABLE memory_events ADD COLUMN principal_id TEXT",
    }
    for column, sql in event_migrations.items():
        if column not in event_columns:
            conn.execute(sql)

    approval_columns = {row[1] for row in conn.execute("PRAGMA table_info(approval_queue)").fetchall()}
    approval_migrations = {
        "tg_last_sent_at": "ALTER TABLE approval_queue ADD COLUMN tg_last_sent_at TEXT",
        "tg_message_ids": "ALTER TABLE approval_queue ADD COLUMN tg_message_ids TEXT",
        "memory_event_id": "ALTER TABLE approval_queue ADD COLUMN memory_event_id INTEGER",
        "decided_by": "ALTER TABLE approval_queue ADD COLUMN decided_by TEXT",
        "last_error": "ALTER TABLE approval_queue ADD COLUMN last_error TEXT",
        # Stage 2 P0 — handler-authorized provenance + durable-finalizer state.
        "approved_via": "ALTER TABLE approval_queue ADD COLUMN approved_via TEXT",
        "approved_by": "ALTER TABLE approval_queue ADD COLUMN approved_by TEXT",
        "approved_tg_message_id": "ALTER TABLE approval_queue ADD COLUMN approved_tg_message_id INTEGER",
        "finalizer_state": "ALTER TABLE approval_queue ADD COLUMN finalizer_state TEXT",
        "finalizer_worker_id": "ALTER TABLE approval_queue ADD COLUMN finalizer_worker_id TEXT",
        "finalizer_claimed_at": "ALTER TABLE approval_queue ADD COLUMN finalizer_claimed_at TEXT",
        "finalizer_attempt_count": "ALTER TABLE approval_queue ADD COLUMN finalizer_attempt_count INTEGER NOT NULL DEFAULT 0",
        "durable_memory_id": "ALTER TABLE approval_queue ADD COLUMN durable_memory_id TEXT",
        "supersede_target_id": "ALTER TABLE approval_queue ADD COLUMN supersede_target_id TEXT",
        "supersede_done_at": "ALTER TABLE approval_queue ADD COLUMN supersede_done_at TEXT",
        "finalizer_last_error": "ALTER TABLE approval_queue ADD COLUMN finalizer_last_error TEXT",
        # Audit-invariant PR1 — the approval RECEIPT, stored atomically with the approved-status
        # flip (same _approve_in_conn txn): the hash-chained audit row (seq + this_hash) recording
        # the authorized decision, plus the sha256 fingerprint of the exact candidate bytes that
        # decision approved. The finalizer refuses durability unless these verify (fail-closed).
        "audit_receipt_seq": "ALTER TABLE approval_queue ADD COLUMN audit_receipt_seq INTEGER",
        "audit_receipt_hash": "ALTER TABLE approval_queue ADD COLUMN audit_receipt_hash TEXT",
        "candidate_sha256": "ALTER TABLE approval_queue ADD COLUMN candidate_sha256 TEXT",
        # PR2 proposal-source — written ONLY by MemoryClient._enqueue_approval (the server enqueue
        # path), never derived from candidate_json: a hand-inserted queue row has it NULL and is
        # honestly unattributed. Attribution flows column → pre-decision receipt (hash-chained) →
        # durable metadata (the finalizer reads the GATE-VERIFIED receipt, not this column).
        "proposed_by_profile": "ALTER TABLE approval_queue ADD COLUMN proposed_by_profile TEXT",
    }
    for column, sql in approval_migrations.items():
        if column not in approval_columns:
            conn.execute(sql)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_retry ON memory_events(status, next_attempt_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_shadow ON memory_events(shadow)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_approval_tg_last_sent ON approval_queue(status, tg_last_sent_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_poll_state (
          bot_name TEXT PRIMARY KEY,
          last_update_id INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE VIEW IF NOT EXISTS outbox AS SELECT * FROM memory_events")


def build_cron_memory_content(
    cron_name: str,
    status: str,
    duration_s: float,
    run_id: str,
    artifacts: Any,
    error: str | None = None,
) -> str:
    """Build stable cron memory text for default/cron."""
    payload: dict[str, Any] = {
        "cron": cron_name,
        "status": status,
        "duration_s": round(duration_s, 2),
        "run_id": run_id,
        "artifacts": artifacts or {},
    }
    if error:
        payload["error"] = error
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _approve_in_conn(
    conn: sqlite3.Connection,
    approval_id: int,
    *,
    decided_by: str,
    now: str,
    approved_via: str | None = None,
    approved_by: str | None = None,
    approved_tg_message_id: int | None = None,
    audit_receipt_seq: int | None = None,
    audit_receipt_hash: str | None = None,
    candidate_sha256: str | None = None,
) -> int:
    """Core approve on an ALREADY-LOCKED connection (caller holds BEGIN IMMEDIATE): idempotent
    check, emit the pending memory_event, mark approved, and — when ``approved_via`` is given —
    stamp the handler-authorized provenance in the SAME transaction. When an audit RECEIPT
    (``audit_receipt_seq``/``audit_receipt_hash``/``candidate_sha256``) is given, it is stored in
    that same transaction — atomically with the approved-status flip — after RE-CHECKING the
    fingerprint against the row's CURRENT ``candidate_json`` (the receipt attests exact bytes; a
    row whose bytes changed since the caller hashed them must abort, not stamp). Returns the
    events id."""
    row = conn.execute("SELECT * FROM approval_queue WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ValueError(f"approval_queue row not found: {approval_id}")
    if candidate_sha256 is not None and candidate_fingerprint(row["candidate_json"]) != candidate_sha256:
        # In-txn fingerprint recheck (v4 FIX-2): the receipt was appended for specific candidate
        # bytes; if the stored bytes no longer match, stamping would bind the receipt to content
        # it never attested. Raising rolls back the whole transaction (no status flip, no stamp).
        raise ValueError(
            f"approval {approval_id} candidate bytes changed since the audit receipt was appended; refusing stamp"
        )
    if row["status"] == "approved" and row["memory_event_id"]:
        # Idempotent re-approve. Stamp provenance only if this is an authorized call and the row
        # is not yet provenanced (e.g. a raw approve preceded it); never re-insert the event.
        if approved_via is not None and row["approved_via"] is None:
            conn.execute(
                "UPDATE approval_queue SET approved_via=?, approved_by=?, "
                "approved_tg_message_id=COALESCE(?, approved_tg_message_id) WHERE id=?",
                (approved_via, approved_by, approved_tg_message_id, approval_id),
            )
        if audit_receipt_seq is not None and row["audit_receipt_seq"] is None:
            # Receipt-if-missing (never overwrite): an approved-but-unreceipted row (legacy, or a
            # raw approve later re-approved through an authorized surface) gains the receipt that
            # will let the finalizer's gate verify it. An already-bound receipt is immutable.
            conn.execute(
                "UPDATE approval_queue SET audit_receipt_seq=?, audit_receipt_hash=?, candidate_sha256=? "
                "WHERE id=? AND audit_receipt_seq IS NULL",
                (audit_receipt_seq, audit_receipt_hash, candidate_sha256, approval_id),
            )
        return int(row["memory_event_id"])
    if row["status"] not in {"pending", "pending_approval", "deferred"}:
        raise ValueError(f"approval_queue row {approval_id} is not approvable: {row['status']}")
    candidate = json.loads(row["candidate_json"])
    payload = build_approval_memory_payload(candidate, approval_id=approval_id, created_at=now)
    # event_type is the owner-templated feedback stream name (owner overlay → 'owner_feedback',
    # byte-identical to the historical literal) — bound as a parameter, not an embedded literal.
    event_type = f"{current_governance().identity.owner_slug}_feedback"
    cur = conn.execute(
        """
        INSERT INTO memory_events (
            event_type, payload_json, created_at, status, attempt_count,
            next_attempt_at, shadow, tenant_id, principal_id
        )
        VALUES (?, ?, ?, 'pending', 0, ?, 0, ?, ?)
        """,
        (
            event_type,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
            now,
            str(payload.get("metadata", {}).get("tenant_id") or default_tenant_id()),
            str(payload.get("metadata", {}).get("principal_id") or ""),
        ),
    )
    event_id = int(cur.lastrowid)
    # DIRECT assignment (NOT COALESCE): a raw approve passes approved_*=None and must CLEAR any
    # pre-existing provenance so a forged/hand-edited approved_via='telegram' on a pending row can
    # never be carried into an approved row and arm finalization (Codex diff R5 B1). The authorized
    # path passes the real verified values, which overwrite anything pre-filled. The receipt triple
    # follows the same rule: a raw approve (no receipt) clears any hand-planted receipt columns, so
    # an unreceipted approval can never smuggle a forged receipt past the finalizer's gate.
    conn.execute(
        """
        UPDATE approval_queue
           SET status = 'approved',
               responded_at = ?,
               decided_by = ?,
               memory_event_id = ?,
               last_error = NULL,
               approved_via = ?,
               approved_by = ?,
               approved_tg_message_id = ?,
               audit_receipt_seq = ?,
               audit_receipt_hash = ?,
               candidate_sha256 = ?
         WHERE id = ?
        """,
        (
            now,
            decided_by,
            event_id,
            approved_via,
            approved_by,
            approved_tg_message_id,
            audit_receipt_seq,
            audit_receipt_hash,
            candidate_sha256,
            approval_id,
        ),
    )
    return event_id


def approve_queued_candidate(db_path: str | Path, approval_id: int, *, decided_by: str | None = None) -> int:
    """Approve one queue row + emit a pending memory_events row. RAW primitive — it does NOT
    stamp finalizer-eligible provenance (``approved_via``/``approved_by`` stay NULL), so a row
    approved this way is NOT durably finalizable. Only ``approve_authorized_telegram_candidate``
    can arm a durable write (Stage 2 P0 fail-closed provenance). ``decided_by`` defaults to the
    CONFIGURED approval transport's name (de-Ownered default — no hardcoded channel)."""
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_outbox_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        event_id = _approve_in_conn(
            conn, approval_id, decided_by=decided_by or _transport_kind(), now=_now_iso()
        )
        conn.commit()
        return event_id


def _sent_message_ids(ids_json: Any, latest: Any) -> set[int]:
    """The set of message ids the bot actually sent for an approval. Parses the accumulated JSON
    array (``tg_message_ids``); falls back to ``[tg_message_id]`` for legacy rows written before
    accumulation; an empty set means the row was never sent (unbound)."""
    out: set[int] = set()
    if ids_json:
        try:
            out = {int(m) for m in json.loads(ids_json) if m is not None}
        except (ValueError, TypeError):
            out = set()
    if not out and latest is not None:
        out = {int(latest)}
    return out


def approve_authorized_telegram_candidate(
    db_path: str | Path,
    approval_id: int,
    *,
    presser_id: str,
    tg_message_id: int | None = None,
    audit_receipt_seq: int | None = None,
    audit_receipt_hash: str | None = None,
    candidate_sha256: str | None = None,
) -> int:
    """Stage 2 P0 — the ONLY path that stamps finalizer-eligible approval provenance.

    Called by the authenticated Telegram approval handler AFTER ``_is_authorized_presser`` has
    verified ``callback.from.id`` == the configured author. Stamps ``approved_via='telegram'`` +
    ``approved_by='telegram:{presser_id}'`` from the VERIFIED presser — NOT a caller-controlled
    ``decided_by`` string. The durable finalizer (``approval_finalizer.py``) trusts ONLY rows
    carrying this provenance, so a raw ``approve_queued_candidate`` or a hand-edited
    ``status='approved'`` row is fail-closed (never finalized). Honest boundary: a single-process
    daemon cannot make this cryptographically unforgeable; this raises forgery to the daemon's
    existing trust boundary (signed attestation / process isolation = a P1+ follow-up).

    Binding check + approve-mark + provenance-stamp run in ONE ``BEGIN IMMEDIATE`` transaction.
    The approval MUST bind to the real bot-sent button message: the row's stored ``tg_message_id``
    (set by ``mark_approval_sent`` when the bot sent the message) AND the callback's message id
    must BOTH be present and equal. A real approval callback can only exist for a message the bot
    sent, so an unbound row (``tg_message_id IS NULL``) is never a legitimate authorized approval
    (Codex diff B1). Returns the ``memory_events`` id.

    AUDIT-INVARIANT boundary (PR1): the receipt triple is OPTIONAL here — it is the adoption seam
    for the OUT-OF-REPO telegram handler. A telegram deployment whose handler does not yet append
    a pre-decision audit record and pass the receipt stamps an approval WITHOUT a receipt, and the
    finalizer's receipt gate then refuses to make it durable (fail-closed). To keep approvals
    finalizable, the external handler must append ``op="approval_decision"`` to the box's audit DB
    (with the candidate fingerprint) and pass the returned (seq, hash, sha256) here. When provided,
    the triple is validated and stored atomically with the approved stamp.
    """
    db_path = Path(db_path)
    presser = str(presser_id or "")
    if not presser:
        raise ValueError("presser_id is required for an authorized approval")
    if audit_receipt_seq is not None or audit_receipt_hash is not None or candidate_sha256 is not None:
        audit_receipt_seq, audit_receipt_hash, candidate_sha256 = _require_receipt(
            audit_receipt_seq, audit_receipt_hash, candidate_sha256
        )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_outbox_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        bound = conn.execute(
            "SELECT tg_message_id, tg_message_ids FROM approval_queue WHERE id = ?", (approval_id,)
        ).fetchone()
        if bound is None:
            raise ValueError(f"approval_queue row not found: {approval_id}")
        # STRICT binding, resend-aware: the callback message id must be a NON-null id the bot
        # actually sent for this approval (initial OR any resend). An unbound row (never sent →
        # empty set) has no real button to press, so it can never be a legitimate authorized
        # approval; a None callback or a non-sent id is rejected (Codex diff R4 + GH P2 resend).
        sent_ids = _sent_message_ids(bound["tg_message_ids"], bound["tg_message_id"])
        if not sent_ids or tg_message_id is None or int(tg_message_id) not in sent_ids:
            raise ValueError(
                f"approval {approval_id} requires a bound bot message; "
                f"callback msg={tg_message_id} not in sent set {sorted(sent_ids)}"
            )
        event_id = _approve_in_conn(
            conn,
            approval_id,
            decided_by=f"telegram:{presser}",
            now=_now_iso(),
            approved_via="telegram",
            approved_by=f"telegram:{presser}",
            approved_tg_message_id=tg_message_id,
            audit_receipt_seq=audit_receipt_seq,
            audit_receipt_hash=audit_receipt_hash,
            candidate_sha256=candidate_sha256,
        )
        conn.commit()
        return event_id


# The approval-queue statuses the MCP surface treats as "awaiting an operator decision": a fresh
# candidate ('pending_approval') and a deferred candidate whose defer window elapsed and was
# resurfaced to 'pending' (resurface_deferred_approvals). 'deferred' is excluded (not yet due;
# resurfaces to 'pending' first), as are terminal 'approved'/'rejected'/finalized (keeps the
# raw-approve→restamp hazard closed). Used by both approve_authorized_candidate + list_pending_approvals.
_MCP_APPROVABLE_STATUSES: frozenset[str] = frozenset({"pending", "pending_approval"})


# Finalizer states from which a row is safe to (re)act on — same set the reject/defer SQL guards
# use. 'finalizing'/'rtbf_deleting' are LIVE claims (a concurrent finalize or hard-delete/RTBF).
_UNCLAIMED_FINALIZER_STATES: frozenset[str] = frozenset({"none", "failed"})


def _finalizer_unclaimed(finalizer_state: Any) -> bool:
    return finalizer_state is None or finalizer_state in _UNCLAIMED_FINALIZER_STATES


def mcp_approve_or_redrive(
    db_path: str | Path,
    approval_id: int,
    *,
    approved_via: str,
    approved_by: str,
    audit_receipt_seq: int,
    audit_receipt_hash: str,
    candidate_sha256: str,
) -> str:
    """Slice-S P3b — the MCP ``memory.approve_candidate`` decision under one ``BEGIN IMMEDIATE``:
    stamp a fresh awaiting candidate, OR authorize a REDRIVE of a stuck one. Returns ``"stamped"``
    (a fresh candidate was just approved) or ``"redrive"`` (already approved by this transport, only
    finalize needs re-running). The caller then runs ``finalize_approved_candidate``.

    AUDIT-INVARIANT (PR1): the receipt triple is REQUIRED — the caller (the shared
    ``approval_service.approve``) must have appended the pre-decision audit record FIRST and pass
    its (seq, this_hash) plus the candidate fingerprint it hashed. A FRESH stamp stores the triple
    atomically with the approved-status flip (after an in-transaction fingerprint recheck); a
    REDRIVE keeps the row's ORIGINAL receipt (the one bound to the actual status flip) and ignores
    this call's triple — the fresh audit append still stands in the chain as the retry's intent
    record. There is deliberately NO receipt-less variant of this function: a finalizable stamp
    without a receipt would be refused by the finalizer gate forever.

    The caller MUST have verified the presser through the configured transport (``verify_presser`` →
    canonical ``approved_by``; ``stamp_provenance`` → ``approved_via``) AND that ``approved_by`` is an
    authorized approver. Cases:

    - **Fresh** — status ``pending``/``pending_approval`` (awaiting) and NOT finalizer-claimed → stamp
      ``approved_via``/``approved_by`` in this transaction (the finalizer trusts only this provenance;
      a raw ``approve_queued_candidate`` row has ``approved_via`` NULL → never redrivable/finalizable).
    - **Redrive** — already ``approved`` by THIS transport (matching ``approved_via``+``approved_by``),
      finalize NOT complete, and NOT finalizer-claimed → return ``"redrive"`` WITHOUT re-stamping.
      This is the ONLY recovery for a self-host box where an earlier finalize failed transiently
      (no background redrive service): the operator calls ``approve_candidate`` again to re-finalize.
    - Anything else (rejected/deferred/finalized, a foreign-transport stamp, or a live
      finalizing/rtbf claim) → ``ValueError`` (the MCP tool maps it to a refusal).
    """
    db_path = Path(db_path)
    via = str(approved_via or "")
    by = str(approved_by or "")
    if not via or not by:
        raise ValueError("mcp_approve_or_redrive requires non-empty approved_via/approved_by")
    audit_receipt_seq, audit_receipt_hash, candidate_sha256 = _require_receipt(
        audit_receipt_seq, audit_receipt_hash, candidate_sha256
    )
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_outbox_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, approved_via, approved_by, finalizer_state, durable_memory_id "
            "FROM approval_queue WHERE id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"approval_queue row not found: {approval_id}")
        status = row["status"]
        # REDRIVE: an already-approved row whose finalize didn't complete, stamped by THIS transport,
        # not currently claimed. No re-stamp — finalize is re-run by the caller.
        if (
            status == "approved"
            and row["approved_via"] == via
            and row["approved_by"] == by
            and _finalizer_unclaimed(row["finalizer_state"])
        ):
            conn.commit()
            return "redrive"
        # FRESH: only the awaiting states, never a terminal/foreign/raw row (keeps the
        # raw-approve→restamp hazard closed — a raw 'approved' row has approved_via NULL, so it fails
        # BOTH the redrive branch above and this one).
        if status not in _MCP_APPROVABLE_STATUSES:
            raise ValueError(
                f"approval {approval_id} is not an awaiting or retriable candidate (status={status!r})"
            )
        # A live finalizer/RTBF claim (finalizing/rtbf_deleting) — refuse (see the reject/defer guard).
        if not _finalizer_unclaimed(row["finalizer_state"]):
            raise ValueError(
                f"approval {approval_id} is claimed by the finalizer/RTBF "
                f"(finalizer_state={row['finalizer_state']!r})"
            )
        _approve_in_conn(
            conn,
            approval_id,
            decided_by=by,
            now=_now_iso(),
            approved_via=via,
            approved_by=by,
            audit_receipt_seq=audit_receipt_seq,
            audit_receipt_hash=audit_receipt_hash,
            candidate_sha256=candidate_sha256,
        )
        conn.commit()
        return "stamped"


def get_approval_candidate(db_path: str | Path, approval_id: int) -> sqlite3.Row | None:
    """One approval row's decision inputs (or None when the row is missing): the STORED
    ``candidate_json`` text — the shared approval service hashes this exact string
    (``candidate_fingerprint``) into the pre-decision audit receipt BEFORE any queue mutation, and
    ``_approve_in_conn`` re-checks it inside the stamp transaction — plus the server-stamped
    ``proposed_by_profile`` column the service projects into that same receipt."""
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_outbox_schema(conn)
        return conn.execute(
            "SELECT candidate_json, proposed_by_profile FROM approval_queue WHERE id = ?",
            (approval_id,),
        ).fetchone()


# --- Audit-receipt migration (PR1 activation) ------------------------------------------------
#
# A populated pre-invariant box has approved-awaiting-finalize rows with NO receipt; the finalizer
# gate would refuse them forever (NEVER grandfather). The migrator attests each one with an
# `approval_decision_backfill` audit append, then CAS-stamps the receipt. The serving entrypoint
# (http/app.py lifespan) runs this at startup and REFUSES to serve until zero eligible rows remain.

# Eligible-NULL = rows the finalizer could act on that lack a receipt. Excludes 'finalized'
# (idempotent early-return, never re-written) and 'rtbf_deleting' (a live/crashed forget claim —
# those rows only ever proceed to deletion, never back to finalizable).
_ELIGIBLE_NULL_WHERE = (
    "status='approved' AND memory_event_id IS NOT NULL "
    "AND approved_via IS NOT NULL AND approved_by IS NOT NULL "
    "AND (finalizer_state IS NULL OR finalizer_state IN ('none','failed','finalizing')) "
    "AND audit_receipt_seq IS NULL"
)


@dataclass(frozen=True)
class AuditReceiptMigration:
    """Result of one ``migrate_audit_receipts`` pass. ``remaining`` > 0 means activation must be
    REFUSED (rows the gate would strand are still unreceipted — e.g. a finalizing-row inspection
    could not consult the store)."""

    eligible: int
    backfilled: int
    remaining: int


def _inspect_finalizing_marker(adapter: Any, approval_id: int, tenant_accessed: str) -> str:
    """Migration-time inspection of a ``finalizer_state='finalizing'`` row: the crashed worker may
    already have written the durable row (crash AFTER adapter write, BEFORE record). Look up the
    unique ``approval_ref:{id}`` marker so the backfill receipt honestly records whether the
    durable write PRECEDED it. Returns ``marker_found`` / ``marker_absent`` /
    ``lookup_unavailable`` (no adapter wired) / ``lookup_failed`` (store error)."""
    if adapter is None:
        return "lookup_unavailable"
    finder = getattr(adapter, "find_active_hash_by_marker", None)
    if finder is None:
        return "lookup_unavailable"
    try:
        found = finder(f"approval_ref:{approval_id}", tenant_accessed)
    except Exception as exc:  # noqa: BLE001 — inspection must never crash the migrator
        logger.warning("audit-receipt migration: marker lookup failed for approval %s: %s", approval_id, exc)
        return "lookup_failed"
    return "marker_found" if found else "marker_absent"


def migrate_audit_receipts(
    outbox_db_path: str | Path,
    audit_db_path: str | Path,
    *,
    principal: Principal,
    tenant_accessed: str,
    adapter: Any | None = None,
) -> AuditReceiptMigration:
    """Backfill audit receipts onto pre-invariant approved-awaiting-finalize rows (restart-safe,
    config-aware, NEVER grandfather).

    Protocol per row: append ``op='approval_decision_backfill'`` to the box's hash-chained audit
    log (decision + row provenance + the candidate fingerprint, plus the finalizing-inspection
    outcome where applicable) → CAS-stamp the receipt onto the row (``WHERE audit_receipt_seq IS
    NULL``). A crash between append and stamp re-runs cleanly on the next pass: the orphan append
    stays in the chain as an intent record and a fresh append gets stamped. Rows whose
    ``finalizer_state='finalizing'`` inspection cannot consult the store (``lookup_unavailable`` /
    ``lookup_failed``) are SKIPPED — they stay eligible-NULL, ``remaining`` stays non-zero, and the
    caller must REFUSE activation until a pass with a reachable store decides them (fail-closed:
    never attest blind whether a durable write preceded the receipt).

    The caller (the serving entrypoint's startup hook) treats ``remaining > 0`` — or any raised
    error — as "do not serve". Fresh installs and boxes with an empty queue return (0, 0, 0)."""
    outbox_db_path = Path(outbox_db_path)
    audit_db_path = Path(audit_db_path)
    with sqlite3.connect(outbox_db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_outbox_schema(conn)
        candidates = [
            int(r["id"])
            for r in conn.execute(
                f"SELECT id FROM approval_queue WHERE {_ELIGIBLE_NULL_WHERE} ORDER BY id"
            ).fetchall()
        ]
    eligible = len(candidates)
    backfilled = 0
    for approval_id in candidates:
        if _backfill_one(
            outbox_db_path, audit_db_path, approval_id,
            principal=principal, tenant_accessed=tenant_accessed, adapter=adapter,
        ):
            backfilled += 1
    with sqlite3.connect(outbox_db_path) as conn:
        remaining = int(
            conn.execute(f"SELECT COUNT(*) FROM approval_queue WHERE {_ELIGIBLE_NULL_WHERE}").fetchone()[0]
        )
    if remaining:
        logger.warning(
            "audit-receipt migration incomplete: %d approved rows still lack receipts", remaining
        )
    return AuditReceiptMigration(eligible=eligible, backfilled=backfilled, remaining=remaining)


def _backfill_one(
    outbox_db_path: Path,
    audit_db_path: Path,
    approval_id: int,
    *,
    principal: Principal,
    tenant_accessed: str,
    adapter: Any | None,
) -> bool:
    """Backfill ONE row — re-validate, inspect, append, and stamp under a SINGLE ``BEGIN
    IMMEDIATE`` on the outbox. The held write lock is what serializes the migrator against a
    still-running LEGACY finalizer (an old-code process upgraded out from under): that finalizer's
    claim CAS needs this same lock, so either it claimed FIRST (the re-read sees ``finalizing`` and
    the marker inspection records whether its write already landed) or the stamp lands FIRST (the
    receipt is committed before any later claim/write — the invariant's normal order). Without the
    single transaction, a claim+write could interleave between the eligibility snapshot and the
    stamp, attaching a receipt AFTER an uninspected durable write (Codex terra R1 P1).

    Lock order is outbox → audit, the same order the finalizer's receipt gate uses (never the
    reverse anywhere), so the cross-DB hold cannot deadlock. The audit append itself is a separate
    committed transaction: a crash after it but before the stamp leaves an orphan intent record and
    the next pass re-appends cleanly (restart-safe).

    Returns True iff the receipt was stamped. Rows that stopped being eligible (finalized /
    rtbf-claimed / already receipted) and finalizing rows whose store inspection is unavailable are
    left untouched — the caller's remaining-count re-query decides whether activation may proceed."""
    conn = sqlite3.connect(outbox_db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_outbox_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, candidate_json, approved_via, approved_by, finalizer_state, proposed_by_profile "
            f"FROM approval_queue WHERE id = ? AND {_ELIGIBLE_NULL_WHERE}",
            (approval_id,),
        ).fetchone()
        if row is None:
            # The row changed state between the eligibility snapshot and now (finalized, claimed,
            # purged, or receipted by a concurrent pass) — never stamp on a stale premise.
            conn.commit()
            return False
        details: dict[str, Any] = {
            "phase": "backfill",
            "decision": "approve",
            "approved_by": row["approved_by"],
            "approved_via": row["approved_via"],
            "approval_id": approval_id,
            "candidate_sha256": candidate_fingerprint(row["candidate_json"]),
        }
        # Project the server-stamped proposer COLUMN into the backfill receipt too (PR2): rows the
        # enqueue path stamped keep their attribution through backfill; legacy/hand-inserted rows
        # (column NULL) stay honestly unattributed — receipt and eventual durable row agree.
        proposer = proposed_by_profile(row["proposed_by_profile"])
        if proposer is not None:
            details["proposed_by_profile"] = proposer
        if row["finalizer_state"] == "finalizing":
            inspection = _inspect_finalizing_marker(adapter, approval_id, tenant_accessed)
            if inspection in ("lookup_unavailable", "lookup_failed"):
                logger.warning(
                    "audit-receipt migration: cannot inspect finalizing approval %s (%s); "
                    "leaving unreceipted (activation must refuse)", approval_id, inspection,
                )
                conn.commit()
                return False
            details["finalizing_inspection"] = inspection
        seq = audit_write(
            principal=principal,
            tenant_accessed=tenant_accessed,
            op="approval_decision_backfill",
            target_id=str(approval_id),
            details=details,
            db_path=audit_db_path,
        )
        this_hash = audit_row_hash(seq, db_path=audit_db_path)
        conn.execute(
            "UPDATE approval_queue SET audit_receipt_seq=?, audit_receipt_hash=?, candidate_sha256=? "
            "WHERE id=? AND audit_receipt_seq IS NULL",
            (seq, this_hash, details["candidate_sha256"], approval_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_pending_approvals(
    db_path: str | Path, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Slice-S P3b — a bounded read of the pending approval queue for the MCP
    ``memory.list_pending_approvals`` operator tool. Returns at most ``limit`` rows
    (id/wing/room/memory_type/created_at + a short content preview) oldest-first. Listing leaks
    candidate content, so the tool is authorized-approver-gated at the MCP layer.

    SINGLE-TENANT assumption: this returns ALL pending rows in the outbox with no tenant/wing
    filter. That is safe for the ONLY surface that calls it today — the MCP approval tools register
    exclusively for ``local_cli`` (single-owner-box) transports. A multi-tenant deployment that ever
    exposes the /mcp approval surface MUST add a tenant filter here first, else an operator would see
    cross-tenant candidate previews."""
    db_path = Path(db_path)
    capped = max(1, min(int(limit), 100))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_outbox_schema(conn)
        # wing/room/memory_type/content are NOT columns on approval_queue — they live inside
        # candidate_json (see MemoryClient._enqueue_approval: text/suggested_wing/suggested_room).
        # List BOTH awaiting-decision states (pending_approval + resurfaced-to-pending) so a deferred
        # candidate that came due is visible/approvable via MCP (must match the approve guard).
        rows = conn.execute(
            """
            SELECT id, candidate_json, created_at, proposed_by_profile
              FROM approval_queue
             WHERE status IN ('pending', 'pending_approval')
             ORDER BY id ASC
             LIMIT ?
            """,
            (capped,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            candidate = json.loads(row["candidate_json"])
            if not isinstance(candidate, dict):
                candidate = {}
        except (ValueError, TypeError):
            candidate = {}
        content = str(candidate.get("text") or "")
        out.append(
            {
                "approval_id": int(row["id"]),
                "wing": candidate.get("suggested_wing"),
                "room": candidate.get("suggested_room"),
                "memory_type": candidate.get("memory_type"),
                "created_at": row["created_at"],
                "content_preview": content[:200],
                # The approver sees WHO is asking — the server-stamped column (None for
                # legacy/hand-inserted/malformed rows; PR2 proposal-source).
                "proposed_by": proposed_by_profile(row["proposed_by_profile"]),
            }
        )
    return out


def count_pending_approvals(db_path: str | Path) -> int:
    """Count awaiting-decision candidates (``pending`` + ``pending_approval``) — the bare integer
    behind the console badge (WP-B2a ``GET /v1/approvals/count``). Zero ids, zero content. Shares
    the pending-status set with ``list_pending_approvals`` so the badge count and the list can never
    disagree on what "pending" means."""
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as conn:
        _ensure_outbox_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*) FROM approval_queue WHERE status IN ('pending', 'pending_approval')"
        ).fetchone()
    return int(row[0]) if row else 0


def update_approval_status(
    db_path: str | Path,
    approval_id: int,
    status: str,
    *,
    decided_by: str | None = None,
) -> int:
    """Set a NON-terminal approval row to ``status`` (rejected/pending). Returns the number of rows
    changed: 0 means the row was missing or already terminal/finalized (so a caller reporting
    success must check this — a stale reject must not read as a fresh rejection)."""
    if status not in {"rejected", "pending"}:
        raise ValueError(f"unsupported approval status update: {status}")
    decided_by = decided_by or _transport_kind()
    now = _now_iso()
    with sqlite3.connect(db_path) as conn:
        _ensure_outbox_schema(conn)
        # Only a NON-terminal, non-finalized row can be rejected/reset — a stale reject callback
        # must never flip an already-approved/finalized row (Codex diff R6 minor).
        cur = conn.execute(
            """
            UPDATE approval_queue
               SET status = ?,
                   responded_at = CASE WHEN ? = 'rejected' THEN ? ELSE responded_at END,
                   decided_by = ?,
                   last_error = NULL
             WHERE id = ?
               AND status IN ('pending', 'pending_approval', 'deferred')
               AND (finalizer_state IS NULL OR finalizer_state IN ('none', 'failed'))
            """,
            (status, status, now, decided_by, approval_id),
        )
        if cur.rowcount == 0:
            logger.info("approval_queue %s not set to %s — terminal/finalized state", approval_id, status)
        return int(cur.rowcount)


def defer_queued_candidate(
    db_path: str | Path,
    approval_id: int,
    *,
    defer_hours: int = 24,
    decided_by: str | None = None,
    now: datetime | None = None,
) -> None:
    decided_by = decided_by or _transport_kind()
    now_dt = now or datetime.now(UTC)
    until = now_dt + timedelta(hours=defer_hours)
    with sqlite3.connect(db_path) as conn:
        _ensure_outbox_schema(conn)
        # Same non-terminal guard as reject — a stale defer must not touch an approved/finalized row.
        cur = conn.execute(
            """
            UPDATE approval_queue
               SET status = 'deferred',
                   responded_at = ?,
                   expires_at = ?,
                   decided_by = ?,
                   last_error = NULL
             WHERE id = ?
               AND status IN ('pending', 'pending_approval', 'deferred')
               AND (finalizer_state IS NULL OR finalizer_state IN ('none', 'failed'))
            """,
            (now_dt.isoformat(), until.isoformat(), decided_by, approval_id),
        )
        if cur.rowcount == 0:
            logger.info("approval_queue %s not deferred — terminal/finalized state", approval_id)


def resurface_deferred_approvals(db_path: str | Path, *, now: datetime | None = None) -> int:
    now_text = (now or datetime.now(UTC)).isoformat()
    with sqlite3.connect(db_path) as conn:
        _ensure_outbox_schema(conn)
        cur = conn.execute(
            """
            UPDATE approval_queue
               SET status = 'pending',
                   expires_at = NULL,
                   tg_message_id = NULL,
                   tg_last_sent_at = NULL
             WHERE status = 'deferred'
               AND expires_at IS NOT NULL
               AND expires_at <= ?
            """,
            (now_text,),
        )
        return cur.rowcount


def build_approval_memory_payload(
    candidate: dict[str, Any],
    *,
    approval_id: int,
    created_at: str | None = None,
    governance: Governance | None = None,
) -> dict[str, Any]:
    """Build the canonical durable payload for an approved candidate.

    Owner identity (wing, content prefix, memory_type/event_type/source_system prefix, tenant /
    subject / principal) is TEMPLATED from governance (§1.6 durable-identity templating) — NEW
    writes only; existing rows are immutable. A deployment overlay that reproduces the exact
    historical byte strings (e.g. owner's ``"Owner"``/``"owner"``) yields byte-identical payloads
    (same content_sha256 / room / deterministic id), so the corpus never forks. The structural
    room vocabulary (``feedback``/``preferences``) and the ``_feedback``/``_preference`` suffixes
    are FIXED product structure, not identity.
    """
    gov = governance or current_governance()
    identity = gov.identity
    created_at = created_at or _now_iso()
    cand_meta = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    text = str(candidate.get("text") or "").strip()
    owner_wing = identity.owner_wing
    wing = str(candidate.get("suggested_wing") or owner_wing)
    room = str(candidate.get("suggested_room") or "feedback")
    if wing != owner_wing:
        wing = owner_wing
    if room not in {"feedback", "preferences"}:
        room = "feedback"
    content = _approval_memory_content(candidate, room=room, governance=gov)
    memory_id = _deterministic_memory_id(wing, room, content)
    tags = candidate.get("tags") if isinstance(candidate.get("tags"), list) else []
    metadata = {
        "schema_version": "v2",
        "tenant_id": identity.default_tenant_id,
        "subject_id": identity.default_subject_id,
        "actor_id": identity.root_principal.id,
        "principal_id": identity.root_principal.id,
        "migration_batch_id": None,
        "memory_type": f"{identity.owner_slug}_feedback" if room == "feedback" else f"{identity.owner_slug}_preference",
        "source_system": f"{identity.owner_slug}-manual",
        "source_id": f"approval_queue:{approval_id}",
        "wing": wing,
        "room": room,
        "created_at": created_at,
        "event_time": created_at,
        "privacy_scope": "agent-wide",
        "readable_by": [],
        "provenance": {
            "approval_queue_id": approval_id,
            "capture_source": candidate.get("source", "unknown"),
            "explicit_trigger": "explicit-trigger" in tags,
        },
        "links": [],
        "memory_id": memory_id,
        "source_file": f"approval_queue:{approval_id}:{_content_hash(text)}",
        "chunk_index": 0,
        # The approval channel's writer label, derived from the CONFIGURED transport (owner
        # overlay kind=telegram → 'telegram-approval-bot', byte-identical to history).
        "added_by": f"{gov.approval.transport_kind}-approval-bot",
        "filed_at": created_at,
        "content_sha256": _content_hash(content),
        "tags": tags,
        # Phase 1 (Codex bot P2 on #197) — keep the additive-metadata contract
        # consistent on the approval-write path: preserve the proposer's
        # provenance from the queued candidate, and stamp bitemporal at approval
        # time (the memory becomes valid when Owner approves). valid_from /
        # ingested_at reuse this payload's created_at for intra-payload uniformity.
        "author_agent_id": cand_meta.get("author_agent_id"),
        "source_bridge": cand_meta.get("source_bridge"),
        "session_id": cand_meta.get("session_id"),
        # Preserve infer only when it is a real bool (mirrors MemoryClient.write's
        # strict isinstance check, which this approval path bypasses): a legacy/
        # manual {"infer": "false"} must NOT coerce to True (Codex bot P3 on #197).
        "infer": cand_meta.get("infer") if isinstance(cand_meta.get("infer"), bool) else False,
        "valid_from": created_at,
        "valid_to": None,
        "ingested_at": created_at,
    }
    return {
        "wing": wing,
        "room": room,
        "content": content,
        "metadata": metadata,
        "aaak": {
            "summary": text[:240],
            "tags": tags,
            "approval_queue_id": approval_id,
        },
        "source": f"{gov.approval.transport_kind}-approval-bot",
        "expected_id": memory_id,
        "shadow": False,
    }


def _approval_memory_content(
    candidate: dict[str, Any], *, room: str, governance: Governance | None = None
) -> str:
    """The durable content line. The owner prefix comes from governance
    ``identity.content_owner_label`` (owner overlay → ``"Owner"``, byte-identical); the
    ``{label}:`` structure is fixed."""
    gov = governance or current_governance()
    label = "Preference" if room == "preferences" else "Feedback"
    text = str(candidate.get("text") or "").strip()
    return f"{gov.identity.content_owner_label} {label.lower()}: {text}"
