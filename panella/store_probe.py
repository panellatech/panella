"""Startup coherence self-check for the memory serving surfaces (Slice-S P2, §1.5.3).

The hazard this closes: a box whose governance identity does not match its corpus (the overlay
forgotten, or a generic config pointed at an owned store) would silently serve nothing — every
read of the real owner's rows dies in ``TenantIsolationError`` and the finalizer fail-closes.
This probe turns that silent dark-out into a LOUD refuse-to-serve (503 on the memory routes,
``/v1/health`` stays reachable for Doctor).

Mechanism: a **bounded direct-SQLite EXISTS** check against the box's own store — the pinned
Panella store HTTP contract has no bounded by-tag primitive (no ``n_results``/pagination), so HTTP is
not a bounded probe. The read pattern (``mode=ro`` URI honoring the WAL, short busy_timeout,
plain ``memories`` table only, never the vec0 virtual table) is ``reconcile.py``'s proven one.

Predicate (two probes, one query family):
  probe-A  store has ANY live ``status:active`` row?   empty → PASS (fresh box)
  probe-B  a live row ATTRIBUTED to the resolved owner tenant? present → PASS
  non-empty ∧ owner-absent ∧ no ``PANELLA_FRESH_BOX=1`` ack → INCOHERENT → refuse (503)

Attribution mirrors the adapter's read-side precedence EXACTLY (``panella_adapter._normalize_hit``:
``meta.get("tenant_id") or tenant_tag or legacy_fallback_tenant()``): metadata JSON ``tenant_id``
wins; else an explicit ``tenant:<id>`` tag; else the row falls back to the deployment's default
tenant. So the probe concludes owner-present iff the read path would actually serve at least one
row to the owner — no false-503 on a metadata-attributed corpus, no false-open on rows whose
metadata pins a foreign tenant. (A mixed multi-owner corpus is a managed-tier limitation: the
probe checks "owner present", not "no foreign tenant".)

Missing/unreachable store (R6): a bare ``FileNotFoundError`` must never propagate.
  - store file ABSENT + no overlay configured → PASS (pure-generic fresh box, nothing to serve
    wrongly)
  - store file ABSENT + overlay configured → REFUSE ("misconfigured overlay or store not
    mounted" — an identity-pinned box expects its corpus) unless the fresh-box ack is set
  - store EXISTS but unreadable/malformed → REFUSE loud (never serve blind)

This module imports nothing outside ``panella`` + stdlib (fence target).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from panella.governance import current_governance, resolve_overlay_path

logger = logging.getLogger(__name__)

FRESH_BOX_ENV = "PANELLA_FRESH_BOX"
STORE_PATH_ENV = "PANELLA_STORE_PATH"
_BUSY_TIMEOUT_MS = 4000

# The bounded query family — one EXISTS, LIMIT 1, plain `memories` table. The comma-bounded
# normalized-tag predicate mirrors upstream's own tag matching (and reconcile.py's proven query)
# so `status:active` never substring-matches `status:active_x`. The contract test pins this shape
# against a real-schema fixture store.
_NORM_TAGS = "(',' || REPLACE(tags, ' ', '') || ',')"
# The row's metadata-JSON tenant, guarded by json_valid so a malformed blob degrades to NULL
# (tag/fallback attribution) instead of erroring the probe; NULLIF folds an EMPTY-STRING
# tenant_id to NULL to match Python's falsy `meta.get("tenant_id") or tenant_tag or fallback`
# (an `""` must fall through to tag attribution, not compare-and-stop). json_valid/json_extract
# are core SQLite JSON1 functions.
_META_TENANT = (
    "(CASE WHEN metadata IS NOT NULL AND json_valid(metadata) "
    "THEN NULLIF(json_extract(metadata, '$.tenant_id'), '') END)"
)
ANY_ACTIVE_QUERY = (
    "SELECT EXISTS(SELECT 1 FROM memories WHERE deleted_at IS NULL "
    f"AND {_NORM_TAGS} LIKE '%,status:active,%' LIMIT 1)"
)
# Exactly-one tenant tag: the read path's tag attribution is LAST-wins on duplicates
# (panella_adapter._parse_namespaced_tag), which a LIKE cannot mirror — so the tag clause
# FAILS CLOSED on duplicate tenant tags (a dup-tagged row never counts as owner-attributed;
# the failure direction is a loud, ackable 503, never a false-open). REPLACE removes every
# ',tenant:' occurrence, so the length delta is 8 (=len(',tenant:')) per occurrence.
_SINGLE_TENANT_TAG = f"(length({_NORM_TAGS}) - length(REPLACE({_NORM_TAGS}, ',tenant:', ''))) = 8"
# Owner-attributed per _normalize_hit precedence: metadata tenant_id wins; else the tenant tag
# (sole occurrence only — see above); else (neither present) the row falls back to the owner.
# All three owner comparisons bind the SAME resolved owner-tenant parameter.
OWNER_ACTIVE_QUERY = (
    "SELECT EXISTS(SELECT 1 FROM memories WHERE deleted_at IS NULL "
    f"AND {_NORM_TAGS} LIKE '%,status:active,%' "
    f"AND ({_META_TENANT} = ? "
    f"OR ({_META_TENANT} IS NULL AND {_SINGLE_TENANT_TAG} "
    f"AND {_NORM_TAGS} LIKE ('%,tenant:' || ? || ',%')) "
    f"OR ({_META_TENANT} IS NULL AND {_NORM_TAGS} NOT LIKE '%,tenant:%')) LIMIT 1)"
)


@dataclass(frozen=True)
class SelfCheckResult:
    serving: bool
    reason: str


def resolve_store_path(store_path: str | os.PathLike[str] | None = None) -> Path:
    """The store path to probe: explicit arg > ``PANELLA_STORE_PATH`` env > governance
    ``paths.store_path``."""
    pointer = store_path or os.environ.get(STORE_PATH_ENV) or current_governance().paths.store_path
    return Path(pointer).expanduser()


def _fresh_box_acked() -> bool:
    return os.environ.get(FRESH_BOX_ENV, "") == "1"


def startup_self_check(store_path: str | os.PathLike[str] | None = None) -> SelfCheckResult:
    """Run the coherence probe. NEVER raises — any unexpected failure returns a refuse result
    (fail-closed), so a serving factory can arm its 503 gate without crashing /v1/health."""
    try:
        return _self_check(store_path)
    except Exception as exc:  # noqa: BLE001 — the gate must arm, not crash the app
        logger.error("memory self-check errored (refusing to serve): %s", exc)
        return SelfCheckResult(False, f"self-check error: {exc}")


def _self_check(store_path: str | os.PathLike[str] | None) -> SelfCheckResult:
    owner_tenant = current_governance().identity.default_tenant_id
    path = resolve_store_path(store_path)
    overlay_pinned = resolve_overlay_path() is not None

    if not path.exists():
        if _fresh_box_acked():
            return SelfCheckResult(True, f"store absent, {FRESH_BOX_ENV}=1 acked: {path}")
        if overlay_pinned:
            # An identity-pinned box (overlay configured) expects its corpus — a missing store is
            # a misconfigured overlay or an unmounted volume, not a fresh box.
            return SelfCheckResult(
                False,
                f"governance overlay is configured but the store is missing: {path} "
                f"(misconfigured overlay or store not mounted; set {FRESH_BOX_ENV}=1 only for a "
                "genuinely fresh box)",
            )
        return SelfCheckResult(True, f"no store yet (pure-generic fresh box): {path}")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        any_active = bool(conn.execute(ANY_ACTIVE_QUERY).fetchone()[0])
        if not any_active:
            return SelfCheckResult(True, "store has no active rows (fresh/empty box)")
        owner_present = bool(
            conn.execute(OWNER_ACTIVE_QUERY, (owner_tenant, owner_tenant)).fetchone()[0]
        )
    finally:
        conn.close()

    if owner_present:
        return SelfCheckResult(True, f"owner tenant {owner_tenant!r} represented in store")
    if _fresh_box_acked():
        return SelfCheckResult(
            True, f"owner tenant {owner_tenant!r} absent but {FRESH_BOX_ENV}=1 acked"
        )
    return SelfCheckResult(
        False,
        f"INCOHERENT: store {path} has active rows but none attributed to the resolved owner "
        f"tenant {owner_tenant!r} — the governance overlay is likely missing/wrong on this box "
        f"(serving would TenantIsolationError on every owned row). Fix the overlay (place it, "
        f"set PANELLA_GOVERNANCE_OVERLAY, restart) or ack a fresh box with {FRESH_BOX_ENV}=1.",
    )
