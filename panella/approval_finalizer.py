"""Stage 2 P0 — post-approval durable finalization of gated memory candidates.

The synchronous replacement for the retired outbox-drain (#127). When Owner approves a
gated candidate through the AUTHENTICATED Telegram handler, the approval row is stamped
with handler-authorized provenance (``approve_authorized_telegram_candidate``) but the
durable Panella store write has no consumer. ``finalize_approved_candidate`` is that consumer.

Guarantees (Codex-converged double-95 — see migration-log/briefs/panella-stage2-p0-impl-plan.md):

- **Fail-closed provenance.** Finalize ONLY rows the authenticated handler stamped:
  ``approved_via='telegram'`` AND ``approved_by`` in the configured authorized set AND a
  linked ``memory_event_id``. A raw ``approve_queued_candidate`` call or a hand-edited
  ``status='approved'`` row is NOT finalizable.
- **No PUBLIC approval-bypassing write surface; honest in-process threat model.** The guarded
  durable write lives in this module's convention-private ``_finalize_write`` — driven only by
  ``finalize_approved_candidate`` after the DB provenance gate + claim. The removed
  ``MemoryClient.finalize_durable_write`` is gone and ``MemoryClient.write()`` refuses the
  finalizer profile, so there is no PUBLIC bypass. But a single-process Python daemon cannot
  enforce a true in-process write boundary: any code with the Panella store credentials can call the
  adapter directly (the same trust assumption as the whole in-process Panella store layer), and
  ``_finalize_write`` is underscore-private by CONVENTION, not security. The SECURITY boundary
  is the authenticated Telegram approval + the provenance gate — NOT in-process code isolation.
  Process isolation / cryptographic attestation = a documented P1+ follow-up.
- **Idempotent + re-drivable, no SQLite txn held across the HTTP write.** A CAS claim-lease
  (``finalizer_state``/``worker_id``/``claimed_at`` + ``STALE_TTL``) + a CAS record (on
  ``worker_id``) + a stable ``conversation_id=approval:{id}`` (Panella store skips SEMANTIC dedup;
  exact dedup converges to one ``content_hash``) + a unique ``approval_ref:{id}`` marker tag
  that recovers the ``content_hash`` by lookup when an exact-dup discloses none. A durable
  row is NEVER left unmapped (that would break the #310 RTBF cascade) — and on the double-rare
  edge where a row is RTBF-purged mid-finalize AND the orphan-cleanup delete ITSELF fails, the
  orphan is recorded as an ``orphan_cleanup_pending`` memory_history tombstone for a reconcile
  sweep to re-purge (so the guarantee holds even when the cleanup can't complete inline).
- **Reserved-field safe.** The durable payload is a CONTROLLED metadata block; only a
  reserved-filtered ``tags`` list is carried (marker FIRST, so the adapter's 100-tag cap can
  never truncate it).
"""

from __future__ import annotations

import json
import logging
import socket
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from panella._default_adapter import default_adapter
from panella.client_raw import (
    FINALIZER_STALE_TTL_SECONDS,
    OUTBOX_DB_PATH,
    _ensure_outbox_schema,
    build_approval_memory_payload,
)
from panella.governance import current_governance
from panella.panella_adapter import PanellaDedupSkipped
from panella.memory_history import append_history
from panella.principal import default_tenant_id, principal_default_for_profile
from panella.profile import AgentProfile
from panella.sanitize import sanitize

logger = logging.getLogger(__name__)

FINALIZER_PROFILE = "panella-finalizer"
# > adapter worst-case HTTP (~10s timeout × (3 retries + 1) + backoff ≈ 45s). The redrive
# sweep (≤ poll interval) reclaims a genuinely stuck claim after this. Shared with the RTBF path
# in client.py (defined in client_raw to avoid a circular import).
STALE_TTL_SECONDS = FINALIZER_STALE_TTL_SECONDS
# Stripped from carried candidate tags: adapter-reserved namespaces (whose last-wins read would
# corrupt identity/status) PLUS our own `approval_ref:` marker namespace (a candidate must not be
# able to inject a colliding marker that breaks the unique-marker recovery invariant).
_RESERVED_TAG_PREFIXES = ("wing:", "room:", "agent:", "mtype:", "tenant:", "status:", "approval_ref:")
_MAX_CARRIED_TAGS = 15

