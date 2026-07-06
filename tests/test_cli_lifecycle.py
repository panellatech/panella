from __future__ import annotations

import json
import sqlite3
import tarfile
import io
from pathlib import Path

import pytest

from panella.audit import audit_write
from panella.cli import main
from panella.principal import root_principal


def _seed_store(path: Path, rows: list[tuple]) -> None:
    """Same minimal real schema as test_store_probe_adapter_contract.py's `_store` helper, plus
    the extra columns reconcile.py/export actually SELECT (content_hash, memory_type,
    created_at(_iso), updated_at(_iso)) so export's real query runs against a real-shaped row."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            content_hash TEXT, content TEXT, tags TEXT, metadata TEXT, deleted_at TEXT,
            memory_type TEXT, created_at REAL, created_at_iso TEXT,
            updated_at REAL, updated_at_iso TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _row(content_hash: str, content: str, wing: str, *, deleted_at: str | None = None) -> tuple:
    tags = f"status:active,tenant:t_owner_personal,wing:{wing}"
    return (
        content_hash, content, tags, "{}", deleted_at,
        "observation", 1.0, "2026-01-01T00:00:00Z", 1.0, "2026-01-01T00:00:00Z",
    )


def _seed_env(monkeypatch, tmp_path: Path, *, with_store=True, with_token=True, with_audit=True, with_outbox=True):
    """Point every lifecycle data source at files under tmp_path via the SAME env vars
    panella.http.config.load_config / panella.store_probe.resolve_store_path already honor —
    no governance overlay needed for these tests."""
    if with_store:
        store = tmp_path / "sqlite_vec.db"
        _seed_store(store, [_row("h1", "hello iris", "iris"), _row("h2", "hello quant", "quant")])
        monkeypatch.setenv("PANELLA_STORE_PATH", str(store))
    if with_token:
        from panella.http.tokens import TokenStore

        token_db = tmp_path / "tokens.db"
        TokenStore(token_db).mint(principal_id=root_principal().id, label="seed-token")
        monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(token_db))
    if with_audit:
        audit_db = tmp_path / "audit.sqlite"
        audit_write(principal=root_principal(), tenant_accessed="*", op="search", db_path=audit_db)
        audit_write(principal=root_principal(), tenant_accessed="*", op="search", db_path=audit_db)
        monkeypatch.setenv("PANELLA_HTTP_AUDIT_DB", str(audit_db))
    if with_outbox:
        from panella.client_raw import _ensure_outbox_schema

        outbox_db = tmp_path / "memory_outbox.db"
        conn = sqlite3.connect(outbox_db)
        _ensure_outbox_schema(conn)
        conn.execute(
            "INSERT INTO memory_events (event_type, payload_json, created_at, status, "
            "attempt_count, next_attempt_at, shadow, tenant_id, principal_id) "
            "VALUES ('test', '{}', '2026-01-01', 'pending', 0, '2026-01-01', 0, 't_owner_personal', 'p1')"
        )
        conn.commit()
        conn.close()
        monkeypatch.setenv("PANELLA_HTTP_OUTBOX_DB", str(outbox_db))


def _read_manifest(archive_path: Path) -> dict:
    with tarfile.open(archive_path, "r:gz") as tar:
        fh = tar.extractfile("MANIFEST.json")
        assert fh is not None
        return json.loads(fh.read().decode("utf-8"))


# --------------------------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------------------------- #


def test_backup_happy_path_manifest_and_store_probe(tmp_path, monkeypatch):
    _seed_env(monkeypatch, tmp_path)
    out = tmp_path / "backup.tar.gz"

    rc = main(["backup", "--out", str(out)])
    assert rc == 0
    assert out.exists()

    manifest = _read_manifest(out)
    assert manifest["format_version"] == 1
    assert manifest["panella_version"]
    assert "created_at" in manifest
    roles = {f["role"] for f in manifest["files"]}
    assert roles == {"store_db", "token_db", "audit_db", "outbox_db"}

    # every manifest hash must match the actual bytes in the tar
    with tarfile.open(out, "r:gz") as tar:
        for entry in manifest["files"]:
            data = tar.extractfile(entry["name"]).read()
            import hashlib

            assert hashlib.sha256(data).hexdigest() == entry["sha256"]

        # store_probe against the extracted store snapshot passes
        from panella.store_probe import startup_self_check

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        tar.extractall(extract_dir, filter="data")
        store_entry = next(f for f in manifest["files"] if f["role"] == "store_db")
        result = startup_self_check(extract_dir / store_entry["name"])
        assert result.serving is True


def test_backup_refuses_overwrite_without_force(tmp_path, monkeypatch):
    _seed_env(monkeypatch, tmp_path, with_token=False, with_outbox=False)
    out = tmp_path / "backup.tar.gz"
    assert main(["backup", "--out", str(out)]) == 0

    original_bytes = out.read_bytes()
    rc = main(["backup", "--out", str(out)])
    assert rc == 2
    assert out.read_bytes() == original_bytes  # untouched

    rc_forced = main(["backup", "--out", str(out), "--force"])
    assert rc_forced == 0


def test_backup_aborts_on_audit_chain_tamper(tmp_path, monkeypatch):
    """Not in the acceptance list verbatim, but proves the validate-before-package guarantee:
    a tampered SOURCE audit DB must abort backup rather than package a broken snapshot."""
    _seed_env(monkeypatch, tmp_path, with_token=False, with_outbox=False)
    audit_db = tmp_path / "audit.sqlite"
    with sqlite3.connect(audit_db) as conn:
        conn.execute("UPDATE audit_log SET op = 'tampered' WHERE seq = 1")

    rc = main(["backup", "--out", str(tmp_path / "backup.tar.gz")])
    assert rc == 1
    assert not (tmp_path / "backup.tar.gz").exists()


# --------------------------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------------------------- #


def test_restore_happy_path_into_empty_dir(tmp_path, monkeypatch):
    _seed_env(monkeypatch, tmp_path)
    backup = tmp_path / "backup.tar.gz"
    assert main(["backup", "--out", str(backup)]) == 0

    data_dir = tmp_path / "restored"
    rc = main(["restore", "--from", str(backup), "--data-dir", str(data_dir)])
    assert rc == 0
    assert (data_dir / "sqlite_vec.db").exists()
    assert (data_dir / "tokens.db").exists()
    assert (data_dir / "audit.sqlite").exists()
    assert (data_dir / "memory_outbox.db").exists()
    # sensitive files land 0600
    assert (data_dir / "audit.sqlite").stat().st_mode & 0o777 == 0o600
    assert (data_dir / "tokens.db").stat().st_mode & 0o777 == 0o600
    assert (data_dir / "memory_outbox.db").stat().st_mode & 0o777 == 0o600


def test_restore_into_nonempty_dir_refuses_without_force(tmp_path, monkeypatch):
    _seed_env(monkeypatch, tmp_path, with_token=False, with_outbox=False)
    backup = tmp_path / "backup.tar.gz"
    assert main(["backup", "--out", str(backup)]) == 0

    data_dir = tmp_path / "restored"
    data_dir.mkdir()
    (data_dir / "sqlite_vec.db").write_text("pre-existing")

    rc = main(["restore", "--from", str(backup), "--data-dir", str(data_dir)])
    assert rc == 2
    assert (data_dir / "sqlite_vec.db").read_text() == "pre-existing"  # untouched

    rc_forced = main(["restore", "--from", str(backup), "--data-dir", str(data_dir), "--force"])
    assert rc_forced == 0
    assert (data_dir / "sqlite_vec.db").read_bytes() != b"pre-existing"


def _corrupt_one_byte(src: Path, dst: Path, member_name: str) -> None:
    """Build a copy of `src` with one byte flipped inside `member_name`'s content."""
    with tarfile.open(src, "r:gz") as tar:
        members = {m.name: (m, tar.extractfile(m).read()) for m in tar.getmembers()}
    corrupted = bytearray(members[member_name][1])
    corrupted[16] ^= 0xFF
    members[member_name] = (members[member_name][0], bytes(corrupted))
    with tarfile.open(dst, "w:gz") as tar:
        for name, (_member, content) in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


