from __future__ import annotations

import sqlite3

import pytest

from panella.audit import AuditChainError, audit_verify_chain, audit_write
from panella.principal import root_principal


def test_audit_hash_chain_continuity_and_perms(tmp_path):
    db = tmp_path / "audit.sqlite"
    principal = root_principal()
    for index in range(100):
        audit_write(principal=principal, tenant_accessed="*", op="search", details={"i": index}, db_path=db)
    assert audit_verify_chain(db) is True
    assert (db.stat().st_mode & 0o777) == 0o600


def test_audit_hash_chain_detects_tamper(tmp_path):
    db = tmp_path / "audit.sqlite"
    audit_write(principal=root_principal(), tenant_accessed="*", op="search", details={"i": 1}, db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE audit_log SET op = 'delete' WHERE seq = 1")
    with pytest.raises(AuditChainError):
        audit_verify_chain(db)
