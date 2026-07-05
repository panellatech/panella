"""Thread-safe rolling memory counters with SQLite flush."""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATS_DB = ROOT / "data" / "memory_stats.db"


class MemoryCounters:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: Counter[tuple[str, str, str]] = Counter()

    def increment(self, agent: str, event_type: str, *, wing: str = "all", count: int = 1) -> None:
        with self._lock:
            self._counts[(agent, wing, event_type)] += count

    def get_today(self, agent: str | None = None) -> dict[str, int]:
        with self._lock:
            totals: Counter[str] = Counter()
            for (row_agent, _wing, event_type), count in self._counts.items():
                if agent is None or row_agent == agent:
                    totals[event_type] += count
            return dict(totals)

    def count(self, agent: str, event_type: str) -> int:
        return int(self.get_today(agent).get(event_type, 0))

    def flush(self, db_path: Path = DEFAULT_STATS_DB, *, ts: datetime | None = None) -> int:
        with self._lock:
            rows = [(agent, wing, event_type, count) for (agent, wing, event_type), count in self._counts.items()]
            self._counts.clear()
        if not rows:
            return 0
        flush_ts = (ts or datetime.now(UTC)).isoformat()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            ensure_schema(conn)
            conn.executemany(
                "INSERT INTO stats(ts, agent, wing, event_type, count) VALUES (?, ?, ?, ?, ?)",
                [(flush_ts, agent, wing, event_type, count) for agent, wing, event_type, count in rows],
            )
        return len(rows)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stats (
            ts TEXT NOT NULL,
            agent TEXT NOT NULL,
            wing TEXT NOT NULL,
            event_type TEXT NOT NULL,
            count INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_stats_ts ON stats(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_stats_agent ON stats(agent, ts)")


COUNTERS = MemoryCounters()


def increment(agent: str, event_type: str, *, wing: str = "all", count: int = 1) -> None:
    COUNTERS.increment(agent, event_type, wing=wing, count=count)


def stats(agent: str | None = None) -> dict[str, int]:
    return COUNTERS.get_today(agent)


def flush(db_path: Path = DEFAULT_STATS_DB) -> int:
    return COUNTERS.flush(db_path)
