from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from panella.panella_adapter import PanellaAdapter, PanellaDedupSkipped, _duplicate_info
from panella.store_probe import OWNER_ACTIVE_QUERY, startup_self_check


def _store(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT)")
    conn.executemany("INSERT INTO memories VALUES (?, ?, ?, ?, NULL)", rows)
    conn.commit()
    conn.close()


def test_store_probe_owner_predicate_real_schema(tmp_path):
    db = tmp_path / "sqlite_vec.db"
    _store(db, [("1", "owned", "status:active,tenant:t_owner_personal", json.dumps({}))])
    assert "SELECT EXISTS" in OWNER_ACTIVE_QUERY
    result = startup_self_check(db)
    assert result.serving is True


def test_adapter_normalizes_tags_and_duplicate_contract():
    adapter = object.__new__(PanellaAdapter)
    hit = adapter._normalize_hit({"id": "1", "memory": "hello", "tags": ["wing:owner", "room:preferences", "tenant:t_owner_personal", "status:active"], "metadata": {}})
    assert hit["wing"] == "owner"
    assert hit["room"] == "preferences"
    assert hit["tenant_id"] == "t_owner_personal"
    is_dup, existing_hash, kind = _duplicate_info({"success": False, "message": "Duplicate content exact match"})
    assert is_dup is True
    assert existing_hash is None
    assert kind == "exact"
    assert issubclass(PanellaDedupSkipped, RuntimeError)
