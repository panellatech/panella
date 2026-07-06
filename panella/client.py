"""Stable profile-driven memory client contract."""

from __future__ import annotations

import fnmatch
import hashlib
import inspect
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from panella import counters, reader
from panella._default_adapter import default_adapter
from panella.audit import AUDIT_DB_PATH, audit_write
from panella.break_glass import break_glass as open_break_glass
from panella.client_raw import FINALIZER_STALE_TTL_SECONDS, OUTBOX_DB_PATH, _ensure_outbox_schema
from panella.panella_adapter import (
    FIND_ACTIVE_HASHES_LIMIT,
    PanellaDedupSkipped,
    PanellaUnknownWriteOutcome,
    _has_excluded_recall_status,
    _utcnow_iso,
)
from panella.memory_history import append_history
from panella.principal import Principal, default_tenant_id, principal_default_for_profile
from panella.profile import AgentProfile
from panella.sanitize import sanitize, stored_content_hash

logger = logging.getLogger(__name__)


class QuotaExceeded(RuntimeError):  # noqa: N818 - public contract name from Iter A+ brief.
    """Raised when a profile quota blocks a memory operation."""


class ApprovalRequired(RuntimeError):  # noqa: N818 - public contract name from Iter A+ brief.
    """Raised by callers that choose exception-driven approval handling."""


class TenantIsolationError(RuntimeError):
    """Raised when a tenant prefilter fails closed at the client boundary."""


class RtbfFinalizeInFlight(RuntimeError):  # noqa: N818 - public retryable signal for the forget path.
    """Raised by hard_delete when an approval it must forget is mid-finalization (a LIVE finalizer
    holds it). The forget is DEFERRED, not failed — retry once the finalizer reaches a terminal
    state (then the durable row exists + is deleted). Serializes RTBF with the Stage 2 finalizer so
    a concurrent finalize can never leave an orphaned, unmapped durable row."""


@dataclass(frozen=True)
class WriteResult:
    drawer_id: str
    wing: str
    room: str
    queued_for_approval: bool = False
    approval_id: int | None = None
    # "stored" | "dedup_skipped" — populated by client.write() so callers don't
    # need the raise_dedup_skipped opt-in to distinguish (which would also
    # bypass _record_write() and leak the quota counter).
    dedup_skipped: bool = False
    # When dedup_skipped is True, this carries the upstream first-writer's
    # content hash if the adapter disclosed it (semantic match). Stays None
    # for exact-match dedup responses where Panella store did not disclose the
    # hash; bridge state stores then preserve "first-writer unknown"
    # rather than collapsing it to the local deterministic drawer_id.
    dedup_existing_hash: str | None = None
    # Phase 1 §2.C — coarse write outcome derived from state Panella already holds:
    # "queued_for_approval" | "dedup_skipped" | "stored". A per-fact op LIST is
    # Phase 2; this single coarse code lets callers branch without re-deriving
    # it from the (queued_for_approval, dedup_skipped) booleans.
    op: str = "stored"


@dataclass(frozen=True)
class AtomWrite:
    """One atom of an oversize cc-sync file's atom set (Panella store layer ③).

    ``content`` is the full embedded+stored text (``prefix + "\n\n" + body``); ``metadata`` is
    the per-atom kwargs forwarded to :meth:`MemoryClient.write` (wing, source_path,
    atom_index, atom_count, source_content_hash, …). The grouping ``ccsk:<key>`` tag and the
    ``cc-sync:<key>`` conversation_id are NOT carried here — :meth:`replace_source_atom_set`
    stamps them from the source key so the key↔tag↔conversation_id binding stays atomic and
    cannot drift from the set the no-op/verify checks read."""

    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AtomSetResult:
    """Outcome of :meth:`MemoryClient.replace_source_atom_set`.

    ``status`` is ``"unchanged"`` (complete-set no-op: A == E, nothing deleted or written) or
    ``"written"`` (prior set deleted once, full set rewritten). ``deleted`` / ``written`` are
    observability counts (``written`` is the number of write() calls; byte-identical atoms
    exact-dedup to fewer stored rows — harmless, expected)."""

    status: str
    deleted: int = 0
    written: int = 0


