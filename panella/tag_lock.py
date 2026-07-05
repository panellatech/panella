"""Cross-process per-memory tag-write lock (Phase 2 G1-B).

Panella store tag updates are full-REPLACEMENT (sqlite_vec.py:2639-2644:
``new_tags = ",".join(updates["tags"])``), and HTTP exposes no conditional /
compare-and-set write. So two Panella writers doing GET-merge-PUT on the SAME drawer
can interleave and lose one writer's tag delta (last-PUT-wins clobber). This lock
SERIALIZES every Panella GET-merge-PUT that sends ``tags`` for a given memory —
``PanellaAdapter.supersede`` / ``tombstone`` plus the backfill / migration scripts.

Scope + honesty (per the brief): this is **best-effort host-local serialization**,
NOT strong CAS. ``fcntl.flock`` is advisory and coordinates only processes that
take the lock on the SAME host filesystem. That covers the real concurrency window
— VPS-side bridges, the reconciler, and the backfill/migration scripts all run on
the VPS and share ``data/``. It does NOT coordinate a different host (e.g. the Mac
codex-desktop drain, which only ``add_memory``-POSTs and never supersedes), nor
does it stop upstream consolidation (which runs inside the Panella store process). Those
residual races are covered structurally elsewhere: supersede PUTs a metadata DELTA
(upstream merges, so a concurrent upstream metadata write is not clobbered), and
the caller re-GETs to verify the tag transition landed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Panella runs only on POSIX (Linux VPS / macOS)
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Same root convention as client_raw.OUTBOX_DB_PATH so the lock dir co-locates
# with the data dir every Panella writer already shares on the host. Override via
# PANELLA_TAG_LOCK_DIR (tests point this at a tmp dir).
_ROOT = Path(os.environ.get("PANELLA_ROOT", Path(__file__).resolve().parents[1]))
_LOCK_DIR = Path(
    os.environ.get("PANELLA_TAG_LOCK_DIR", _ROOT / "data" / "locks" / "panella-tags")
)

DEFAULT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.1


class TagLockTimeout(RuntimeError):  # noqa: N818 - explicit timeout signal for callers.
    """Raised when the per-memory tag lock cannot be acquired within the timeout."""


def _lock_path(key: str) -> Path:
    # Hash the key to a fixed safe filename: callers pass a content_hash (64-hex)
    # or, for legacy/local rows, a ``drawer_...`` id — both must map to a stable,
    # filesystem-safe name, and two writers on the SAME memory must derive the
    # SAME path (so they pass the SAME identifier — the Panella store content hash).
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:32]
    return _LOCK_DIR / f"{digest}.lock"


@contextmanager
def tag_lock(key: str, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Serialize tag-mutating GET-merge-PUT writes against one memory across
    processes on this host.

    ``key`` is the memory's Panella store content hash (a.k.a. drawer_id). Acquires an
    exclusive ``flock`` on a per-key lock file, polling until ``timeout`` then
    raising ``TagLockTimeout``. Do NOT nest ``tag_lock(same_key)`` within one
    process — ``flock`` on a second fd would self-deadlock (Panella never nests).
    """
    if fcntl is None:  # pragma: no cover - non-POSIX fallback: no cross-proc lock available
        logger.warning("tag_lock: fcntl unavailable; proceeding WITHOUT cross-process serialization")
        yield
        return

    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(key)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - start >= timeout:
                    raise TagLockTimeout(
                        f"tag_lock timeout after {timeout:.1f}s for key={key[:16]}…"
                    ) from None
                time.sleep(_POLL_INTERVAL)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
