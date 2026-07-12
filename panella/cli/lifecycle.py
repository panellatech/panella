"""``panella backup`` / ``restore`` / ``export`` — the durable-state lifecycle.

A self-host operator's only durable state is a handful of SQLite files: the store (memories),
the facade's own token/audit/outbox DBs, and (if configured) a governance overlay file. This
module gives that operator three commands to not lose it:

- ``backup``  — consistent, validated snapshot packaged as one ``tar.gz`` + ``MANIFEST.json``.
- ``restore`` — hash-verified, atomic placement of a backup's files back onto disk.
- ``export``  — an OFFLINE, read-only JSONL dump of one wing's memories (works with the box down).

Consistency guarantees (see MANIFEST + tests):
  1. Every SQLite file is snapshotted via the SQLite **backup API** (``sqlite3.Connection.backup``),
     never a raw file copy — a raw copy of a live WAL-mode DB can capture a torn, inconsistent
     image; the backup API is always transactionally consistent.
  2. ``restore`` verifies every file's sha256 against the manifest BEFORE placing anything, and
     places atomically (write to temp names in the target dir, then ``os.replace``).
  3. ``export`` opens the store with a read-only (``mode=ro``) URI — it never needs the box running
     and never writes to the store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import zlib
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MANIFEST_NAME = "MANIFEST.json"
MANIFEST_FORMAT_VERSION = 1
# Files placed by restore that hold sensitive/durable state and should never be group/world
# readable — mirrors the 0600 convention panella.audit / panella.http.tokens already apply to
# their own DBs (audit.py:_ensure schema, http/tokens.py TokenStore.__init__).
# Every restored role is sensitive: token/audit/outbox hold bearer tokens + the audit chain, and
# store_db holds the actual memory CONTENTS — a restored store left at the umask default (0644) would
# make private memories group/world-readable to other local users or containers sharing the dir
# (GH-bot B4 P2). All restored files land 0600.
_SENSITIVE_ROLES = {"store_db", "token_db", "audit_db", "outbox_db", "governance_overlay"}


def register(subparsers: argparse._SubParsersAction) -> None:
    backup = subparsers.add_parser("backup", help="Snapshot durable state into one tar.gz.")
    backup.add_argument("--out", required=True, type=Path, help="Output archive path (.tar.gz).")
    backup.add_argument("--force", action="store_true", help="Overwrite --out if it already exists.")
    backup.set_defaults(func=_cmd_backup)

    restore = subparsers.add_parser("restore", help="Restore durable state from a backup archive.")
    restore.add_argument("--from", dest="from_path", required=True, type=Path, help="Backup archive path.")
    restore.add_argument("--data-dir", required=True, type=Path, help="Directory to restore files into.")
    restore.add_argument("--force", action="store_true", help="Overwrite existing target files.")
    restore.set_defaults(func=_cmd_restore)

    export = subparsers.add_parser("export", help="Offline JSONL export of one wing's memories.")
    export.add_argument("--wing", required=True, help="Wing to export.")
    export.add_argument("--out", type=Path, default=None, help="Output .jsonl path (default: stdout).")
    export.add_argument("--store", type=Path, default=None, help="Store DB path override.")
    export.set_defaults(func=_cmd_export)


# --------------------------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _SourceFile:
    role: str
    path: Path


def _panella_version() -> str:
    """The installed package version, with a dev fallback to the pyproject value so a
    ``pip install -e`` checkout (no wheel metadata yet published) still stamps something
    meaningful instead of raising."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("panella")
    except Exception:  # noqa: BLE001 — any resolution failure falls through to the dev fallback
        pass
    root = Path(__file__).resolve().parents[2]
    pyproject = root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                # `version = "0.2.0"` — split on '=' then strip quotes/whitespace.
                _, _, rhs = stripped.partition("=")
                return rhs.strip().strip('"').strip("'")
    except OSError:
        pass
    return "0.0.0-dev"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup_to(src: Path, dst: Path) -> None:
    """Consistent snapshot of a (possibly live, WAL-mode) SQLite file via the backup API —
    NEVER a raw ``shutil.copy`` of a live DB, which can capture a torn/inconsistent image
    mid-write. Safe to call against a file with no ``-wal``/``-shm`` sidecars too."""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _collect_source_files() -> list[_SourceFile]:
    """The durable files this deployment actually has, in manifest order. Each is included only
    if it exists on disk — a fresh box with no outbox yet, or no configured governance overlay,
    backs up exactly what it has."""
    from panella.governance import resolve_overlay_path
    from panella.http.config import load_config
    from panella.store_probe import resolve_store_path

    config = load_config(None)
    candidates = [
        _SourceFile("store_db", resolve_store_path()),
        _SourceFile("token_db", Path(config.token_db_path)),
        _SourceFile("audit_db", Path(config.audit_db_path)),
        _SourceFile("outbox_db", Path(config.outbox_db_path)),
    ]
    overlay = resolve_overlay_path()
    if overlay is not None:
        candidates.append(_SourceFile("governance_overlay", overlay))
    return [c for c in candidates if c.path.exists()]