class MemoryClient:
    def __init__(
        self,
        profile: AgentProfile,
        principal: Principal,
        *,
        adapter: Any | None = None,
        outbox_db_path: str | Path = OUTBOX_DB_PATH,
        audit_db_path: str | Path = AUDIT_DB_PATH,
        clock: Any = time.time,
    ) -> None:
        self.profile = profile
        self.principal = principal
        self.adapter = adapter if adapter is not None else default_adapter(
            retrieval_mode=profile.retrieval_mode,
            source=f"memory-client:{profile.name}",
        )
        self.outbox_db_path = Path(outbox_db_path)
        self.audit_db_path = Path(audit_db_path)
        self.clock = clock
        self._write_timestamps: list[float] = []

    @classmethod
    def for_profile(cls, profile_name: str, **kwargs: Any) -> MemoryClient:
        profile = AgentProfile.load(profile_name)
        return cls(profile, principal_default_for_profile(profile), **kwargs)

    def search(self, query: str, k: int | None = None, wings_hint: list[str] | None = None) -> list[dict[str, Any]]:
        self.principal.require_scope("memory.read")
        self._enforce_break_glass_ttl()
        limit = min(k if k is not None else self.profile.max_query_k, self.profile.max_query_k)
        hints = wings_hint or _allowlist_wings(self.profile.read_allowlist) or [self.profile.write_default.wing]
        tenant_ids = self._tenant_ids()
        # S-read: snapshot the reader mode ONCE per search (one env read — a search never
        # straddles an env flip), and widen only the INTERNAL fetch when enabled. The
        # caller still receives <= limit <= profile.max_query_k rows; every over-fetched
        # row passes the same _filter_hits ABAC below BEFORE the reader can see it.
        # PANELLA_READER=off (default) -> fetch == limit, reader never called: fetch/ranking
        # parity with the pre-S-read pipeline. (The ONE deliberate off-mode delta lives in
        # _filter_hits: the validity net now also blocks `candidate` — a governance-hole
        # closure for non-Panella adapters, a no-op on the live adapter which already drops
        # candidates in-adapter.)
        reader_on = reader.reader_enabled()
        fetch = reader.fetch_k(limit, enabled=reader_on)
        try:
            raw_hits = self.adapter.search_memories(
                query,
                k=fetch,
                wings_hint=hints,
                retrieval_mode=self.profile.retrieval_mode,
                tenant_ids=tenant_ids,
            )
        except TypeError:
            raw_hits = self.adapter.search_memories(query, k=fetch, wings_hint=hints)

        filtered, blocked = self._filter_hits(list(raw_hits or []))
        boosted = self._apply_profile_boost(filtered)
        if reader_on:
            scorer, scorer_state = reader.resolve_scorer()
            if scorer_state == "unavailable":
                # Enabled-but-broken reranker: loud per-search counter (the load error
                # itself logs once, sticky); the search proceeds on the deterministic
                # reader++ order — read availability > opt-in rerank quality.
                counters.increment(self.profile.name, "reranker_unavailable")
            boosted = reader.select(boosted, limit, query=query, scorer=scorer)
        counters.increment(self.profile.name, "queries")
        for hit in boosted[:limit]:
            counters.increment(self.profile.name, "wing_hit", wing=str(hit.get("wing") or "unknown"))
        if blocked:
            counters.increment(self.profile.name, "privacy_blocks", count=len(blocked))
        self._audit_if_cross_tenant(
            op="search",
            target_id=None,
            details={"query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(), "k": limit},
        )
        return boosted[:limit]

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Read one active drawer by id under the same ABAC net used by search."""
        self.principal.require_scope("memory.read")
        self._enforce_break_glass_ttl()
        getter = getattr(self.adapter, "get_drawer", None)
        if getter is None:
            raise RuntimeError("memory adapter does not support get_drawer")
        raw = getter(memory_id)
        if raw is None:
            return None
        filtered, blocked = self._filter_hits([dict(raw)])
        if blocked:
            counters.increment(self.profile.name, "privacy_blocks", count=len(blocked))
        if not filtered:
            return None
        self._audit_if_cross_tenant(
            op="get_memory",
            target_id=memory_id,
            details={"memory_id": memory_id},
            tenant_accessed=str(filtered[0].get("tenant_id") or self.principal.tenant_id),
        )
        return filtered[0]

    def write(
        self,
        content: str,
        room: str,
        memory_type: str,
        *,
        conversation_id: str | None = None,
        **metadata: Any,
    ) -> WriteResult:
        self.principal.require_scope("memory.write")
        self._enforce_break_glass_ttl()
        # oversize-floor component 2 — `conversation_id` is a SCOPED control arg,
        # NOT metadata. Panella store treats a unique conversation_id as a SEMANTIC-dedup
        # skip (panella_adapter.add_memory → upstream skip_dedup=bool(conversation_id);
        # exact-hash dedup still applies). Letting arbitrary callers set it would
        # disable a 底座 quality gate for EVERY writer, so it is locked to the
        # in-process cc-sync batch: only the "cc-sync" profile, only a "cc-sync:"-
        # prefixed id. The /v1/memory/write route additionally strips it from inbound
        # metadata so a network caller can never reach this kwarg. (design §9, r6 blocker 1.)
        if conversation_id is not None and (
            self.profile.name != "cc-sync" or not conversation_id.startswith("cc-sync:")
        ):
            raise PermissionError(
                "conversation_id is scope-restricted to the cc-sync profile with a "
                f"'cc-sync:' prefix; profile={self.profile.name!r} rejected"
            )
        # Stage 2 P0 — a finalizer_only profile (e.g. panella-finalizer) has an empty
        # approval_required_for, so a normal write() would skip the gate and land directly via
        # the adapter = an ungated back door around the entire approval system. Refuse it here:
        # such a profile is usable ONLY by the post-approval finalizer (panella/
        # approval_finalizer.py), which gates the durable write behind the approval-provenance check.
        if self.profile.finalizer_only:
            raise PermissionError(
                f"profile {self.profile.name} is finalizer_only; durable writes go through "
                "approval_finalizer.finalize_approved_candidate(), not write()"
            )
        # G1-A (never-leak): redact-and-keep at the EARLIEST write ingress, so the
        # approval-queue copy, the deterministic drawer_id, AND the eventual Panella store
        # POST all derive from sanitized content. This is the chokepoint for the
        # HTTP /v1/memory/write, cc-sync, and command-center funnels (all call
        # write()). The adapter re-sanitizes at the POST boundary (idempotent) to
        # also catch direct-adapter callers that bypass this facade.
        content = sanitize(content)
        raise_dedup_skipped = bool(metadata.pop("raise_dedup_skipped", False))
        # Phase 1 §2.B — provenance hint that this write was machine-inferred
        # rather than verbatim-asserted. INERT in Phase 1: recorded in metadata
        # only, must NOT trigger any extract/decide path (that is Phase 2, §4).
        # Validated as a strict bool here so a malformed caller fails loud.
        infer = metadata.pop("infer", False)
        if not isinstance(infer, bool):
            raise ValueError(f"infer must be a bool, got {type(infer).__name__}")
        if memory_type not in self.profile.memory_type_allowlist:
            raise ValueError(f"memory_type not allowed for {self.profile.name}: {memory_type}")

        self._enforce_write_quota()
        wing = str(metadata.pop("wing", None) or self.profile.write_default.wing)
        room = str(room or self.profile.write_default.room)
        # Phase B discovery telemetry — logged BEFORE the enforce gate so it captures
        # what real callers ask for whether or not enforce is on. Aggregated across
        # ≥14 days of production traffic to drive the per-profile write_room_allowlist.
        logger.info(
            "memory_write_pair profile=%s wing=%s room=%s memory_type=%s",
            self.profile.name,
            wing,
            room,
            memory_type,
        )
        # ⚙️v4 P0-1 — opt-in write allowlist gate. enforce=False keeps legacy
        # profiles (7 existing) unchanged; enforce=True bridge profiles assert
        # both wing and "{wing}/{room}" are explicitly listed.
        if self.profile.enforce_write_allowlist:
            if wing not in self.profile.write_wing_allowlist:
                raise PermissionError(
                    f"profile {self.profile.name} write_wing_allowlist denies wing={wing}"
                )
            pair = f"{wing}/{room}"
            if pair not in self.profile.write_room_allowlist:
                raise PermissionError(
                    f"profile {self.profile.name} write_room_allowlist denies pair={pair}"
                )
        retention = self._retention_metadata(room)
        bitemporal = self._bitemporal_metadata()
        write_tenant_id = self._resolve_write_tenant_id(metadata.get("tenant_id"))
        payload_metadata = {
            **metadata,
            **retention,
            **bitemporal,
            "tenant_id": write_tenant_id,
            "subject_id": metadata.get("subject_id") or self.principal.subject_id,
            "actor_id": metadata.get("actor_id") or self.principal.actor_id,
            "principal_id": metadata.get("principal_id") or self.principal.id,
            # Phase 1 §2.A — provenance. Each defaults to None when unsupplied;
            # author_agent_id is NOT auto-filled from principal.id (principal_id
            # already carries that) so the log can distinguish the caller-asserted
            # author from the inferred principal.
            "author_agent_id": metadata.get("author_agent_id"),
            "source_bridge": metadata.get("source_bridge"),
            "session_id": metadata.get("session_id"),
            # Phase 1 §2.B — inert infer hint.
            "infer": infer,
            "schema_version": metadata.get("schema_version", "v2"),
            "migration_batch_id": metadata.get("migration_batch_id"),
            "memory_type": memory_type,
            "source_system": metadata.get("source_system", f"memory-client:{self.profile.name}"),
            "agent_profile": self.profile.name,
        }
        drawer_id = _deterministic_memory_id(wing, room, content)

        if self._approval_required(wing, room):
            approval_id = self._enqueue_approval(wing, room, content, memory_type, payload_metadata, drawer_id)
            self._record_write()
            self._audit_if_cross_tenant(
                op="write",
                target_id=drawer_id,
                tenant_accessed=write_tenant_id,
                details={"queued_for_approval": True},
            )
            append_history(
                op="queued_for_approval",
                drawer_id=drawer_id,
                tenant_id=write_tenant_id,
                principal_id=self.principal.id,
                wing=wing,
                room=room,
                author_agent_id=payload_metadata.get("author_agent_id"),
                source_bridge=payload_metadata.get("source_bridge"),
                session_id=payload_metadata.get("session_id"),
                db_path=self.outbox_db_path,
            )
            return WriteResult(drawer_id=drawer_id, wing=wing, room=room, queued_for_approval=True, approval_id=approval_id, op="queued_for_approval")

        writer = getattr(self.adapter, "add_memory", None) or getattr(self.adapter, "add_drawer", None)
        if writer is None:
            raise RuntimeError("memory adapter does not support writes")
        dedup_skipped = False
        dedup_existing_hash: str | None = None
        try:
            # Forward conversation_id ONLY when set AND the resolved writer accepts it
            # (PanellaAdapter.add_memory does; a writer whose signature omits it would
            # TypeError). conversation_id=None keeps the exact pre-existing
            # positional shape, so the 7 legacy profiles are byte-for-byte unchanged.
            if conversation_id is not None and _writer_accepts_conversation_id(writer):
                written_id = str(
                    writer(wing, room, content, payload_metadata, conversation_id=conversation_id) or drawer_id
                )
            else:
                written_id = str(writer(wing, room, content, payload_metadata) or drawer_id)
        except PanellaDedupSkipped as exc:
            if raise_dedup_skipped:
                raise
            dedup_skipped = True
            dedup_existing_hash = exc.existing_hash
            written_id = str(exc.existing_hash or drawer_id)
        # Charge the write quota only for real writes. A dedup_skipped write is a
        # no-op (content already stored); counting it lets a trusted batch bridge's
        # daily re-scan of already-present files burn the burst budget and starve
        # genuinely-new writes (cc-sync 2026-05-29 incident). Profiles default to
        # quota_counts_dedup=True (unchanged anti-spam posture); cc-sync opts out.
        # The approval-queue _record_write() at the top of this method is unchanged.
        if not (dedup_skipped and not self.profile.quota_counts_dedup):
            self._record_write()
        self._audit_if_cross_tenant(
            op="write",
            target_id=written_id,
            tenant_accessed=write_tenant_id,
            details={
                "queued_for_approval": False,
                "dedup_skipped": dedup_skipped,
            },
        )
        # On dedup_skipped, written_id is the upstream content hash ONLY when Panella store
        # disclosed it (exc.existing_hash); otherwise it falls back to the LOCAL
        # deterministic drawer id, which is NOT the reconciliation join key. Record
        # the join-key provenance in details_json so reconciliation never silently
        # mis-joins a local-fallback id as an upstream content hash (Codex bot P2).
        dedup_details = (
            json.dumps(
                {
                    "dedup_existing_hash": dedup_existing_hash,
                    "drawer_id_kind": "upstream_content_hash" if dedup_existing_hash else "local_fallback",
                },
                sort_keys=True,
            )
            if dedup_skipped
            else None
        )
        append_history(
            op="dedup_skipped" if dedup_skipped else "stored",
            drawer_id=written_id,
            tenant_id=write_tenant_id,
            principal_id=self.principal.id,
            wing=wing,
            room=room,
            author_agent_id=payload_metadata.get("author_agent_id"),
            source_bridge=payload_metadata.get("source_bridge"),
            session_id=payload_metadata.get("session_id"),
            details_json=dedup_details,
            db_path=self.outbox_db_path,
        )
        return WriteResult(
            drawer_id=written_id,
            wing=wing,
            room=room,
            queued_for_approval=False,
            dedup_skipped=dedup_skipped,
            dedup_existing_hash=dedup_existing_hash,
            op="dedup_skipped" if dedup_skipped else "stored",
        )

    @contextmanager
    def break_glass(self, reason: str, ttl_seconds: int = 600):
        old_principal = self.principal
        with open_break_glass(reason, ttl_seconds=ttl_seconds, caller=old_principal, audit_db_path=self.audit_db_path) as principal:
            self.principal = principal
            try:
                yield principal
            finally:
                self.principal = old_principal

    def tombstone(self, drawer_id: str, reason: str) -> bool:
        self.principal.require_scope("memory.write")
        self._enforce_break_glass_ttl()
        if not str(reason or "").strip():
            raise ValueError("tombstone reason is required")
        if not self._require_mutation_target_owned(drawer_id):
            return False
        tombstone = getattr(self.adapter, "tombstone", None)
        if tombstone is None:
            raise RuntimeError("memory adapter does not support tombstone")
        ok = bool(tombstone(drawer_id, reason, principal=self.principal))
        self._audit_if_cross_tenant(
            op="tombstone",
            target_id=drawer_id,
            details={"reason": reason, "hard_delete": False},
        )
        if ok:
            append_history(
                op="tombstone",
                drawer_id=drawer_id,
                tenant_id=self.principal.tenant_id,
                principal_id=self.principal.id,
                reason=reason,
                db_path=self.outbox_db_path,
            )
        return ok

    def supersede(self, drawer_id: str, reason: str, *, superseded_by: str | None = None) -> bool:
        """Phase 1 §3.2 — mark a memory superseded (validity ended) WITHOUT
        deleting it. Mirrors the tombstone() facade: scope check, adapter
        GET-merge-PUT, cross-tenant audit. status:superseded != status:tombstoned;
        the row stays retrievable via get_drawer (never-lose) but is excluded
        from recall by the validity predicate. Appends a best-effort memory_history
        row (item E, Phase 1.5) after a successful supersede.
        """
        self.principal.require_scope("memory.write")
        self._enforce_break_glass_ttl()
        if not str(reason or "").strip():
            raise ValueError("supersede reason is required")
        if not self._require_mutation_target_owned(drawer_id):
            return False
        supersede = getattr(self.adapter, "supersede", None)
        if supersede is None:
            raise RuntimeError("memory adapter does not support supersede")
        ok = bool(supersede(drawer_id, reason, superseded_by=superseded_by, principal=self.principal))
        self._audit_if_cross_tenant(
            op="supersede",
            target_id=drawer_id,
            details={"reason": reason, "superseded_by": superseded_by, "hard_delete": False},
        )
        if ok:
            append_history(
                op="supersede",
                drawer_id=drawer_id,
                tenant_id=self.principal.tenant_id,
                principal_id=self.principal.id,
                reason=reason,
                superseded_by=superseded_by,
                db_path=self.outbox_db_path,
            )
        return ok

    def delete_prior_source_versions(
        self, source_artifact_key: str, *, keep_content_hash: str | None = None
    ) -> list[str]:
        """oversize-floor conflict-detection fix — cc-sync source-version REPLACE.

        Find every ACTIVE row tagged ``ccsk:<source_artifact_key>`` in the caller's tenant and
        hard-delete them (except ``keep_content_hash``), so a re-synced OVERSIZE file's new version
        is written with NO large near-duplicate active neighbor — which is exactly what Panella store's
        post-insert ``_detect_conflicts`` needs to run its uncapped O(n*m) ``SequenceMatcher`` under
        ``_conn_lock`` and wedge the whole service. Returns the deleted content_hashes.

        This is NOT RTBF: it replaces cc-sync's OWN prior version of a file that is canonical on
        disk (re-derivable), so it deliberately uses the raw adapter DELETE rather than the
        break-glass/RTBF ``hard_delete()`` cascade. Tightly scoped: cc-sync profile ONLY; the
        ``source_artifact_key`` is a sha256 of (tenant, device, path) so a tag match is tenant-,
        device-, and path-bound by construction; each candidate ALSO passes the
        ``_require_mutation_target_owned`` tenant-ownership IDOR guard before deletion.
        """
        self.principal.require_scope("memory.write")
        self._enforce_break_glass_ttl()
        if self.profile.name != "cc-sync":
            raise PermissionError(
                f"delete_prior_source_versions is restricted to the cc-sync profile; "
                f"profile={self.profile.name!r} rejected"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", source_artifact_key or ""):
            raise ValueError("source_artifact_key must be a sha256 hex digest")
        # Preflight the write quota BEFORE the destructive delete: if the caller has exhausted its
        # quota, the subsequent write() would QuotaExceeded — and a delete-then-quota-fail would leave
        # the source with NO active version until a later run (recall gap). Raising here (a pure check;
        # _record_write does the increment) means a quota tempfail never deletes the prior. (GH-bot P2.)
        self._enforce_write_quota()
        finder = getattr(self.adapter, "find_active_hashes_by_tag", None)
        deleter = getattr(self.adapter, "hard_delete", None)
        if finder is None or deleter is None:
            return []  # legacy/in-process adapter — no source-version replace
        tenant_id = self._write_tenant_id()
        candidates = finder(f"ccsk:{source_artifact_key}", tenant_id)
        deleted: list[str] = []
        for content_hash in candidates:
            if keep_content_hash and content_hash == keep_content_hash:
                continue
            # per-row tenant-ownership guard (IDOR): never delete a row outside the caller's tenant.
            if not self._require_mutation_target_owned(content_hash):
                continue
            if bool(deleter(content_hash, "cc-sync oversize source-version replace", principal=self.principal)):
                deleted.append(content_hash)
                self._audit_if_cross_tenant(
                    op="hard_delete",
                    target_id=content_hash,
                    tenant_accessed=tenant_id,
                    details={"reason": "cc_sync_source_version_replace", "source_artifact_key": source_artifact_key},
                )
                append_history(
                    op="cc_sync_source_replace_delete",
                    drawer_id=content_hash,
                    tenant_id=tenant_id,
                    principal_id=self.principal.id,
                    reason="cc-sync oversize source-version replace",
                    db_path=self.outbox_db_path,
                )
        # Fail-closed post-condition: NO prior active version may remain. A hard_delete can return
        # False without raising (upstream HTTP 200 {"success": false} — e.g. sqlite "database is
        # locked" under _conn_lock, or "not initialized"), which the per-row check above silently
        # tolerates. Re-verify: if any non-kept prior version is still active+tagged, RAISE so the
        # caller aborts the write — writing next to a surviving large near-dup would re-introduce the
        # exact conflict-detection wedge this exists to prevent. (find_active_hashes_by_tag also
        # raises on overflow, so this is bounded.)
        remaining = [h for h in finder(f"ccsk:{source_artifact_key}", tenant_id) if h != keep_content_hash]
        if remaining:
            raise PanellaUnknownWriteOutcome(
                f"delete_prior_source_versions: {len(remaining)} prior version(s) still active after "
                f"delete for source_artifact_key={source_artifact_key}: {remaining}; refusing to leave "
                "a conflict-detection near-duplicate"
            )
        return deleted

    def replace_source_atom_set(
        self,
        source_artifact_key: str,
        atoms: list[AtomWrite],
        *,
        room: str,
        memory_type: str,
    ) -> AtomSetResult:
        """Atomically replace an oversize cc-sync file's stored atom SET (Panella store layer ③).

        An oversize file is stored as N small structural atoms (see ``panella/atomize.py``),
        all tagged ``ccsk:<source_artifact_key>``. This is the SOURCE-LEVEL set commit: it must
        NOT loop atoms through ``_commit_item``/per-atom replace, which would make each atom
        delete the previous sibling so only the last survives (design §4, R1 P0#1). Flow:

        1. **Complete-set no-op (A == E):** ``E`` = the deduped expected stored-hash set
           ``{stored_content_hash(atom.content)}``; ``A`` = the active ``ccsk:<key>`` hash set
           (``find_active_hashes_by_tag``). Return ``unchanged`` (NO delete, NO write) ONLY if
           ``A == E`` exactly. Any subset/superset/mismatch/unreadable → fall through to rewrite.
           This self-heals a crash that wrote only atoms 0..k (``A ⊊ E`` → rewrite next sync),
           is dedup-immune (both sides deduped), and ends the daily churn the whole-file raw
           hash index can't stop (atoms carry per-atom NORMALIZED hashes).
        2. **N-quota preflight** for ``len(atoms)`` writes BEFORE any delete — a set that can't
           fit raises :class:`QuotaExceeded` with the prior set UNTOUCHED.
        3. **Delete the prior set ONCE** via ``delete_prior_source_versions(keep=None)`` (reuses
           layer ②'s fail-closed post-condition + IDOR guard + audit/history). NOT per-atom.
        4. **Write all atoms**, each through the gated ``write()`` with the ``cc-sync:<key>``
           conversation_id and the ``ccsk:<key>`` grouping tag stamped here (so the key↔tag↔
           conversation_id binding stays atomic with the set the no-op/verify read).
        5. **Fail-closed verify (A == E):** re-read the active set; ``A != E`` → RAISE. The next
           sync's step-1 no-op re-detects and rewrites (genuinely self-healing); bounded recall
           gap until then (same class as layer ②'s delete-then-write window).

        cc-sync profile ONLY (mirrors ``delete_prior_source_versions``); destructive + governed.
        """
        self.principal.require_scope("memory.write")
        self._enforce_break_glass_ttl()
        if self.profile.name != "cc-sync":
            raise PermissionError(
                f"replace_source_atom_set is restricted to the cc-sync profile; "
                f"profile={self.profile.name!r} rejected"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", source_artifact_key or ""):
            raise ValueError("source_artifact_key must be a sha256 hex digest")
        if not atoms:
            # An oversize file always yields ≥1 atom; an empty set would delete every prior and
            # store nothing. Refuse rather than silently wipe the source (caller bug).
            raise ValueError("replace_source_atom_set requires a non-empty atom set")

        tag = f"ccsk:{source_artifact_key}"
        conversation_id = f"cc-sync:{source_artifact_key}"
        tenant_id = self._write_tenant_id()
        expected = {stored_content_hash(atom.content) for atom in atoms}
        # Reject an over-cap set BEFORE any delete/write (GH-bot P2): the no-op/verify/delete-prior
        # reads go through find_active_hashes_by_tag, which fails closed at FIND_ACTIVE_HASHES_LIMIT
        # active rows. If a source's stored atom set reached that limit, the FIRST sync would write
        # the whole set (no priors → delete is a no-op) and only THEN overflow at verify — leaving a
        # large active set that subsequent retries also can't enumerate or delete (operator-cleanup
        # only). Fail loudly upfront instead, so an over-large source file is never partially stored.
        if len(expected) >= FIND_ACTIVE_HASHES_LIMIT:
            raise PanellaUnknownWriteOutcome(
                f"replace_source_atom_set: atom set for source_artifact_key={source_artifact_key} has "
                f"{len(expected)} distinct atoms, at/above the {FIND_ACTIVE_HASHES_LIMIT}-row tag-lookup "
                "cap; refusing to write an un-enumerable set (split the source file)"
            )
        finder = getattr(self.adapter, "find_active_hashes_by_tag", None)

        # Step 1 — complete-set no-op. Unreadable (finder absent OR raises, e.g. overflow) →
        # cannot confirm unchanged → fall through to the authoritative rewrite path.
        if finder is not None:
            active: set[str] | None
            try:
                active = set(finder(tag, tenant_id))
            except Exception:  # noqa: BLE001 — any read failure means "can't confirm unchanged"
                active = None
            if active is not None and active == expected:
                return AtomSetResult(status="unchanged")

        # Step 2 — N-aware quota preflight BEFORE the destructive delete.
        self.check_write_quota(len(atoms))
        # Step 3 — delete the entire prior set once (keep=None). Reuses layer ②'s fail-closed
        # post-condition + IDOR guard; raises on a silent-fail prior, aborting before any write.
        deleted = self.delete_prior_source_versions(source_artifact_key, keep_content_hash=None)
        # Step 4 — write every atom fresh.
        written = 0
        for atom in atoms:
            metadata = {k: v for k, v in atom.metadata.items() if k not in ("room", "memory_type")}
            # Prepend the grouping tag so an explicit caller tag can never push it past the
            # adapter's tag cap, mirroring _commit_item.
            metadata["tags"] = [tag, *list(metadata.get("tags") or [])]
            self.write(
                content=atom.content,
                room=room,
                memory_type=memory_type,
                conversation_id=conversation_id,
                **metadata,
            )
            written += 1
        # Step 5 — fail-closed completeness verify (the SAME A == E set comparison as step 1,
        # not a bare count). A crash/transient mid-set already raised before here; this catches a
        # silent partial/extra (e.g. an unexpected surviving sibling) so the operator sees it AND
        # the next sync's no-op rewrites.
        if finder is not None:
            final_active = set(finder(tag, tenant_id))
            if final_active != expected:
                raise PanellaUnknownWriteOutcome(
                    f"replace_source_atom_set verify failed for source_artifact_key="
                    f"{source_artifact_key}: active set ({len(final_active)}) != expected "
                    f"({len(expected)}); next sync re-detects A != E and rewrites"
                )
        return AtomSetResult(status="written", deleted=len(deleted), written=written)

    def hard_delete(self, drawer_id: str, reason: str) -> bool:
        self.principal.require_scope("memory.write")
        self.principal.require_active_break_glass()
        if not self.principal.is_root_with_break_glass():
            raise PermissionError("hard delete requires active break-glass")
        if not str(reason or "").strip():
            raise ValueError("hard delete reason is required")
        if not self._require_mutation_target_owned(drawer_id):
            return False
        hard_delete = getattr(self.adapter, "hard_delete", None)
        if hard_delete is None:
            raise RuntimeError("memory adapter does not support hard delete")
        # Stage 2 P0 — serialize with the finalizer: CLAIM the linked approval rows as
        # 'rtbf_deleting' so a concurrent finalize can't write/record a durable row after this
        # point (and DEFER if a LIVE finalize is in-flight). Returns the claimed approval ids for
        # the marker sweep below.
        claimed_approval_ids = self._claim_sidecar_for_rtbf(drawer_id)
        ok = bool(hard_delete(drawer_id, reason, principal=self.principal))
        # RTBF divergent-id cascade (Codex r8/r9): an approved queued write drains under a re-derived
        # id (D_event != the caller's D_write), so the delete above (by D_write) misses the real
        # upstream row. Resolve those ids from the LOCAL outbox mapping and delete them upstream
        # BEFORE purging that mapping — the outbox is the only place the D_write->D_event link lives,
        # so if an upstream delete RAISES we must keep the mapping (skip the purge below) for a retry
        # to rediscover it; letting the exception propagate does exactly that.
        divergent_ids = sorted(self._resolve_divergent_upstream_ids(drawer_id))
        for other_id in divergent_ids:
            if hard_delete(other_id, reason, principal=self.principal):
                ok = True
        # Stage 2 P0 — marker sweep BEFORE purging the sidecars: delete any durable row a finalizer
        # already wrote for a claimed approval that the divergent-id resolve missed (its
        # completed_memory_id was not yet recorded — esp. a DEAD stale finalizer that wrote upstream
        # but never recorded). The unique approval_ref:{id} marker recovers it. If a lookup/delete
        # RAISES, it propagates and aborts before the sidecar purge → the mapping is kept for retry.
        marker_deleted = sorted(self._rtbf_marker_sweep(claimed_approval_ids, reason))
        if marker_deleted:
            ok = True
        # Divergent upstream rows are confirmed gone (a raise above aborts before here) → now drop
        # the local sidecars UNCONDITIONALLY: a queued-but-unapproved candidate has its full text in
        # approval_queue with NO upstream row yet (upstream delete returned False), but the local
        # copy must still be forgotten.
        self._hard_delete_sidecar(drawer_id)
        # Record EVERY id this RTBF deleted (D_write + any divergent D_event + marker-swept) in the
        # durable, append-only tombstones so restore-replay and manual re-purge are complete — the
        # local mapping that named the divergent ids is now gone (Codex r9 P1).
        divergent_details: dict[str, Any] = {}
        if divergent_ids:
            divergent_details["divergent_upstream_ids"] = divergent_ids
        if marker_deleted:
            divergent_details["finalizer_marker_deleted_ids"] = marker_deleted
        if ok:
            append_history(
                op="hard_delete",
                drawer_id=drawer_id,
                tenant_id=self.principal.tenant_id,
                principal_id=self.principal.id,
                reason=reason,
                details_json=(json.dumps(divergent_details, sort_keys=True) if divergent_details else None),
                db_path=self.outbox_db_path,
            )
        self._audit_if_cross_tenant(
            op="hard_delete",
            target_id=drawer_id,
            details={"reason": reason, "hard_delete": True, **divergent_details},
        )
        return ok

    def stats(self) -> dict[str, Any]:
        today = counters.stats(self.profile.name)
        writes = int(today.get("writes", 0))
        return {
            **today,
            "quota_remaining": max(0, self.profile.write_quota.daily_max_drawers - writes),
        }

    def aggregate_stats(self, *, wing_filter: str | None = None) -> dict[str, Any]:
        """Corpus aggregate stats. Honors read_allowlist + deny + tenant scope.

        Called by the HTTP /v1/memory/stats route AND by count_by_wing/count_by_room.
        Returns: {total_drawers, wing_breakdown: [{wing, drawer_count, rooms,
        most_recent_write_ts}], last_synced_ts}.

        Tenant scoping happens at the adapter layer (Codex PR #142 P1) — passes
        self._tenant_ids() so foreign-tenant rows are dropped BEFORE tallying.
        """
        self.principal.require_scope("memory.read")
        self._enforce_break_glass_ttl()
        tenant_ids = self._tenant_ids()
        try:
            raw = self.adapter.aggregate_stats(wing_filter=wing_filter, tenant_ids=tenant_ids)
        except TypeError:
            # Adapter doesn't accept tenant_ids (legacy / in-process path) — fall back
            raw = self.adapter.aggregate_stats(wing_filter=wing_filter)
        filtered_wings: list[dict[str, Any]] = []
        for row in raw.get("wing_breakdown", []):
            wing = row["wing"]
            # Filter rooms within this wing using the same gate as _filter_hits.
            permitted_rooms: dict[str, int] = {}
            for room, count in (row.get("rooms") or {}).items():
                path = f"{wing}/{room}"
                if _matches_any(path, self.profile.deny):
                    continue
                if not _matches_any(path, self.profile.read_allowlist):
                    continue
                permitted_rooms[room] = count
            if permitted_rooms:
                filtered_wings.append({
                    "wing": wing,
                    "drawer_count": sum(permitted_rooms.values()),
                    "rooms": permitted_rooms,
                    "most_recent_write_ts": row.get("most_recent_write_ts"),
                })
        return {
            "total_drawers": sum(w["drawer_count"] for w in filtered_wings),
            "wing_breakdown": filtered_wings,
            "last_synced_ts": raw.get("last_synced_ts"),
        }

    def count_by_wing(self) -> dict[str, int]:
        """Per-wing drawer count. Wraps aggregate_stats (scope + allowlist enforced there)."""
        result = self.aggregate_stats()
        return {row["wing"]: row["drawer_count"] for row in result["wing_breakdown"]}

    def count_by_room(self, wing: str) -> dict[str, int]:
        """Per-room drawer count within one wing. Returns only allowlist-permitted rooms."""
        result = self.aggregate_stats(wing_filter=wing)
        for row in result["wing_breakdown"]:
            if row["wing"] == wing:
                return row["rooms"]
        return {}

    def sample_by_wing(self, wing: str, k: int = 10) -> list[dict[str, Any]]:
        """Return k recent drawers from a wing. Metadata + first-240-chars snippet.

        Tenant scoping happens at the adapter (Codex PR #142 P2) — without it,
        _filter_hits raises TenantIsolationError on foreign-tenant rows and
        breaks the probe instead of returning valid in-tenant samples.
        """
        self.principal.require_scope("memory.read")
        self._enforce_break_glass_ttl()
        tenant_ids = self._tenant_ids()
        try:
            raw = self.adapter.list_recent_by_wing(wing=wing, k=k, tenant_ids=tenant_ids)
        except TypeError:
            raw = self.adapter.list_recent_by_wing(wing=wing, k=k)
        # _filter_hits applies deny + read_allowlist at wing/room granularity.
        filtered, blocked = self._filter_hits(list(raw or []))
        if blocked:
            counters.increment(self.profile.name, "privacy_blocks", count=len(blocked))
        for hit in filtered:
            content = str(hit.get("content") or "")
            if len(content) > 240:
                hit["content"] = content[:240] + "..."
        return filtered[:k]

    def _filter_hits(self, hits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        allowed: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        tenant_ids = self._tenant_ids()
        is_root = self.principal.is_root_with_break_glass()
        for hit in hits:
            # Phase 1 §3.0/§3.3 — validity predicate as an EXCLUSION of
            # EXCLUDED_RECALL_STATUSES (NOT "require status:active") so
            # legacy/untagged rows (which normalize to status="active") stay
            # visible. Shares the adapter's predicate (status field AND
            # status: tags) so the client net can never drift from the
            # adapter's set — the hand-rolled pair here used to miss
            # `candidate`, letting unapproved rows through non-Panella/fake
            # adapters (S-read plan F3). This is a redundant client-side net:
            # the load-bearing drop for semantic backfill happens in-adapter
            # BEFORE the [:k] slice (search_memories); this runs on the
            # already-sliced rows and cannot backfill — keep both, do not
            # optimize either away.
            if _has_excluded_recall_status(hit):
                blocked.append(hit)
                continue
            hit_tenant = hit.get("tenant_id")
            # ⚙️v3 P0-3: None tenant on a non-root caller is fail-closed (the
            # adapter normalizes most hits but legacy/stale rows can still slip
            # through — block, do not surface).
            if hit_tenant is None:
                if not is_root:
                    blocked.append(hit)
                    continue
            elif "*" not in tenant_ids and str(hit_tenant) not in tenant_ids:
                raise TenantIsolationError(
                    f"tenant prefilter leaked hit {hit.get('id') or hit.get('drawer_id')} from {hit_tenant}"
                )
            path = _room_path(hit)
            if _matches_any(path, self.profile.deny) or not _matches_any(path, self.profile.read_allowlist):
                blocked.append(hit)
            else:
                allowed.append(hit)
        return allowed, blocked

    def _apply_profile_boost(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not any("score" in hit for hit in hits):
            return hits

        def boosted_score(hit: dict[str, Any]) -> float:
            wing = str(hit.get("wing") or "").strip()
            factor = self.profile.wing_boost.get(wing, self.profile.wing_boost["default"])
            try:
                return float(hit.get("score", 0.0)) * factor
            except (TypeError, ValueError):
                return 0.0

        return sorted(hits, key=boosted_score, reverse=True)

    def _enforce_write_quota(self) -> None:
        writes_today = counters.COUNTERS.count(self.profile.name, "writes")
        if writes_today >= self.profile.write_quota.daily_max_drawers:
            counters.increment(self.profile.name, "quota_violations")
            raise QuotaExceeded(f"{self.profile.name} daily memory write quota exceeded")

        now = float(self.clock())
        self._write_timestamps = [ts for ts in self._write_timestamps if now - ts < 60]
        if len(self._write_timestamps) >= self.profile.write_quota.burst_max_per_minute:
            counters.increment(self.profile.name, "quota_violations")
            raise QuotaExceeded(f"{self.profile.name} burst memory write quota exceeded")

    def check_write_quota(self, n: int) -> None:
        """Pure N-aware write-quota preflight: raise :class:`QuotaExceeded` if ``n`` more writes
        would not fit BOTH the daily and the per-minute-burst headroom. No counter increment, no
        side effect (unlike :meth:`_enforce_write_quota`, which gates a SINGLE write).

        Used by :meth:`replace_source_atom_set` BEFORE the destructive delete so a quota that
        cannot fit the WHOLE atom set raises with the prior set untouched — never a
        delete-then-quota-fail-mid-set that leaves a partial/empty set (design §4.2.a, P0#2).
        ``n <= 0`` is a no-op. Consistency: passing here for ``n ≥ 1`` implies the binary
        :meth:`_enforce_write_quota` inside each subsequent ``write()`` also passes, so a clean
        preflight never desyncs from a mid-set per-write quota raise."""
        if n <= 0:
            return
        writes_today = counters.COUNTERS.count(self.profile.name, "writes")
        if writes_today + n > self.profile.write_quota.daily_max_drawers:
            counters.increment(self.profile.name, "quota_violations")
            raise QuotaExceeded(
                f"{self.profile.name} daily memory write quota: {writes_today}+{n} "
                f"> {self.profile.write_quota.daily_max_drawers}"
            )
        now = float(self.clock())
        recent = [ts for ts in self._write_timestamps if now - ts < 60]
        if len(recent) + n > self.profile.write_quota.burst_max_per_minute:
            counters.increment(self.profile.name, "quota_violations")
            raise QuotaExceeded(
                f"{self.profile.name} burst memory write quota: {len(recent)}+{n} "
                f"> {self.profile.write_quota.burst_max_per_minute}"
            )

    def _record_write(self) -> None:
        self._write_timestamps.append(float(self.clock()))
        counters.increment(self.profile.name, "writes", wing=self.profile.write_default.wing)

    def _retention_metadata(self, room: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        ttl_days = self.profile.retention_policy.default_ttl_days
        if room in self.profile.retention_policy.ephemeral_rooms:
            ttl_days = 7
        expires_at = (now + timedelta(days=ttl_days)).isoformat() if ttl_days is not None else None
        return {
            "retention_ttl_days": ttl_days,
            "retention_expires_at": expires_at,
            "retention_stamped_at": now.isoformat(),
        }

    def _bitemporal_metadata(self) -> dict[str, Any]:
        # Phase 1 §3.1 — bi-temporal validity audit tags. valid_to=None means
        # "currently valid"; supersede() later stamps valid_to. All three use the
        # adapter's _utcnow_iso() (…SSZ) so valid_from/valid_to are directly
        # comparable (P2-2). These are AUDIT metadata, not the recall gate — recall
        # filters on the status tag/metadata, not these timestamps. (The
        # _retention_metadata family uses isoformat() shape; the two stamp families
        # are never cross-compared — acceptable.)
        now = _utcnow_iso()
        return {
            "valid_from": now,
            "valid_to": None,
            "ingested_at": now,
        }

    def _approval_required(self, wing: str, room: str) -> bool:
        return _matches_any(f"{wing}/{room}", self.profile.approval_required_for)

    def _tenant_ids(self) -> tuple[str, ...]:
        if self.principal.tenant_id == "*":
            if not self.principal.is_root_with_break_glass():
                raise PermissionError("cross-tenant scope requires an active break-glass token")
            return ("*",)
        if not self.profile.allows_tenant(self.principal.tenant_id):
            raise PermissionError(f"profile {self.profile.name} does not allow tenant {self.principal.tenant_id}")
        return (self.principal.tenant_id,)

    def _require_mutation_target_owned(self, drawer_id: str) -> bool:
        """Fail-closed cross-tenant guard for row mutations (tombstone/supersede/hard_delete).

        A caller may mutate a row ONLY in a tenant it owns. Root-with-break-glass ("*" scope)
        passes (its purpose is cross-tenant). Returns False — caller treats as a no-op — when no
        live row exists. Mirrors the read-path ``_filter_hits`` tenant logic so the mutation
        boundary is exactly as strict as the read boundary: closes the IDOR where a known/guessable
        ``drawer_id`` let one tenant tombstone/supersede another tenant's row (the mutation paths
        previously trusted upstream tags with no client-side ownership pre-check).
        """
        tenant_ids = self._tenant_ids()
        if "*" in tenant_ids:
            return True  # root-with-break-glass: cross-tenant mutation is its purpose
        getter = getattr(self.adapter, "get_drawer", None)
        if getter is None:
            # Cannot verify ownership for a non-root caller → fail closed, never fail open.
            raise RuntimeError("memory adapter does not support get_drawer; cannot verify mutation ownership")
        target = getter(drawer_id)
        if target is None:
            return False  # no live row → mutation is a no-op; nothing to mutate or leak
        target_tenant = target.get("tenant_id")
        if target_tenant is None or str(target_tenant) not in tenant_ids:
            raise PermissionError(
                f"principal {self.principal.id} (tenant {self.principal.tenant_id}) "
                f"cannot mutate drawer {drawer_id} owned by tenant {target_tenant}"
            )
        return True

    def _write_tenant_id(self) -> str:
        tenant_ids = self._tenant_ids()
        if "*" in tenant_ids:
            return default_tenant_id()
        return tenant_ids[0]

    def _resolve_write_tenant_id(self, requested_tenant_id: Any) -> str:
        write_tenant = self._write_tenant_id()
        if not requested_tenant_id:
            return write_tenant
        requested = str(requested_tenant_id)
        if self.principal.is_root_with_break_glass():
            return requested
        if requested != write_tenant:
            raise PermissionError(f"principal {self.principal.id} cannot write tenant {requested}")
        return write_tenant

    def _enforce_break_glass_ttl(self) -> None:
        self.principal.require_active_break_glass()

    def _audit_if_cross_tenant(
        self,
        *,
        op: str,
        target_id: str | None,
        details: dict[str, Any],
        tenant_accessed: str = "*",
    ) -> None:
        if not self.principal.is_root_with_break_glass():
            return
        audit_write(
            principal=self.principal,
            tenant_accessed=tenant_accessed,
            op=op,
            target_id=target_id,
            reason_code=self.principal.break_glass_token.reason if self.principal.break_glass_token else None,
            details=details,
            db_path=self.audit_db_path,
        )

    def _enqueue_approval(
        self,
        wing: str,
        room: str,
        content: str,
        memory_type: str,
        metadata: dict[str, Any],
        drawer_id: str,
    ) -> int:
        created_at = datetime.now(UTC).isoformat()
        candidate = {
            "text": content,
            "suggested_wing": wing,
            "suggested_room": room,
            "memory_type": memory_type,
            "metadata": metadata,
            "memory_id": drawer_id,
            "agent_profile": self.profile.name,
        }
        self.outbox_db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.outbox_db_path) as conn:
            _ensure_outbox_schema(conn)
            cur = conn.execute(
                """
                INSERT INTO approval_queue (candidate_json, status, created_at)
                VALUES (?, 'pending_approval', ?)
                """,
                (json.dumps(candidate, ensure_ascii=False, sort_keys=True), created_at),
            )
            return int(cur.lastrowid)

    def _scan_sidecar(
        self, conn: sqlite3.Connection, drawer_id: str
    ) -> tuple[list[int], set[int], set[str]]:
        """Read-only scan of the outbox sidecars for one drawer. Returns
        ``(stale_approval_ids, event_ids, extra_upstream_ids)``:

        - ``stale_approval_ids`` — approval_queue rows to purge: candidate.memory_id == drawer_id
          (my write path) OR linked via memory_event_id to one of this drawer's events (Telegram
          path, incl. approved-but-not-yet-drained).
        - ``event_ids`` — memory_events rows to purge: IDENTITY match (completed_memory_id column,
          or payload ``expected_id``/``metadata.memory_id`` — never a ``payload_json`` blob
          substring, which would catch an unrelated candidate that merely quotes the id, Codex
          r5/r6) OR linked from a matched approval row whose event re-derived a different id (r7).
        - ``extra_upstream_ids`` — OTHER upstream ids a matched event already DRAINED to (D_event !=
          D_write, because approve_queued_candidate re-derives the id from prefixed content); the
          caller must delete those upstream too, else the forgotten row stays searchable (r8 P1)."""
        completed_by_event: dict[int, str | None] = {}
        event_ids: set[int] = set()
        for ev_id, completed_id, payload_json in conn.execute(
            "SELECT id, completed_memory_id, payload_json FROM memory_events"
        ).fetchall():
            completed_by_event[ev_id] = completed_id
            if completed_id == drawer_id or _event_payload_identity(payload_json) == drawer_id:
                event_ids.add(ev_id)
        stale_ids: list[int] = []
        for row_id, candidate_json, mev_id in conn.execute(
            "SELECT id, candidate_json, memory_event_id FROM approval_queue"
        ).fetchall():
            if mev_id is not None and mev_id in event_ids:
                stale_ids.append(row_id)
                continue
            try:
                cand = json.loads(candidate_json)
            except (ValueError, TypeError):
                continue  # unparseable row: not this drawer's candidate, and must not abort RTBF
            # isinstance guard: a legacy/manual non-object candidate_json (`[]`, a bare string)
            # parses fine but has no `.get` — without this an UNRELATED malformed row would raise
            # AttributeError mid-scan and abort hard_delete before local cleanup completes (r6).
            if isinstance(cand, dict) and cand.get("memory_id") == drawer_id:
                stale_ids.append(row_id)
                # The linked event may carry a DIFFERENT expected_id than the caller's drawer_id —
                # approve_queued_candidate re-derives it from prefixed approval content — so an
                # identity scan of memory_events alone misses it; follow the link (Codex r7).
                if mev_id is not None:
                    event_ids.add(mev_id)
        extra_upstream_ids = {
            str(completed_by_event[ev_id])
            for ev_id in event_ids
            if completed_by_event.get(ev_id) and completed_by_event[ev_id] != drawer_id
        }
        return stale_ids, event_ids, extra_upstream_ids

    def _resolve_divergent_upstream_ids(self, drawer_id: str) -> set[str]:
        """Read-only: the upstream ids an approved queued write drained to under a re-derived id
        (D_event != D_write). ``hard_delete`` deletes these upstream BEFORE purging the local
        sidecar that records the mapping, so a failed cascade leaves the mapping intact for a retry
        to rediscover (the outbox is the only place the D_write->D_event link lives — Codex r9 P1)."""
        if not self.outbox_db_path.exists():
            return set()
        with sqlite3.connect(self.outbox_db_path) as conn:
            _ensure_outbox_schema(conn)
            _, _, extra_upstream_ids = self._scan_sidecar(conn, drawer_id)
        return extra_upstream_ids

    def _hard_delete_sidecar(self, drawer_id: str) -> None:
        """RTBF v1 — purge the LOCAL content sidecars a hard-delete must also reach (Panella store's
        DELETE only removes the upstream row): the ``memory_events`` outbox AND the
        ``approval_queue`` (whose ``candidate_json`` carries the FULL candidate text +
        ``memory_id`` — the content-leak Codex flagged). Append-only audit/history tombstones are
        intentionally KEPT (the deletion must stay provable). Backups/R2 snapshots are out of
        scope for v1 — covered by the documented restore-time purge runbook, not live scrubbing.

        Runs under ``BEGIN IMMEDIATE`` so a concurrent ``approve_queued_candidate`` (which takes the
        same lock) cannot insert + link a full-content event between our scan and our delete (Codex
        r8 P2): either it commits first and we observe the link, or we commit first and it then
        finds its approval row already gone and aborts — never a surviving orphan event. Call only
        AFTER the divergent upstream ids (``_resolve_divergent_upstream_ids``) have been deleted
        upstream — this drops the local D_write->D_event mapping that records them (Codex r9 P1)."""
        if not self.outbox_db_path.exists():
            return
        with sqlite3.connect(self.outbox_db_path) as conn:
            _ensure_outbox_schema(conn)
            conn.execute("BEGIN IMMEDIATE")  # serialize with approve_queued_candidate's BEGIN IMMEDIATE
            stale_ids, event_ids, _ = self._scan_sidecar(conn, drawer_id)
            if stale_ids:
                conn.executemany(
                    "DELETE FROM approval_queue WHERE id = ?", [(rid,) for rid in stale_ids]
                )
            # Purge EXACTLY the memory_events we identified (by id) — their linked approval rows are
            # already gone above, so no orphan survives and no unrelated event is ever touched.
            if event_ids:
                conn.executemany(
                    "DELETE FROM memory_events WHERE id = ?", [(eid,) for eid in event_ids]
                )
            conn.commit()

    def _claim_sidecar_for_rtbf(self, drawer_id: str) -> list[int]:
        """Stage 2 P0 — claim the approval rows linked to ``drawer_id`` for RTBF: set
        ``finalizer_state='rtbf_deleting'`` so a concurrent finalize can NOT claim/record a durable
        row for them after this point (the finalizer's claim + redrive exclude that state, and its
        ``_record`` lost-CAS path treats it as 'forget wins' → cleans up). If a LIVE finalize is
        in-flight (``finalizer_state='finalizing'`` within the stale TTL) for any matched row, raise
        ``RtbfFinalizeInFlight`` — the forget is DEFERRED and retried once that finalize is terminal
        (then its durable row exists with ``completed_memory_id`` and is deleted). Runs under
        ``BEGIN IMMEDIATE`` so the check+claim is atomic against the finalizer's own ``BEGIN
        IMMEDIATE`` claim. Returns the claimed approval ids (for the marker sweep)."""
        if not self.outbox_db_path.exists():
            return []
        stale_cutoff = (datetime.now(UTC) - timedelta(seconds=FINALIZER_STALE_TTL_SECONDS)).isoformat()
        with sqlite3.connect(self.outbox_db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_outbox_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            stale_ids, _, _ = self._scan_sidecar(conn, drawer_id)
            for aid in stale_ids:
                row = conn.execute(
                    "SELECT finalizer_state, finalizer_claimed_at FROM approval_queue WHERE id = ?",
                    (aid,),
                ).fetchone()
                if (
                    row is not None
                    and row["finalizer_state"] == "finalizing"
                    and (row["finalizer_claimed_at"] or "") >= stale_cutoff
                ):
                    conn.rollback()
                    raise RtbfFinalizeInFlight(f"forget deferred: approval {aid} finalize in-flight")
            for aid in stale_ids:
                conn.execute(
                    "UPDATE approval_queue SET finalizer_state='rtbf_deleting', "
                    "finalizer_worker_id='rtbf' WHERE id = ?",
                    (aid,),
                )
            conn.commit()
            return list(stale_ids)

    def _rtbf_marker_sweep(self, approval_ids: list[int], reason: str) -> set[str]:
        """Stage 2 P0 — delete any durable row a finalizer wrote for a claimed approval, found by
        its unique ``approval_ref:{id}`` marker. Covers a durable row the divergent-id resolve
        missed (a dead stale finalizer that wrote upstream but never recorded
        ``completed_memory_id``). Returns the deleted content hashes. A lookup/delete error
        PROPAGATES so ``hard_delete`` aborts before purging the sidecar (mapping kept for retry)."""
        finder = getattr(self.adapter, "find_active_hash_by_marker", None)
        deleter = getattr(self.adapter, "hard_delete", None)
        if finder is None or deleter is None:
            return set()
        # RTBF is always root-with-break-glass → tenant_id '*'; the marker lookup then skips the
        # tenant tag and searches all tenants (the marker is unique, so it still returns ≤1).
        tenant_id = self.principal.tenant_id
        deleted: set[str] = set()
        for aid in approval_ids:
            content_hash = finder(f"approval_ref:{aid}", tenant_id)
            if content_hash and deleter(str(content_hash), reason, principal=self.principal):
                deleted.add(str(content_hash))
        return deleted


def _event_payload_identity(payload_json: Any) -> str | None:
    """The drawer id a ``memory_events`` row is ABOUT, read from its payload's IDENTITY fields
    only — top-level ``expected_id``, else ``metadata.memory_id`` — never the free-text
    ``content``. Both daemon outbox writers (``client_raw``) stamp these equal to the
    deterministic drawer id. Returns None for unparseable/foreign payloads, which the RTBF purge
    treats as 'not this drawer' (fail-closed against over-deletion of unrelated pending content)."""
    try:
        payload = json.loads(payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    ident = payload.get("expected_id")
    if ident:
        return str(ident)
    meta = payload.get("metadata")
    if isinstance(meta, dict) and meta.get("memory_id"):
        return str(meta["memory_id"])
    return None


def _room_path(hit: dict[str, Any]) -> str:
    wing = str(hit.get("wing") or "").strip("/")
    room = str(hit.get("room") or "").strip("/")
    if "/" in room:
        return room
    return f"{wing}/{room}".strip("/")


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _allowlist_wings(patterns: list[str]) -> list[str]:
    wings: list[str] = []
    for pattern in patterns:
        wing = pattern.split("/", 1)[0]
        if wing and "*" not in wing and wing not in wings:
            wings.append(wing)
    return wings


def _deterministic_memory_id(wing: str, room: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return f"drawer_{wing}_{room}_{digest}"


def _writer_accepts_conversation_id(writer: Callable[..., Any]) -> bool:
    """True if ``writer`` accepts a ``conversation_id`` keyword.

    PanellaAdapter.add_memory declares it explicitly; a writer that omits it would
    raise TypeError if passed one. A bound-method signature omits
    ``self``, so an explicit ``conversation_id`` param OR a ``**kwargs`` catch-all
    both qualify. Falls back to False for any uninspectable callable.
    """
    try:
        params = inspect.signature(writer).parameters
    except (TypeError, ValueError):
        return False
    if "conversation_id" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
