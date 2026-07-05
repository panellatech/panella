"""Vendored read-only scan for stale HNSW segments.

Forked verbatim from `lib/memory/memory/backends/chroma.py` v3.3.4:
  - `_segment_appears_healthy()` (chroma.py:79-126) — kept private.
  - `quarantine_stale_hnsw()` scan loop body (chroma.py:179-228) — `os.rename`
    calls dropped; the dirs that *would* be renamed are returned as records.

This module is the read-only side of the daemon's quarantine policy
(round-3 R2 + round-4 R6, plan v7.3.1 §3 Phase A.5b). Default daemon
behavior is OFF — this module is only invoked when
`PANELLA_QUARANTINE_DRY_RUN=1`. The mutating upstream
`quarantine_stale_hnsw` is reachable via `PANELLA_QUARANTINE_MUTATE=1`
once Phase F has cleared the cost/safety benchmark and Owner approves.

Drift policy: Phase F diffs upstream HEAD against this file. If upstream
changes its safety heuristics, re-port verbatim; do not invent local
divergence.
"""

from __future__ import annotations

import os
from typing import Any


def _segment_appears_healthy(seg_dir: str) -> bool:
    """Return True if a chromadb HNSW segment dir looks intact.

    Vendored from upstream chroma.py:79-126 (v3.3.4). Sniff-tests
    `index_metadata.pickle` for pickle-protocol bytes without
    deserializing. A complete chromadb-written file starts with `0x80`
    and ends with `0x2e`. Missing metadata is treated as fresh / empty
    (healthy). Format-sniffs only — never deserializes.
    """
    meta_path = os.path.join(seg_dir, "index_metadata.pickle")
    if not os.path.isfile(meta_path):
        return True
    try:
        size = os.path.getsize(meta_path)
        if size < 16:
            return False
        with open(meta_path, "rb") as f:
            head = f.read(2)
            f.seek(-1, 2)
            tail = f.read(1)
    except OSError:
        return False
    return len(head) == 2 and head[0] == 0x80 and tail == b"\x2e"


def scan_stale_hnsw_candidates(palace_path: str, stale_seconds: float = 300.0) -> list[dict[str, Any]]:
    """Stat-first walk for stale HNSW segments. Read-only — never renames.

    Vendored from upstream `quarantine_stale_hnsw()` chroma.py:179-228
    with every `os.rename(...)` removed. Each candidate is reported as
    a record so callers can decide whether to mutate (post-Phase F) or
    just observe.

    Returns a list of dicts:
        {
            "dir": <seg_dir absolute path>,
            "sqlite_mtime": <float>,
            "hnsw_mtime": <float>,
            "mtime_gap_seconds": <float>,
            "integrity_ok": <bool>,  # True if `_segment_appears_healthy`
        }

    Only segments that pass the mtime gate AND fail the integrity gate
    would be quarantined upstream — but this read-only port reports ALL
    mtime-gate hits with their integrity verdict so dry-run telemetry
    distinguishes flush-lag (`integrity_ok=True`, no action upstream)
    from real corruption (`integrity_ok=False`, would be renamed).
    """
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    if not os.path.isfile(db_path):
        return []
    try:
        sqlite_mtime = os.path.getmtime(db_path)
    except OSError:
        return []

    candidates: list[dict[str, Any]] = []
    try:
        entries = os.listdir(palace_path)
    except OSError:
        return []

    for name in entries:
        if "-" not in name or name.startswith(".") or ".drift-" in name:
            continue
        seg_dir = os.path.join(palace_path, name)
        if not os.path.isdir(seg_dir):
            continue
        hnsw_bin = os.path.join(seg_dir, "data_level0.bin")
        if not os.path.isfile(hnsw_bin):
            continue
        try:
            hnsw_mtime = os.path.getmtime(hnsw_bin)
        except OSError:
            continue
        if sqlite_mtime - hnsw_mtime < stale_seconds:
            continue

        candidates.append(
            {
                "dir": seg_dir,
                "sqlite_mtime": sqlite_mtime,
                "hnsw_mtime": hnsw_mtime,
                "mtime_gap_seconds": sqlite_mtime - hnsw_mtime,
                "integrity_ok": _segment_appears_healthy(seg_dir),
            }
        )

    return candidates