AdapterFactory = Callable[[], Any]


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def _default_finalizer_adapter() -> Any:
    return default_adapter(source=f"memory-client:{FINALIZER_PROFILE}")


def _now() -> datetime:
    return datetime.now(UTC)


def _strip_reserved_tags(tags: Any) -> list[str]:
    """Drop any reserved-namespace tag (`wing:/room:/agent:/mtype:/tenant:/status:`) and the
    `permanent` literal from carried candidate tags — the adapter re-emits the authoritative
    ones, and read-side namespaced tags are last-wins, so a carried reserved tag would corrupt
    identity/status."""
    out: list[str] = []
    if not isinstance(tags, list):
        return out
    for tag in tags:
        if not isinstance(tag, str):
            continue
        clean = tag.strip()
        if not clean or clean == "permanent":
            continue
        if any(clean.startswith(prefix) for prefix in _RESERVED_TAG_PREFIXES):
            continue
        out.append(clean)
    return out


# The canonical metadata keys the finalizer OWNS for a Owner-approved durable write — all of
# these are derived by build_approval_memory_payload from the FINAL content / the approval
# itself, NOT copied from the candidate. We rebuild durable metadata from THIS allowlist (not
# the full payload), so candidate-controlled provenance (author_agent_id / source_bridge /
# session_id / infer) and reserved adapter fields (status / agent / importance_score) can never
# reach the durable write (Codex diff R3 B1 — allowlist, not denylist).
_CANONICAL_METADATA_KEYS = frozenset({
    "schema_version", "tenant_id", "subject_id", "actor_id", "principal_id", "migration_batch_id",
    "memory_type", "source_system", "source_id", "wing", "room", "created_at", "event_time",
    "privacy_scope", "readable_by", "links", "memory_id", "source_file", "chunk_index",
    "added_by", "filed_at", "content_sha256", "valid_from", "valid_to", "ingested_at",
})


def _expected_approved_via() -> str:
    """The ``approved_via`` value the finalizer trusts — the CONFIGURED approval transport's name
    (``governance approval.transport.kind``). The loader already rejects empty/unknown kinds; the
    finalizer re-checks non-empty as belt-and-braces (fail-closed on any misconfiguration)."""
    return current_governance().approval.transport_kind


def reconstruct_durable_payload(
    candidate_json: str, marker: str, approval_id: int, *, approved_via: str | None = None
) -> dict[str, Any]:
    """Build the CONTROLLED durable payload from the approval candidate.

    Derives content + a canonical metadata block via ``build_approval_memory_payload`` (identity
    / bitemporal owned by Panella, derived from the FINAL content), but rebuilds the durable
    metadata from an EXPLICIT allowlist (``_CANONICAL_METADATA_KEYS``) rather than copying the
    payload wholesale — so candidate-controlled provenance (author_agent_id / source_bridge /
    session_id / infer) and reserved adapter fields (status / agent / importance_score) cannot
    survive. Tags = (reserved-filtered carried tags, capped) with the unique marker FIRST; the
    writer agent + provenance are pinned canonically.
    """
    candidate = json.loads(candidate_json)
    payload = build_approval_memory_payload(candidate, approval_id=approval_id)
    canonical = payload["metadata"]
    metadata: dict[str, Any] = {k: canonical[k] for k in _CANONICAL_METADATA_KEYS if k in canonical}
    cand_meta = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    # client.write() candidates carry tags under metadata['tags']; FeedbackCandidate-style
    # direct enqueues carry them top-level. Prefer metadata, fall back to top-level.
    carried = cand_meta.get("tags") if isinstance(cand_meta.get("tags"), list) else candidate.get("tags")
    metadata["tags"] = [marker, *_strip_reserved_tags(carried)[:_MAX_CARRIED_TAGS]]
    # Pinned canonically — NOT from the candidate (a forged candidate must not claim provenance).
    metadata["agent"] = FINALIZER_PROFILE
    metadata["agent_profile"] = FINALIZER_PROFILE
    metadata["author_agent_id"] = FINALIZER_PROFILE
    metadata["source_bridge"] = None
    metadata["session_id"] = None
    metadata["infer"] = True  # a finalized preference is a machine-inferred fact the owner approved
    via = approved_via or _expected_approved_via()
    metadata["provenance"] = {"approval_queue_id": approval_id, "capture": f"approved-via-{via}"}
    return {
        "wing": str(payload["wing"]),
        "room": str(payload["room"]),
        "content": str(payload["content"]),
        "metadata": metadata,
        "tenant_id": str(metadata.get("tenant_id") or default_tenant_id()),
    }