def test_restore_refuses_before_placing_files_on_corrupt_snapshot(tmp_path, monkeypatch):
    _seed_env(monkeypatch, tmp_path)
    backup = tmp_path / "backup.tar.gz"
    assert main(["backup", "--out", str(backup)]) == 0

    corrupt = tmp_path / "backup-corrupt.tar.gz"
    _corrupt_one_byte(backup, corrupt, "store_db")  # tar members are keyed by role, not basename

    data_dir = tmp_path / "restored"
    rc = main(["restore", "--from", str(corrupt), "--data-dir", str(data_dir)])
    assert rc == 1
    assert not data_dir.exists()  # nothing was placed, not even the other 3 valid files


def test_restore_prints_version_mismatch_warning(tmp_path, monkeypatch, capsys):
    _seed_env(monkeypatch, tmp_path, with_token=False, with_outbox=False)
    backup = tmp_path / "backup.tar.gz"
    assert main(["backup", "--out", str(backup)]) == 0

    with tarfile.open(backup, "r:gz") as tar:
        members = {m.name: (m, tar.extractfile(m).read()) for m in tar.getmembers()}
    manifest = json.loads(members["MANIFEST.json"][1])
    manifest["panella_version"] = "0.0.1-old"
    members["MANIFEST.json"] = (members["MANIFEST.json"][0], (json.dumps(manifest) + "\n").encode())
    old_backup = tmp_path / "backup-old.tar.gz"
    with tarfile.open(old_backup, "w:gz") as tar:
        for name, (_member, content) in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    capsys.readouterr()
    rc = main(["restore", "--from", str(old_backup), "--data-dir", str(tmp_path / "restored")])
    assert rc == 0
    captured = capsys.readouterr()
    assert "0.0.1-old" in captured.err
    assert "WARNING" in captured.err


