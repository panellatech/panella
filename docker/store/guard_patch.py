#!/usr/bin/env python3
"""Apply and prove Panella's source-level hash-embedding fallback guard."""

from __future__ import annotations

import argparse
import base64
import compileall
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import py_compile
import subprocess
import sys
from typing import Final


PACKAGE: Final = "mcp-memory-service"
VERSION: Final = "10.67.1"
MARKER: Final = "PANELLA: refusing pure-Python hash-embedding fallback"
ANCHOR: Final = "    async def _initialize_hash_embedding_fallback(self):\n"
GUARD: Final = '''        if os.environ.get("PANELLA_REQUIRE_REAL_EMBEDDINGS", "1") != "0":
            raise RuntimeError(
                "PANELLA: refusing pure-Python hash-embedding fallback "
                "(real embedding backend required; set PANELLA_REQUIRE_REAL_EMBEDDINGS=0 "
                "for explicit degraded mode)"
            )
'''
PROVENANCE: Final = Path("/usr/local/share/panella/guard-patch-provenance.json")


def fail(message: str) -> None:
    raise RuntimeError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_source() -> Path:
    spec = importlib.util.find_spec("mcp_memory_service.storage.sqlite_vec")
    if spec is None or spec.origin is None:
        fail("could not locate mcp_memory_service.storage.sqlite_vec")
    return Path(spec.origin)


def update_record(source: Path, patched: bytes) -> None:
    distribution = importlib.metadata.distribution(PACKAGE)
    record_file = next(
        (
            item
            for item in distribution.files or ()
            if item.as_posix().endswith(".dist-info/RECORD")
        ),
        None,
    )
    if record_file is None:
        fail("could not locate wheel RECORD")
    record_path = Path(distribution.locate_file(record_file))
    site_packages = record_path.parent.parent
    try:
        source_name = source.relative_to(site_packages).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"patched source is outside site-packages: {source}") from exc

    with record_path.open(newline="") as handle:
        rows = list(csv.reader(handle))
    digest = base64.urlsafe_b64encode(hashlib.sha256(patched).digest()).decode().rstrip("=")
    replacements = 0
    for row in rows:
        if row and row[0] == source_name:
            row[1] = f"sha256={digest}"
            row[2] = str(len(patched))
            replacements += 1
    if replacements != 1:
        fail(f"expected exactly one RECORD entry for {source_name}, found {replacements}")
    with record_path.open("w", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(rows)


def recompile(source: Path) -> None:
    pycache = source.parent / "__pycache__"
    for stale in pycache.glob("sqlite_vec.*.pyc") if pycache.exists() else ():
        stale.unlink()
    ok = compileall.compile_file(
        str(source),
        force=True,
        quiet=1,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    if not ok:
        fail(f"compileall failed for {source}")


def child_code(require_real: bool) -> str:
    expectation = '''\
try:
    asyncio.run(coro)
except RuntimeError as exc:
    assert "PANELLA: refusing pure-Python hash-embedding fallback" in str(exc), str(exc)
else:
    raise AssertionError("guard did not raise RuntimeError")
''' if require_real else '''\
asyncio.run(coro)
assert isinstance(stub.embedding_model, m._HashEmbeddingModel), type(stub.embedding_model)
assert stub.embedding_dimension == 384, stub.embedding_dimension
'''
    return f'''\
import asyncio
from mcp_memory_service.storage import sqlite_vec as m
stub = m.SqliteVecMemoryStorage.__new__(m.SqliteVecMemoryStorage)
stub.conn = None; stub.embedding_dimension = 384
coro = m.SqliteVecMemoryStorage.__dict__["_initialize_hash_embedding_fallback"](stub)
{expectation}'''


def verify_behavior() -> None:
    for require_real in (True, False):
        env = dict(__import__("os").environ)
        env["PANELLA_REQUIRE_REAL_EMBEDDINGS"] = "1" if require_real else "0"
        result = subprocess.run(
            [sys.executable, "-c", child_code(require_real)],
            env=env,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            mode = env["PANELLA_REQUIRE_REAL_EMBEDDINGS"]
            fail(
                f"fresh-child verification failed for PANELLA_REQUIRE_REAL_EMBEDDINGS={mode}:\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if not (args.apply and args.verify):
        fail("--apply and --verify are required together")

    # This version check deliberately precedes every source/package operation.
    installed_version = importlib.metadata.version(PACKAGE)
    if installed_version != VERSION:
        fail(f"expected {PACKAGE}=={VERSION}, found {installed_version}")

    source = package_source()
    upstream = source.read_bytes()
    if upstream.count(ANCHOR.encode()) != 1:
        fail(f"expected anchor exactly once in {source}")
    if MARKER.encode() in upstream:
        fail("guard marker already present; refusing a non-idempotent second patch")

    upstream_sha256 = sha256(upstream)
    patched = upstream.replace(ANCHOR.encode(), ANCHOR.encode() + GUARD.encode(), 1)
    source.write_bytes(patched)
    patched_sha256 = sha256(patched)
    recompile(source)
    update_record(source, patched)
    PROVENANCE.write_text(
        json.dumps(
            {
                "package": PACKAGE,
                "version": VERSION,
                "file": str(source),
                "anchor": ANCHOR.rstrip("\n"),
                "upstream_sha256": upstream_sha256,
                "patched_sha256": patched_sha256,
                "marker": MARKER,
            },
            sort_keys=True,
        )
        + "\n"
    )
    verify_behavior()
    print(f"guard patch verified: {source}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"guard patch failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