def _finalize_write(adapter: Any, profile: AgentProfile, built: dict[str, Any],
                    *, conversation_id: str) -> str:
    """The guarded durable write — lives ONLY here, reachable ONLY after the provenance gate +
    claim in ``finalize_approved_candidate`` (there is NO public approval-bypassing write
    method). Enforces the finalizer profile's write boundary, writes ``status:active`` via the
    adapter (``conversation_id`` makes Panella store skip SEMANTIC dedup). Returns the upstream
    ``content_hash``; propagates ``PanellaDedupSkipped`` so the caller resolves the id by marker
    lookup."""
    if not profile.finalizer_only:
        raise PermissionError(f"finalize requires a finalizer_only profile, got {profile.name}")
    wing, room = built["wing"], built["room"]
    if profile.enforce_write_allowlist:
        if wing not in profile.write_wing_allowlist:
            raise PermissionError(f"profile {profile.name} write_wing_allowlist denies wing={wing}")
        if f"{wing}/{room}" not in profile.write_room_allowlist:
            raise PermissionError(f"profile {profile.name} write_room_allowlist denies pair={wing}/{room}")
    memory_type = str(built["metadata"].get("memory_type") or "")
    if memory_type not in profile.memory_type_allowlist:
        raise ValueError(f"memory_type not allowed for {profile.name}: {memory_type}")
    content = sanitize(built["content"])
    # History is appended by _record() AFTER the DB finalization, so a write that is never
    # recorded (worker stalls / loses the claim) does not leave an orphan audit row.
    return str(adapter.add_memory(wing, room, content, built["metadata"],
                                  conversation_id=conversation_id))


