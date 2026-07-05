"""Append-only hash-chained audit store for memory tenant boundary events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from panella.principal import Principal

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DB_PATH = ROOT / "data" / "audit.sqlite"
ZERO_HASH = "0" * 64

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_iso TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  tenant_accessed TEXT NOT NULL,
  op TEXT NOT NULL,
  target_id TEXT,
  reason_code TEXT,
  details_json TEXT,
  prev_hash TEXT NOT NULL,
  this_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_principal ON audit_log(principal_id, ts_iso);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_log(tenant_accessed, ts_iso);
"""


class AuditChainError(RuntimeError):
    """Raised when the audit hash chain is malformed or tampered."""


def audit_connect(db_path: str | Path = AUDIT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    if not existed or (path.stat().st_mode & 0o777) != 0o600:
        os.chmod(path, 0o600)
    return conn


def audit_tail_hash(db_path: str | Path = AUDIT_DB_PATH) -> str:
    with audit_connect(db_path) as conn:
        row = conn.execute("SELECT this_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    return str(row["this_hash"]) if row else ZERO_HASH


def audit_write(
    *,
    principal: Principal,
    tenant_accessed: str,
    op: str,
    target_id: str | None = None,
    reason_code: str | None = None,
    details: dict[str, Any] | None = None,
    db_path: str | Path = AUDIT_DB_PATH,
    ts: datetime | None = None,
) -> int:
    ts_iso = (ts or datetime.now(UTC)).isoformat()
    details_json = _canonical_json(details or {}) if details else None
    with audit_connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute("SELECT seq, this_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        seq = int(last["seq"]) + 1 if last else 1
        prev_hash = str(last["this_hash"]) if last else ZERO_HASH
        row = {
            "seq": seq,
            "ts_iso": ts_iso,
            "principal_id": principal.id,
            "tenant_accessed": str(tenant_accessed),
            "op": str(op),
            "target_id": target_id,
            "reason_code": reason_code,
            "details_json": details_json,
            "prev_hash": prev_hash,
        }
        this_hash = _row_hash(prev_hash, row)
        conn.execute(
            """
            INSERT INTO audit_log (
              seq, ts_iso, principal_id, tenant_accessed, op, target_id,
              reason_code, details_json, prev_hash, this_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                ts_iso,
                principal.id,
                str(tenant_accessed),
                str(op),
                target_id,
                reason_code,
                details_json,
                prev_hash,
                this_hash,
            ),
        )
        conn.commit()
        return seq


def audit_verify_chain(db_path: str | Path = AUDIT_DB_PATH) -> bool:
    previous = ZERO_HASH
    with audit_connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT seq, ts_iso, principal_id, tenant_accessed, op, target_id,
                   reason_code, details_json, prev_hash, this_hash
              FROM audit_log
             ORDER BY seq
            """
        ).fetchall()
    for row in rows:
        data = dict(row)
        this_hash = str(data.pop("this_hash"))
        prev_hash = str(data["prev_hash"])
        if prev_hash != previous:
            raise AuditChainError(f"audit chain discontinuity at seq={row['seq']}")
        expected = _row_hash(prev_hash, data)
        if this_hash != expected:
            raise AuditChainError(f"audit row hash mismatch at seq={row['seq']}")
        previous = this_hash
    return True


def _row_hash(prev_hash: str, row_without_this_hash: dict[str, Any]) -> str:
    payload = prev_hash + _canonical_json(row_without_this_hash)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
