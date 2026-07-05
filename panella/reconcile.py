"""Phase 2 G1-C — read-only reconciler for the never-lose invariant.

Detects the divergence this session PROVED: upstream forgetting soft-deletes a row
(``deleted_at`` set, embeddings dropped) while Panella still tags it ``status:active``
— so the memory is invisible to recall yet Panella believes it is live (the 309-memory
violation, 2026-05-03→05-14). For each such row it appends ONE ``forgotten`` row to
the Phase-1.5 ``memory_history`` audit log (idempotent across runs), which feeds the
G0-B recovery.

STRICTLY READ-ONLY against the source of truth: it opens the Panella store store with a
WAL-safe ``mode=ro`` URI, SELECT-only, and never writes to it. The only write is the
append-only ``memory_history`` audit row in Panella's own outbox DB, and only with
``--emit`` (default is dry-run). Wiring this to a schedule is a separate, gated
Infra step; this module ships runnable-by-hand.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from panella.client_raw import OUTBOX_DB_PATH
from panella.governance import current_governance
from panella.panella_adapter import legacy_fallback_tenant
from panella.memory_history import append_history, forgotten_events

logger = logging.getLogger(__name__)

def default_store_path() -> str:
    """The Panella store store path from governance ``paths.store_path`` (deployment overlay pins the
    real on-host path; the generic base points at the self-host location)."""
    return str(Path(current_governance().paths.store_path).expanduser())
_BUSY_TIMEOUT_MS = 4000

# Dry-run query contract (G1-C). The comma-bounded match on the normalized tag
# string mirrors upstream's own tag predicate
# (sqlite_vec.py: "(',' || REPLACE(tags,' ','') || ',') GLOB ..."), so it matches
# the EXACT ``status:active`` tag — never a ``status:active_x`` substring.
FORGOTTEN_QUERY = (
    "SELECT content_hash, tags, deleted_at, metadata, memory_type "
    "FROM memories "
    "WHERE deleted_at IS NOT NULL "
    "AND (',' || REPLACE(tags, ' ', '') || ',') LIKE '%,status:active,%'"
)


def _open_readonly(store_path: str) -> sqlite3.Connection:
    """WAL-safe read-only connection to the live Panella store store.

    ``mode=ro`` (NOT ``immutable=1``) so the connection HONORS the -wal file and
    reads a coherent latest snapshot — ``immutable=1`` ignores the WAL and would
    read stale pre-checkpoint data (the live store carries a multi-MB WAL). A short
    ``busy_timeout`` keeps us from stalling behind a checkpoint; SELECT-only; the
    caller closes immediately. No ``sqlite_vec`` extension is loaded: this query
    reads only the plain ``memories`` metadata table, never the vec0 virtual table
    (verified ext-free against the live store). Requires read access to the ``.db``
    + ``-wal`` + ``-shm`` files — run on-host as a user that can read the Panella store
    store (same-user execution per the brief avoids a cross-UID permission error).
    """
    if not Path(store_path).exists():
        raise FileNotFoundError(f"Panella store store not found: {store_path}")
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def _tenant_from_tags(tags: str | None) -> str | None:
    if not tags:
        return None
    for tag in str(tags).replace(" ", "").split(","):
        if tag.startswith("tenant:"):
            return tag[len("tenant:") :] or None
    return None


def detect_forgotten(store_path: str | None = None) -> list[dict[str, Any]]:
    """Return rows upstream forgetting soft-deleted while Panella still tags them
    status:active. Read-only; never mutates the store."""
    conn = _open_readonly(store_path or default_store_path())
    try:
        rows = conn.execute(FORGOTTEN_QUERY).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for content_hash, tags, deleted_at, metadata, memory_type in rows:
        meta: dict[str, Any] = {}
        if metadata:
            try:
                parsed = json.loads(metadata)
                if isinstance(parsed, dict):
                    meta = parsed
            except (TypeError, ValueError):
                meta = {}
        tenant_id = str(
            meta.get("tenant_id") or _tenant_from_tags(tags) or legacy_fallback_tenant()
        )
        out.append(
            {
                "drawer_id": str(content_hash),
                "tenant_id": tenant_id,
                "deleted_at": deleted_at,
                "memory_type": memory_type,
                "wing": meta.get("wing"),
                "room": meta.get("room"),
            }
        )
    return out


def reconcile(
    *,
    store_path: str | None = None,
    outbox_db_path: str | Path = OUTBOX_DB_PATH,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Detect forgotten-but-active rows and (unless ``dry_run``) append one
    ``forgotten`` memory_history row per NEW event. Returns a summary dict.

    Idempotent: drawers already carrying a ``forgotten`` history row are skipped,
    so repeated runs never duplicate audit rows for the same event.
    """
    store_path = store_path or default_store_path()
    divergent = detect_forgotten(store_path)
    already = forgotten_events(outbox_db_path)
    # Key idempotency on the (drawer, deleted_at) EVENT — a re-forgetting of a
    # recovered drawer (new deleted_at) is a fresh violation, not a dup (bot P2).
    fresh = [d for d in divergent if (d["drawer_id"], str(d["deleted_at"] or "")) not in already]

    emitted = 0
    if not dry_run:
        for d in fresh:
            append_history(
                op="forgotten",
                drawer_id=d["drawer_id"],
                tenant_id=d["tenant_id"],
                wing=d.get("wing"),
                room=d.get("room"),
                source_bridge="reconciler",
                reason="upstream forgetting soft-deleted a status:active memory (never-lose violation)",
                details_json=json.dumps(
                    {"deleted_at": d["deleted_at"], "memory_type": d.get("memory_type")},
                    sort_keys=True,
                ),
                db_path=outbox_db_path,
            )
            emitted += 1

    return {
        "store_path": store_path,
        "divergent_total": len(divergent),
        "already_recorded": len(divergent) - len(fresh),
        "fresh": len(fresh),
        "emitted": emitted,
        "dry_run": dry_run,
        "query": FORGOTTEN_QUERY,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 2 G1-C — read-only reconciler for deleted_at∧status:active divergence"
    )
    parser.add_argument("--store-path", default=None)
    parser.add_argument("--outbox-db", default=str(OUTBOX_DB_PATH))
    parser.add_argument(
        "--emit",
        action="store_true",
        help="append `forgotten` memory_history rows (default: dry-run, read-only)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = reconcile(
        store_path=args.store_path,
        outbox_db_path=Path(args.outbox_db),
        dry_run=not args.emit,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