def finalize_approved_candidate(
    approval_id: int,
    *,
    authorized_approvers: set[str],
    db_path: str | Path = OUTBOX_DB_PATH,
    adapter_factory: AdapterFactory = _default_finalizer_adapter,
    worker_id: str | None = None,
    expected_approved_via: str | None = None,
) -> str | None:
    """Finalize ONE approved candidate to durable ``status:active``.

    The provenance gate trusts only rows whose ``approved_via`` equals the CONFIGURED approval
    transport's name (``expected_approved_via``; default = governance ``approval.transport.kind``)
    AND whose ``approved_by`` is in ``authorized_approvers`` — so a row stamped by a channel the
    deployment does not run (e.g. a stale telegram stamp on a local_cli box) is refused.

    Returns the durable upstream ``content_hash``, or None when the row is not finalizable
    (missing/unprovenanced), already owned by a live worker, or the attempt failed (the row
    is left ``finalizer_state='failed'`` and the sweep retries it).
    """
    db_path = Path(db_path)
    if not authorized_approvers:
        # Misconfiguration, not a forged row — do NOT mark anything failed; surface + skip.
        logger.warning("finalize: no authorized approvers configured; skipping id=%s", approval_id)
        return None
    expected_via = expected_approved_via or _expected_approved_via()
    if not expected_via:
        logger.warning("finalize: no approval transport configured; skipping id=%s", approval_id)
        return None
    wid = worker_id or default_worker_id()
    now = _now()
    stale_cutoff = (now - timedelta(seconds=STALE_TTL_SECONDS)).isoformat()

    # --- 0 (provenance gate) + 1 (CAS claim), under one BEGIN IMMEDIATE; lock released before HTTP ---
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_outbox_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM approval_queue WHERE id = ?", (approval_id,)).fetchone()
        if row is None or row["status"] != "approved" or row["memory_event_id"] is None:
            conn.commit()
            return None
        if row["approved_via"] != expected_via or row["approved_by"] not in authorized_approvers:
            conn.execute(
                "UPDATE approval_queue SET finalizer_state='failed', finalizer_last_error=? "
                "WHERE id=? AND (finalizer_state IS NULL OR finalizer_state != 'finalized')",
                ("unauthorized or unprovenanced approval", approval_id),
            )
            conn.commit()
            logger.warning(
                "finalize: refusing unprovenanced approval id=%s via=%r by=%r",
                approval_id, row["approved_via"], row["approved_by"],
            )
            return None
        state = row["finalizer_state"]
        if state == "finalized":
            conn.commit()
            return row["durable_memory_id"]
        if state == "finalizing" and (row["finalizer_claimed_at"] or "") >= stale_cutoff:
            conn.commit()  # a live worker owns it
            return None
        # Claim (CAS on state). Provenance + status validated above under this same lock.
        claimed = conn.execute(
            """
            UPDATE approval_queue
               SET finalizer_state='finalizing', finalizer_worker_id=?, finalizer_claimed_at=?,
                   finalizer_attempt_count=finalizer_attempt_count+1
             WHERE id=? AND status='approved'
               AND (finalizer_state IS NULL OR finalizer_state IN ('none','failed')
                    OR (finalizer_state='finalizing' AND finalizer_claimed_at < ?))
            """,
            (wid, now.isoformat(), approval_id, stale_cutoff),
        )
        if claimed.rowcount == 0:
            conn.commit()
            return None
        candidate_json = row["candidate_json"]
        memory_event_id = row["memory_event_id"]
        conn.commit()
    finally:
        conn.close()

    # --- 2 (durable write + verify-by-lookup recovery); NO txn held ---
    marker = f"approval_ref:{approval_id}"
    profile = AgentProfile.load(FINALIZER_PROFILE)
    principal = principal_default_for_profile(profile)
    try:
        # Inside the failure path: a malformed candidate_json / payload error must _fail the
        # row (re-drivable + observable), not leave it stuck in 'finalizing' (Codex diff R3).
        built = reconstruct_durable_payload(candidate_json, marker, approval_id,
                                            approved_via=expected_via)
        tenant_id = built["tenant_id"]
        adapter = adapter_factory()
        # Pre-write claim revalidation (Codex RTBF-fix edge a): if RTBF reclaimed this row while we
        # stalled (now 'rtbf_deleting') or it was purged, ABORT — never write a durable row the
        # forget path is removing. The adapter HTTP timeout (~45s) << STALE_TTL (300s), so a write
        # can't itself outlast the lease; the only stale-reclaim window is a stall BEFORE the write,
        # which this catches. (A clean abort, NOT a _fail: the row is being forgotten, not failed.)
        if not _still_owns_claim(db_path, approval_id, wid):
            logger.info("finalize: claim lost before write (RTBF reclaim/purge) id=%s; aborting", approval_id)
            return None
        try:
            content_hash: str | None = _finalize_write(
                adapter, profile, built, conversation_id=f"approval:{approval_id}",
            )
        except PanellaDedupSkipped as exc:
            # Semantic dup discloses the first-writer hash; an exact dup discloses none →
            # recover the already-durable row's content_hash by its unique marker.
            content_hash = exc.existing_hash or adapter.find_active_hash_by_marker(marker, tenant_id)
        if not content_hash:
            content_hash = adapter.find_active_hash_by_marker(marker, tenant_id)
    except Exception as exc:  # noqa: BLE001 — record + surface; the sweep re-drives
        _fail(db_path, approval_id, wid, f"durable write failed: {exc}")
        logger.warning("finalize: durable write failed id=%s: %s", approval_id, exc)
        return None
    if not content_hash:
        _fail(db_path, approval_id, wid, "durable id unrecoverable (exact-dup, no marker match)")
        logger.warning("finalize: durable id unrecoverable id=%s", approval_id)
        return None

    # --- 3 (record, CAS on worker_id; reconcile if the claim was lost) ---
    durable_id, finalized_here = _record(db_path, approval_id, wid, str(content_hash), memory_event_id)
    if not finalized_here and durable_id is None:
        # The approval row was purged (RTBF) while we were finalizing → the durable row we wrote is
        # orphaned. Remove it to honor the forget (keeps the RTBF invariant: no orphaned durable
        # row). If the cleanup delete itself fails, a durable orphan_cleanup_pending handle is
        # recorded for a reconcile sweep.
        removed = _cleanup_orphaned_durable(adapter, principal, str(content_hash), db_path=db_path)
        if removed:
            logger.warning("finalize: approval id=%s purged mid-finalize; removed orphaned durable row", approval_id)
        else:
            logger.warning(
                "finalize: approval id=%s purged mid-finalize; orphan cleanup deferred "
                "(recorded orphan_cleanup_pending for reconcile)", approval_id,
            )
        return None
    if finalized_here and durable_id:
        # Audit row appended exactly once, by whichever worker actually finalized the durable row.
        append_history(op="finalized_active", drawer_id=durable_id, tenant_id=principal.tenant_id,
                       principal_id=principal.id, wing=built["wing"], room=built["room"], db_path=db_path)
    return durable_id


