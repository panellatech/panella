"""Phase 1.5 (item E) — Panella-owned append-only audit log of memory state transitions.

Append-only by construction: the only operation is INSERT — no UPDATE, no DELETE.
Lives in the same SQLite DB as approval_queue / memory_events. The caller passes its own
``self.outbox_db_path`` so the audit row always co-locates with the queue/event row it
audits (the module-global ``OUTBOX_DB_PATH`` is only a parameterless fallback).
BEST-EFFORT: any exception is logged at WARNING and swallowed; it NEVER fails or alters the
underlying memory operation (which has already committed by the time this is called).
No GC, no sync to Panella store. Phase 2 G1-C adds ONE narrow read-only helper
(``forgotten_events``) so the reconciler stays idempotent — still append-only
(SELECT only; no UPDATE/DELETE).

``op`` is a SUPERSET of ``WriteResult.op``. The 6 write-outcome values:
    stored | dedup_skipped | queued_for_approval | supersede | tombstone | hard_delete
plus the Phase 2 G1-C reconciliation values (emitted by the read-only reconciler,
never by a WriteResult): forgotten | archived.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from panella.client_raw import OUTBOX_DB_PATH, _ensure_outbox_schema
from panella.panella_adapter import _utcnow_iso

logger = logging.getLogger(__name__)

# The canonical history ops. Pinned by test so a future WriteResult.op edit cannot
# silently desync the reconciliation key. memory_history.op is a SUPERSET of
# WriteResult.op (which is only the 3 write outcomes). The first 6 are write
# outcomes; the last 2 are Phase 2 G1-C reconciliation ops emitted by the
# read-only reconciler, never by a WriteResult:
#   forgotten — a row upstream forgetting soft-deleted (deleted_at set) while Panella
#               still tags it status:active (the 309-memory never-lose violation).
#   archived  — reserved for the symmetric status divergence (a row upstream
#               archived/superseded out from under an active Panella tag).
HISTORY_OPS = (
    "stored",
    "dedup_skipped",
    "queued_for_approval",
    "supersede",
    "tombstone",
    "hard_delete",
    "forgotten",
    "archived",
    # Stage 2 P0 — the finalizer's durable write of an approved candidate. Distinct
    # provenance from "stored" (a normal gated/direct write): this row is the durable
    # landing of a human-approved inferred fact about Owner.
    "finalized_active",
    # Stage 2 P0 fast-follow — a durable handle for an orphaned durable row whose RTBF
    # cleanup could not complete inline (purge-mid-finalize AND the cleanup delete failed);
    # an ops reconcile sweep queries these to re-purge. drawer_id = the orphan content_hash.
    "orphan_cleanup_pending",
    # oversize-floor — cc-sync source-version replace hard-deleted a prior version of a source
    # file before writing its new version (drawer_id = the deleted prior content_hash).
    "cc_sync_source_replace_delete",
)


def append_history(
    *,
    op: str,
    drawer_id: str,
    tenant_id: str,
    principal_id: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    author_agent_id: str | None = None,
    source_bridge: str | None = None,
    session_id: str | None = None,
    reason: str | None = None,
    superseded_by: str | None = None,
    details_json: str | None = None,
    ts_iso: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Append one audit row for a successful memory state transition.

    Best-effort: the entire body is guarded; any exception is logged and swallowed so
    that a history-log failure can never fail or roll back the caller's memory op.
    Callers pass ``db_path=self.outbox_db_path`` so the row co-locates with the
    approval/event rows it audits.
    """
    try:
        path = Path(db_path) if db_path is not None else Path(OUTBOX_DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)  # sibling guard (client.py:561)
        with sqlite3.connect(path) as conn:
            _ensure_outbox_schema(conn)
            conn.execute(
                """
                INSERT INTO memory_history
                  (ts_iso, op, drawer_id, tenant_id, principal_id, wing, room,
                   author_agent_id, source_bridge, session_id, reason, superseded_by, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_iso or _utcnow_iso(),
                    op,
                    drawer_id,
                    tenant_id,
                    principal_id,
                    wing,
                    room,
                    author_agent_id,
                    source_bridge,
                    session_id,
                    reason,
                    superseded_by,
                    details_json,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — best-effort audit; never fail the memory op
        logger.warning(
            "memory_history append failed (op=%s drawer=%s): %s", op, drawer_id, exc
        )


def forgotten_events(db_path: str | Path | None = None) -> set[tuple[str, str]]:
    """Phase 2 G1-C — ``(drawer_id, deleted_at)`` pairs already recorded as forgotten.

    The reconciler keys idempotency on the forgetting EVENT, not just the drawer:
    a plain re-run of the reconciler is suppressed (same drawer + same deleted_at),
    but a drawer that was recovered and then forgotten AGAIN (same drawer_id, a NEW
    deleted_at) is recorded as a fresh never-lose violation — otherwise a repeat
    forgetting would be silently hidden (Codex bot P2). ``deleted_at`` is carried in
    the history row's ``details_json``. Read-only SELECT on the append-only log
    (``mode=ro``); returns an empty set if the DB/table is absent or on any read
    error — the reconciler tolerates an occasional duplicate over a crash.
    """
    try:
        path = Path(db_path) if db_path is not None else Path(OUTBOX_DB_PATH)
        if not path.exists():
            return set()
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT drawer_id, details_json FROM memory_history WHERE op = 'forgotten'"
            ).fetchall()
        out: set[tuple[str, str]] = set()
        for drawer_id, details_json in rows:
            if drawer_id is None:
                continue
            deleted_at = ""
            if details_json:
                try:
                    parsed = json.loads(details_json)
                    if isinstance(parsed, dict):
                        deleted_at = str(parsed.get("deleted_at") or "")
                except (TypeError, ValueError):
                    deleted_at = ""
            out.add((str(drawer_id), deleted_at))
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort; dupes are tolerable, crashes are not
        logger.warning("forgotten_events read failed: %s", exc)
        return set()