def _validate_snapshot(role: str, path: Path) -> str | None:
    """Post-snapshot / pre-restore validation for the two roles that have a real integrity check.
    Returns None on pass, else a human-readable failure reason."""
    if role == "store_db":
        from panella.store_probe import startup_self_check

        result = startup_self_check(path)
        if not result.serving:
            return f"store_probe refused the snapshot: {result.reason}"
        return None
    if role == "audit_db":
        from panella.audit import AuditChainError, audit_verify_chain

        try:
            audit_verify_chain(path)
        except AuditChainError as exc:
            return f"audit hash-chain verify failed: {exc}"
        return None
    return None


def _cmd_backup(args: argparse.Namespace) -> int:
    out_path: Path = args.out
    if out_path.exists() and not args.force:
        print(f"{out_path} already exists — pass --force to overwrite.", file=sys.stderr)
        return 2

    sources = _collect_source_files()
    if not sources:
        print("nothing to back up — no store/token/audit/outbox files found on this box.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="panella-backup-") as tmp_name:
        tmp_dir = Path(tmp_name)
        manifest_files: list[dict[str, Any]] = []
        for source in sources:
            # The archive MEMBER name is the role (unique per source), NOT the basename: two
            # configured durable files can share a basename (e.g. token + audit DBs both named
            # state.db in different dirs), which would collide in tmp_dir and silently overwrite one
            # snapshot with another → an unrestorable or wrong backup (GH-bot B4 P2). ``target_name``
            # carries the original basename so restore places each file back under its real name.
            snapshot_path = tmp_dir / source.role
            if source.role == "governance_overlay":
                # A plain YAML file — copy2 preserves mtime/mode; the SQLite backup API does not
                # apply here (it is not a database).
                shutil.copy2(source.path, snapshot_path)
            else:
                _sqlite_backup_to(source.path, snapshot_path)
                failure = _validate_snapshot(source.role, snapshot_path)
                if failure:
                    print(
                        f"backup aborted: {source.role} snapshot failed validation: {failure}",
                        file=sys.stderr,
                    )
                    return 1
            manifest_files.append(
                {
                    "name": source.role,
                    "target_name": source.path.name,
                    "sha256": _sha256_file(snapshot_path),
                    "role": source.role,
                    "size": snapshot_path.stat().st_size,
                }
            )

        manifest = {
            "format_version": MANIFEST_FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "files": manifest_files,
            "panella_version": _panella_version(),
        }
        manifest_path = tmp_dir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Build the archive at a temp path in the SAME directory as --out, then atomically rename
        # into place — a crash mid-tar never leaves a half-written file at the requested --out path.
        # The archive bundles bearer tokens + the store's memory contents, so create it 0600 (not the
        # umask default 0644): on a multi-user host or a shared backup dir, other local readers must
        # not be able to extract tokens/private memories (GH-bot B4 P2).
        tmp_archive = out_path.with_suffix(out_path.suffix + ".tmp")
        fd = os.open(tmp_archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        os.chmod(tmp_archive, 0o600)  # umask-proof the fresh file
        with tarfile.open(tmp_archive, "w:gz") as tar:
            for manifest_file in manifest_files:
                tar.add(tmp_dir / manifest_file["name"], arcname=manifest_file["name"])
            tar.add(manifest_path, arcname=MANIFEST_NAME)
        os.replace(tmp_archive, out_path)

    size = out_path.stat().st_size
    print(f"backup written: {out_path} ({size} bytes)")
    for manifest_file in manifest_files:
        print(f"  {manifest_file['role']:<20} {manifest_file['target_name']} ({manifest_file['size']} bytes)")
    return 0


# --------------------------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------------------------- #


def _load_manifest(archive: tarfile.TarFile) -> dict[str, Any]:
    member = archive.getmember(MANIFEST_NAME)
    fh = archive.extractfile(member)
    if fh is None:
        raise ValueError(f"{MANIFEST_NAME} is not a regular file in the archive")
    return json.loads(fh.read().decode("utf-8"))


def _cmd_restore(args: argparse.Namespace) -> int:
    archive_path: Path = args.from_path
    data_dir: Path = args.data_dir

    if not archive_path.exists():
        print(f"backup archive not found: {archive_path}", file=sys.stderr)
        return 2

    with ExitStack() as stack:
        try:
            tar = stack.enter_context(tarfile.open(archive_path, "r:gz"))
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            print(f"invalid backup archive: {exc}", file=sys.stderr)
            return 1
        try:
            manifest = _load_manifest(tar)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"invalid backup archive: {exc}", file=sys.stderr)
            return 1
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            # tarfile.open is LAZY: a valid gzip truncated near its tail opens cleanly, then raises
            # here while reading/indexing the MANIFEST member. Corrupt gzip *data* (a flipped byte
            # mid-stream) raises zlib.error, which is NOT an OSError/TarError subclass — catch it too
            # so every read-time failure is the same clean error, never a traceback.
            print(f"invalid backup archive: {exc}", file=sys.stderr)
            return 1

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            print("invalid backup archive: MANIFEST.json has no files entry", file=sys.stderr)
            return 1

        manifest_version = manifest.get("panella_version")
        current_version = _panella_version()
        if manifest_version and manifest_version != current_version:
            print(
                f"WARNING: backup was made by panella {manifest_version}, this box runs "
                f"{current_version} — the store schema is upstream-owned, so this is not "
                "necessarily a problem, but verify after restore.",
                file=sys.stderr,
            )

        targets: list[tuple[dict[str, Any], Path]] = []
        for entry in files:
            name = entry.get("name")
            # ``name`` is the tar member (unique role); ``target_name`` is the original basename the
            # file is restored under (fallback to name for a manifest written before the split). Both
            # must be single-segment — no traversal into or out of data_dir.
            target_name = entry.get("target_name", name)
            for candidate in (name, target_name):
                if not isinstance(candidate, str) or not candidate or "/" in candidate or candidate in {".", ".."}:
                    print(f"invalid backup archive: unsafe file name in manifest: {candidate!r}", file=sys.stderr)
                    return 1
            targets.append((entry, data_dir / target_name))

        # Two backed-up files can share a basename (different source dirs). The archive holds both
        # (unique role members), but a flat --data-dir cannot — placing both would silently overwrite
        # one with the other. Refuse loudly instead of restoring wrong data.
        seen: dict[Path, str] = {}
        for entry, target in targets:
            if target in seen:
                print(
                    f"restore refused — the {seen[target]} and {entry.get('role')} files share the "
                    f"basename {target.name!r}, which a single --data-dir cannot hold both of; restore "
                    "into separate directories (one --from per role) or rename the sources.",
                    file=sys.stderr,
                )
                return 2
            seen[target] = str(entry.get("role"))

        existing = [str(target) for _entry, target in targets if target.exists()]
        if existing and not args.force:
            print(
                "restore refused — the following target files already exist "
                f"(pass --force to overwrite): {', '.join(existing)}",
                file=sys.stderr,
            )
            return 2
        if existing:
            print(f"--force set: overwriting {', '.join(existing)}")

        # Verify EVERY file's sha256 against the manifest BEFORE placing anything — a corrupt
        # snapshot inside the tar must never land on disk, not even partially.
        with tempfile.TemporaryDirectory(prefix="panella-restore-") as tmp_name:
            tmp_dir = Path(tmp_name)
            extracted: list[tuple[dict[str, Any], Path, Path]] = []
            for entry, target in targets:
                name = entry["name"]
                try:
                    member = tar.getmember(name)
                except KeyError:
                    print(f"backup archive is missing manifest-listed file: {name}", file=sys.stderr)
                    return 1
                staged = tmp_dir / name
                try:
                    fh = tar.extractfile(member)
                    if fh is None:
                        print(f"manifest-listed file is not a regular file in the archive: {name}", file=sys.stderr)
                        return 1
                    with staged.open("wb") as out_fh:
                        shutil.copyfileobj(fh, out_fh)
                except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
                    # A tail-truncated archive can index + open a member but fail mid-read here; a
                    # mid-stream data corruption raises zlib.error (not an OSError subclass). Surface
                    # either as the clean error instead of a traceback (same as the manifest guard).
                    print(f"invalid backup archive: {exc}", file=sys.stderr)
                    return 1
                actual_hash = _sha256_file(staged)
                expected_hash = entry.get("sha256")
                if actual_hash != expected_hash:
                    print(
                        f"restore refused BEFORE placing any files — {name} sha256 mismatch "
                        f"(expected {expected_hash}, got {actual_hash}); the archive is corrupt "
                        "or was tampered with.",
                        file=sys.stderr,
                    )
                    return 1
                extracted.append((entry, staged, target))

            # All hashes verified — place atomically (temp name in the target dir, then rename).
            data_dir.mkdir(parents=True, exist_ok=True)
            for entry, staged, target in extracted:
                placing_tmp = target.with_name(target.name + ".restoring")
                shutil.copy2(staged, placing_tmp)
                if entry.get("role") in _SENSITIVE_ROLES:
                    os.chmod(placing_tmp, 0o600)
                os.replace(placing_tmp, target)

    print(f"restored {len(targets)} file(s) into {data_dir}")
    return _post_restore_verify(data_dir, files)


def _post_restore_verify(data_dir: Path, files: list[dict[str, Any]]) -> int:
    """Re-run the same integrity checks backup used, against the just-placed files. Prints one
    pass/fail line per checked role; returns nonzero if any check fails."""
    ok = True
    for entry in files:
        role = entry.get("role")
        # Files were RESTORED under target_name (the original basename, e.g. sqlite_vec.db), NOT the
        # archive member/role name (store_db). Validate the file that was actually placed — using the
        # role name here would probe a nonexistent path (store) or a fresh empty DB (audit) and
        # mis-report a successful restore (GH-bot B4 P2, introduced by the unique-member-name fix).
        target_name = entry.get("target_name", entry.get("name"))
        if role not in {"store_db", "audit_db"}:
            continue
        target = data_dir / str(target_name)
        failure = _validate_snapshot(str(role), target)
        if failure:
            print(f"  FAIL  {role}: {failure}")
            ok = False
        else:
            print(f"  PASS  {role}: {target}")
    if not ok:
        print("post-restore verification FAILED — do not trust this data-dir yet.", file=sys.stderr)
        return 1
    print("post-restore verification passed. Next: restart the stack (docker compose up -d).")
    return 0


# --------------------------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------------------------- #


_EXPORT_QUERY = (
    "SELECT content_hash, content, tags, metadata, memory_type, created_at, created_at_iso, "
    "updated_at, updated_at_iso "
    "FROM memories WHERE deleted_at IS NULL"
)


def _open_readonly_store(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"store not found: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout = 4000")
    return conn


def _row_wing(tags: str | None, metadata: dict[str, Any]) -> str:
    """Metadata wins over tag — mirrors panella_adapter._normalize_hit's read-side precedence
    EXACTLY, so export agrees with what the live read path would call this row's wing."""
    meta_wing = metadata.get("wing")
    if isinstance(meta_wing, str) and meta_wing:
        return meta_wing
    # A row can carry multiple wing: tags; the live adapter (_parse_namespaced_tag) resolves them
    # LAST-wins, not first. Export MUST match, or `panella export --wing X` would include/omit
    # different rows than the read path classifies for that wing (GH-bot B4 P2). Scan ALL wing tags
    # and keep the last non-empty value.
    wing = None
    for tag in (tags or "").replace(" ", "").split(","):
        if tag.startswith("wing:"):
            value = tag[len("wing:"):]
            if value:
                wing = value
    if wing is not None:
        return wing
    return "knowledge"  # LEGACY_FALLBACK_WING (panella_adapter.py) — no hard import (fence-light).


def _cmd_export(args: argparse.Namespace) -> int:
    if args.store is not None:
        store_path = args.store
    else:
        from panella.store_probe import resolve_store_path

        store_path = resolve_store_path()

    try:
        conn = _open_readonly_store(store_path)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        rows = conn.execute(_EXPORT_QUERY).fetchall()
    finally:
        conn.close()

    out_lines: list[str] = []
    for (
        content_hash,
        content,
        tags,
        metadata_json,
        memory_type,
        created_at,
        created_at_iso,
        updated_at,
        updated_at_iso,
    ) in rows:
        metadata: dict[str, Any] = {}
        if metadata_json:
            try:
                parsed = json.loads(metadata_json)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (TypeError, ValueError):
                metadata = {}
        if _row_wing(tags, metadata) != args.wing:
            continue
        tag_list = [t for t in (tags or "").split(",") if t]
        record = {
            "id": content_hash,
            "content": content,
            "wing": args.wing,
            "tags": tag_list,
            "metadata": metadata,
            "memory_type": memory_type,
            "created_at": created_at,
            "created_at_iso": created_at_iso,
            "updated_at": updated_at,
            "updated_at_iso": updated_at_iso,
        }
        out_lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))

    text = "\n".join(out_lines) + ("\n" if out_lines else "")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        # The export is raw memory contents + metadata — write it 0600, not the umask default
        # (Path.write_text would leave 0644), so exported private memories aren't readable by other
        # local users / shared-volume consumers (GH-bot B4 P2). Fresh 0600 fd + chmod, umask-proof.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, text.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(args.out, 0o600)
        print(f"exported {len(out_lines)} memor{'y' if len(out_lines) == 1 else 'ies'} to {args.out}")
    else:
        sys.stdout.write(text)
    return 0