# --------------------------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------------------------- #


def test_export_seeded_wing_yields_exactly_seeded_memories(tmp_path):
    store = tmp_path / "sqlite_vec.db"
    _seed_store(
        store,
        [
            _row("h1", "iris memory one", "iris"),
            _row("h2", "iris memory two", "iris"),
            _row("h3", "quant memory", "quant"),
        ],
    )
    out = tmp_path / "iris.jsonl"

    rc = main(["export", "--wing", "iris", "--store", str(store), "--out", str(out)])
    assert rc == 0

    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    ids = {r["id"] for r in records}
    assert ids == {"h1", "h2"}
    for record in records:
        assert record["wing"] == "iris"
        assert "iris memory" in record["content"]
        assert record["memory_type"] == "observation"
        assert "embedding" not in record
        assert "vector" not in record


def test_export_multiple_wing_tags_resolves_last_wins(tmp_path):
    # A row can carry more than one wing: tag. The live adapter (_parse_namespaced_tag) resolves
    # them LAST-wins; export must agree, or `--wing` would include/omit different rows than the read
    # path (GH-bot B4 P2). Row h1 has wing:first then wing:last → belongs to "last", not "first".
    store = tmp_path / "sqlite_vec.db"
    multi = (
        "h1", "multi-wing memory", "status:active,tenant:t_owner_personal,wing:first,wing:last",
        "{}", None, "observation", 1.0, "2026-01-01T00:00:00Z", 1.0, "2026-01-01T00:00:00Z",
    )
    _seed_store(store, [multi, _row("h2", "plain last memory", "last")])

    out_last = tmp_path / "last.jsonl"
    assert main(["export", "--wing", "last", "--store", str(store), "--out", str(out_last)]) == 0
    last_ids = {json.loads(line)["id"] for line in out_last.read_text().strip().splitlines()}
    assert last_ids == {"h1", "h2"}  # the multi-wing row lands in its LAST wing

    out_first = tmp_path / "first.jsonl"
    assert main(["export", "--wing", "first", "--store", str(store), "--out", str(out_first)]) == 0
    assert out_first.read_text().strip() == ""  # ...and NOT in its first wing