def _still_owns_claim(db_path: Path, approval_id: int, wid: str) -> bool:
    """True iff this worker STILL holds the finalize claim (state='finalizing', worker_id=wid).
    Re-checked right before the external write so a stale finalizer that RTBF reclaimed (state now
    'rtbf_deleting') — or whose row was purged — ABORTS instead of writing a durable row the forget
    path is removing (Codex RTBF-fix edge a)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_outbox_schema(conn)
        row = conn.execute(
            "SELECT finalizer_state, finalizer_worker_id FROM approval_queue WHERE id = ?",
            (approval_id,),
        ).fetchone()
    finally:
        conn.close()
    return bool(row and row["finalizer_state"] == "finalizing" and row["finalizer_worker_id"] == wid)


def _cleanup_orphaned_durable(adapter: Any, principal: Any, content_hash: str, *, db_path: Path) -> bool:
    """Remove a durable row whose approval was RTBF-purged mid-finalize. The row is not
    recall-surfaced until finalized, so the window is tiny — but if the cleanup delete itself
    can't complete (a transient delete error, or an adapter with no hard_delete), record a durable
    ``orphan_cleanup_pending`` memory_history tombstone (the content_hash + reason) so an ops
    reconcile sweep can find + re-purge it. That keeps the 'no durable row left unmapped' invariant
    honest even on the double-rare edge (purge-mid-finalize AND the cleanup delete fails).

    Returns True if the orphan was removed inline; False if it could not be removed and a pending
    handle was recorded instead (so the caller can log honestly)."""
    deleter = getattr(adapter, "hard_delete", None)
    if deleter is None:
        _record_orphan_cleanup_pending(db_path, principal, content_hash, "adapter does not support hard_delete")
        return False
    try:
        # A successful call removes the row (a False return = already gone / no-op; only an
        # exception is a real failure under the adapter contract).
        deleter(content_hash, "approval purged during finalize (RTBF); removing orphan", principal=principal)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("finalize: failed to clean up orphaned durable row %s: %s", content_hash, exc)
        _record_orphan_cleanup_pending(db_path, principal, content_hash, f"cleanup delete failed: {exc}")
        return False


def _record_orphan_cleanup_pending(db_path: Path, principal: Any, content_hash: str, reason: str) -> None:
    """Durably record an orphaned durable row whose RTBF cleanup could not complete, so an ops
    reconcile sweep can find it (memory_history ``op='orphan_cleanup_pending'``, keyed by the
    upstream ``content_hash`` in ``drawer_id``) and re-purge it. Best-effort like the rest of the
    finalizer's history (append_history swallows its own errors), but durable when the local DB is
    writable — which it must be for the finalizer to function at all."""
    append_history(
        op="orphan_cleanup_pending",
        drawer_id=content_hash,
        tenant_id=getattr(principal, "tenant_id", None) or default_tenant_id(),
        principal_id=getattr(principal, "id", None),
        reason=reason[:500],
        db_path=db_path,
    )


def _record(
    db_path: Path, approval_id: int, wid: str, content_hash: str, memory_event_id: int | None,
) -> tuple[str | None, bool]:
    """Record the finalized durable id (CAS on worker_id) + close the memory_event so #310 RTBF
    can purge the row. Returns ``(durable_id, finalized_here)``.

    On a lost CAS (another worker reclaimed the lease while this worker was in-flight), VERIFY
    the row instead of assuming: if it is genuinely ``finalized``, return that id. If it is NOT
    finalized (e.g. the reclaiming worker's write failed and it marked the row ``failed``),
    RECONCILE with THIS worker's VERIFIED ``content_hash`` — the durable Panella store row exists, so it
    must NEVER be left unmapped (broken RTBF). The reconcile is forced (no worker_id guard)
    because the durable write is real; it overrides a premature ``failed``."""
    now_iso = _now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_outbox_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        recorded = conn.execute(
            "UPDATE approval_queue SET finalizer_state='finalized', durable_memory_id=?, "
            "finalizer_worker_id=?, finalizer_last_error=NULL WHERE id=? AND finalizer_worker_id=? "
            "AND (finalizer_state IS NULL OR finalizer_state != 'finalized')",
            (content_hash, wid, approval_id, wid),
        )
        if recorded.rowcount == 0:
            cur = conn.execute(
                "SELECT finalizer_state, durable_memory_id, memory_event_id FROM approval_queue WHERE id=?",
                (approval_id,),
            ).fetchone()
            if cur is None or cur["finalizer_state"] == "rtbf_deleting":
                # Either the approval row was PURGED concurrently, or an RTBF hard_delete has CLAIMED
                # it ('rtbf_deleting') while this worker was finalizing — in both cases the forget
                # wins. The durable Panella store row we just wrote is orphaned + must be removed; signal the
                # caller (durable_id=None, finalized_here=False) to clean it up. Crucially do NOT
                # force-reconcile to 'finalized' (that would resurrect a row RTBF is forgetting).
                conn.commit()
                return None, False
            if cur["finalizer_state"] == "finalized" and cur["durable_memory_id"]:
                conn.commit()  # another worker already finalized it (convergent id)
                return str(cur["durable_memory_id"]), False
            # Not finalized but we hold a verified durable hash → reconcile (forced).
            forced = conn.execute(
                "UPDATE approval_queue SET finalizer_state='finalized', durable_memory_id=?, "
                "finalizer_worker_id=?, finalizer_last_error=NULL WHERE id=?",
                (content_hash, wid, approval_id),
            )
            if forced.rowcount == 0:  # raced to purged between SELECT and UPDATE → orphan
                conn.commit()
                return None, False
            memory_event_id = cur["memory_event_id"]
        if memory_event_id is not None:
            conn.execute(
                "UPDATE memory_events SET status='completed', completed_memory_id=?, processed_at=? WHERE id=?",
                (content_hash, now_iso, memory_event_id),
            )
        conn.commit()
    finally:
        conn.close()
    return content_hash, True


def _fail(db_path: Path, approval_id: int, wid: str, message: str) -> None:
    """Mark a claimed row failed (CAS on worker_id; NEVER clobber a finalized row)."""
    conn = sqlite3.connect(db_path)
    try:
        _ensure_outbox_schema(conn)
        conn.execute(
            "UPDATE approval_queue SET finalizer_state='failed', finalizer_last_error=? "
            "WHERE id=? AND finalizer_worker_id=? AND (finalizer_state IS NULL OR finalizer_state != 'finalized')",
            (message[:500], approval_id, wid),
        )
        conn.commit()
    finally:
        conn.close()


def redrive_pending_finalizations(
    *,
    authorized_approvers: set[str],
    db_path: str | Path = OUTBOX_DB_PATH,
    adapter_factory: AdapterFactory = _default_finalizer_adapter,
    worker_id: str | None = None,
    expected_approved_via: str | None = None,
) -> int:
    """Sweep approved-but-unfinalized rows (failed inline / process restart) through
    ``finalize_approved_candidate``. The retry safety-net — NOT a separate approval channel
    (all approvals are authenticated at the transport's verify step). Sweeps only rows stamped
    by the CONFIGURED transport (parametrized ``approved_via``). Returns count finalized."""
    db_path = Path(db_path)
    if not authorized_approvers:
        logger.warning("redrive: no authorized approvers configured; skipping sweep")
        return 0
    expected_via = expected_approved_via or _expected_approved_via()
    if not expected_via:
        logger.warning("redrive: no approval transport configured; skipping sweep")
        return 0
    stale_cutoff = (_now() - timedelta(seconds=STALE_TTL_SECONDS)).isoformat()
    placeholders = ",".join("?" for _ in authorized_approvers)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_outbox_schema(conn)
        rows = conn.execute(
            f"""
            SELECT id FROM approval_queue
             WHERE status='approved' AND memory_event_id IS NOT NULL
               AND approved_via=? AND approved_by IN ({placeholders})
               AND (finalizer_state IS NULL OR finalizer_state IN ('none','failed')
                    OR (finalizer_state='finalizing' AND finalizer_claimed_at < ?))
             ORDER BY id
            """,
            (expected_via, *sorted(authorized_approvers), stale_cutoff),
        ).fetchall()
    finally:
        conn.close()
    finalized = 0
    for row in rows:
        try:
            if finalize_approved_candidate(
                int(row["id"]), authorized_approvers=authorized_approvers,
                db_path=db_path, adapter_factory=adapter_factory, worker_id=worker_id,
                expected_approved_via=expected_via,
            ):
                finalized += 1
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the sweep
            logger.warning("redrive: finalize id=%s raised: %s", row["id"], exc)
    return finalized
