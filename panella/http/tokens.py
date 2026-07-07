"""Opaque bearer-token storage for the memory HTTP facade."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from panella.principal import (
    Principal,
    default_subject_id,
    default_tenant_id,
    principal_default_for_profile,
    root_principal,
)
from panella.profile import AgentProfile

TOKEN_PREFIX = "m2_"


def _bare_root_alias() -> str:
    """The root id's bare local part (the segment after ``human:``) — the legacy principal_id
    form historical tokens carry. Derived from governance, never a hardcoded name."""
    return root_principal().id.split(":", 1)[-1]


def _bare_alias_enabled() -> bool:
    """One-release compat flag for resolving the bare root alias (PANELLA_ALLOW_BARE_ROOT_ALIAS=0
    disables; default on so existing minted tokens keep working through the P2 release)."""
    return os.environ.get("PANELLA_ALLOW_BARE_ROOT_ALIAS", "1") != "0"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tokens (
  token_sha256 TEXT PRIMARY KEY,
  principal_id TEXT NOT NULL,
  tenant_scope_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  rotated_at TEXT,
  revoked_at TEXT,
  expires_at TEXT,
  last_used_at TEXT,
  label TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_tokens_principal ON tokens(principal_id);
"""


@dataclass(frozen=True)
class TokenRecord:
    token_sha256: str
    principal_id: str
    tenant_scope: tuple[str, ...]
    created_at: datetime
    rotated_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None
    last_used_at: datetime | None
    label: str

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)


class TokenStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def mint(
        self,
        *,
        principal_id: str,
        label: str,
        tenant_scope: tuple[str, ...] | list[str] | None = None,
        ttl_seconds: int | None = None,
        raw_token: str | None = None,
    ) -> str:
        token = raw_token or generate_token()
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        scope = tuple(tenant_scope or default_tenant_scope_for_principal(principal_id))
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tokens (
                  token_sha256, principal_id, tenant_scope_json, created_at,
                  rotated_at, revoked_at, expires_at, last_used_at, label
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, ?)
                """,
                (
                    token_sha256(token),
                    normalize_principal_id(principal_id),
                    json.dumps(list(scope), sort_keys=True),
                    now.isoformat(),
                    expires_at.isoformat() if expires_at else None,
                    label,
                ),
            )
        return token

    def resolve(self, raw_token: str, *, touch: bool = True) -> TokenRecord | None:
        digest = token_sha256(raw_token)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT token_sha256, principal_id, tenant_scope_json, created_at,
                       rotated_at, revoked_at, expires_at, last_used_at, label
                  FROM tokens
                 WHERE token_sha256 = ?
                """,
                (digest,),
            ).fetchone()
            if row and touch:
                conn.execute(
                    "UPDATE tokens SET last_used_at = ? WHERE token_sha256 = ?",
                    (datetime.now(UTC).isoformat(), digest),
                )
        return _record_from_row(row) if row else None

    def list(self) -> list[TokenRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT token_sha256, principal_id, tenant_scope_json, created_at,
                       rotated_at, revoked_at, expires_at, last_used_at, label
                  FROM tokens
                 ORDER BY created_at DESC
                """
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def revoke(self, label: str) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            cur = conn.execute(
                # An operator revoke is IMMEDIATE. Pull a FUTURE revoked_at back to now — otherwise a
                # token in a rotate() grace window (rotate sets revoked_at = now + grace, so the old
                # bearer stays valid until then, and resolve_bearer only rejects when revoked_at <= now)
                # would survive a `tokens revoke` while the CLI claims it is rejected. A genuinely
                # PAST revoked_at is preserved, so re-revoking never moves the timestamp (idempotent).
                # ISO-8601 UTC strings (both written via datetime.isoformat()) compare chronologically.
                "UPDATE tokens SET revoked_at = "
                "CASE WHEN revoked_at IS NULL OR revoked_at > ? THEN ? ELSE revoked_at END "
                "WHERE label = ?",
                (now, now, label),
            )
            return cur.rowcount > 0

    def rotate(self, label: str, *, grace_seconds: int = 300) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT principal_id, tenant_scope_json
                  FROM tokens
                 WHERE label = ? AND revoked_at IS NULL
                """,
                (label,),
            ).fetchone()
            if row is None:
                raise KeyError(label)
            now = datetime.now(UTC)
            revoke_at = now + timedelta(seconds=grace_seconds)
            conn.execute(
                "UPDATE tokens SET rotated_at = ?, revoked_at = ? WHERE label = ?",
                (now.isoformat(), revoke_at.isoformat(), label),
            )
        return self.mint(
            principal_id=str(row["principal_id"]),
            label=f"{label}-{now.strftime('%Y%m%d%H%M%S')}",
            tenant_scope=tuple(json.loads(str(row["tenant_scope_json"]))),
        )

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.db_path.exists()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        if not existed or (self.db_path.stat().st_mode & 0o777) != 0o600:
            os.chmod(self.db_path, 0o600)
        return conn


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_sha256(raw_token: str) -> str:
    return hashlib.sha256(str(raw_token).encode("utf-8")).hexdigest()


def principal_from_record(record: TokenRecord) -> Principal:
    principal_id = normalize_principal_id(record.principal_id)
    tenant_id = record.tenant_scope[0] if record.tenant_scope else default_tenant_id()
    root = root_principal()
    if principal_id == root.id:
        return Principal(
            id=root.id,
            tenant_id=tenant_id if tenant_id != "*" else default_tenant_id(),
            subject_id=root.subject_id,
            actor_kind="human",
            scopes=frozenset({"*"}),
            roles=root.roles,
            root_flag=True,
        )
    if principal_id.startswith("agent:"):
        return _principal_from_agent_id(principal_id, tenant_id)
    profile = AgentProfile.load(principal_id)
    return principal_default_for_profile(profile)


def normalize_principal_id(value: str) -> str:
    text = str(value).strip()
    if text == _bare_root_alias() and _bare_alias_enabled():
        return root_principal().id
    return text


def default_tenant_scope_for_principal(principal_id: str) -> tuple[str, ...]:
    normalized = normalize_principal_id(principal_id)
    if normalized == root_principal().id:
        return (default_tenant_id(),)
    if normalized.startswith("agent:") and "@" in normalized:
        return (normalized.rsplit("@", 1)[1],)
    try:
        return tuple(AgentProfile.load(normalized).tenant_scope)
    except Exception:
        return (default_tenant_id(),)


def _principal_from_agent_id(principal_id: str, tenant_id: str) -> Principal:
    return Principal(
        id=principal_id,
        tenant_id=tenant_id,
        subject_id=default_subject_id(),
        actor_kind="agent",
        scopes=frozenset({"memory.read", "memory.write"}),
        roles=frozenset({"agent_default"}),
    )


def _record_from_row(row: sqlite3.Row) -> TokenRecord:
    return TokenRecord(
        token_sha256=str(row["token_sha256"]),
        principal_id=str(row["principal_id"]),
        tenant_scope=tuple(str(item) for item in json.loads(str(row["tenant_scope_json"]))),
        created_at=_parse_dt(row["created_at"]),
        rotated_at=_parse_optional_dt(row["rotated_at"]),
        revoked_at=_parse_optional_dt(row["revoked_at"]),
        expires_at=_parse_optional_dt(row["expires_at"]),
        last_used_at=_parse_optional_dt(row["last_used_at"]),
        label=str(row["label"]),
    )


def _parse_optional_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    return _parse_dt(value)


def _parse_dt(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