def test_backup_same_basename_sources_do_not_collide(tmp_path, monkeypatch):
    # Two durable files sharing a basename in different dirs must each be captured correctly — the
    # archive keys members by unique role, not basename, so neither snapshot overwrites the other
    # (GH-bot B4 P2). Point the token + audit DBs at same-named files in separate dirs.
    store = tmp_path / "sqlite_vec.db"
    _seed_store(store, [_row("h1", "hello", "iris")])
    monkeypatch.setenv("PANELLA_STORE_PATH", str(store))
    from panella.http.tokens import TokenStore

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    token_db = tmp_path / "a" / "state.db"
    TokenStore(token_db).mint(principal_id=root_principal().id, label="seed")
    monkeypatch.setenv("PANELLA_HTTP_TOKEN_DB", str(token_db))
    audit_db = tmp_path / "b" / "state.db"  # SAME basename, different dir
    audit_write(principal=root_principal(), tenant_accessed="*", op="search", db_path=audit_db)
    monkeypatch.setenv("PANELLA_HTTP_AUDIT_DB", str(audit_db))

    archive = tmp_path / "backup.tar.gz"
    assert main(["backup", "--out", str(archive)]) == 0
    manifest = _read_manifest(archive)
    by_role = {f["role"]: f for f in manifest["files"]}
    # Distinct unique member names (roles), both original basenames preserved as target_name.
    assert by_role["token_db"]["name"] != by_role["audit_db"]["name"]
    assert by_role["token_db"]["target_name"] == "state.db"
    assert by_role["audit_db"]["target_name"] == "state.db"
    # Distinct snapshots (token DB vs audit DB have different content) → no collision/overwrite.
    assert by_role["token_db"]["sha256"] != by_role["audit_db"]["sha256"]

    # And restore into a single flat dir refuses loudly rather than overwriting one with the other.
    dest = tmp_path / "restored"
    rc = main(["restore", "--from", str(archive), "--data-dir", str(dest)])
    assert rc == 2


def test_export_other_wing_rows_absent(tmp_path):
    store = tmp_path / "sqlite_vec.db"
    _seed_store(store, [_row("h1", "iris memory", "iris"), _row("h2", "quant memory", "quant")])
    out = tmp_path / "quant.jsonl"

    assert main(["export", "--wing", "quant", "--store", str(store), "--out", str(out)]) == 0
    records = [json.loads(line) for line in out.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["id"] == "h2"
    assert all(r["wing"] != "iris" for r in records)


def test_export_excludes_deleted_rows(tmp_path):
    store = tmp_path / "sqlite_vec.db"
    _seed_store(
        store,
        [
            _row("h1", "live iris memory", "iris"),
            _row("h2", "deleted iris memory", "iris", deleted_at="2026-01-02T00:00:00Z"),
        ],
    )
    out = tmp_path / "iris.jsonl"
    assert main(["export", "--wing", "iris", "--store", str(store), "--out", str(out)]) == 0
    records = [json.loads(line) for line in out.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["id"] == "h1"


def test_export_works_with_no_running_app(tmp_path, capsys):
    """Offline direct-read contract: export needs nothing but the store file on disk — no HTTP
    server, no adapter, no network. Asserting stdout-only output (no --out) doubles as proof it
    never tried to reach a running app."""
    store = tmp_path / "sqlite_vec.db"
    _seed_store(store, [_row("h1", "solo memory", "iris")])

    capsys.readouterr()
    rc = main(["export", "--wing", "iris", "--store", str(store)])
    assert rc == 0
    captured = capsys.readouterr()
    record = json.loads(captured.out.strip())
    assert record["id"] == "h1"
    assert record["wing"] == "iris"


def test_export_missing_store_is_actionable_not_a_traceback(tmp_path, capsys):
    rc = main(["export", "--wing", "iris", "--store", str(tmp_path / "does-not-exist.db")])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err
