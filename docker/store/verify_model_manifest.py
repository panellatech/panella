#!/usr/bin/env python3
"""Verify the exact, pinned files in Panella's baked Hugging Face snapshot."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys


def fail(message: str) -> None:
    print(f"model manifest verification failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def manifest_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            digest, relative = line.split("  ", 1)
        except ValueError:
            fail(f"invalid manifest entry: {raw}")
        if len(digest) != 64 or relative in entries:
            fail(f"invalid or duplicate manifest entry: {raw}")
        entries[relative] = digest
    if not entries:
        fail("manifest has no entries")
    return entries


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("hf_home", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()

    repo_dir = args.hf_home / "hub" / f"models--{args.repo.replace('/', '--')}"
    snapshots = repo_dir / "snapshots"
    if not repo_dir.is_dir() or not snapshots.is_dir():
        fail(f"missing repository cache directory: {repo_dir}")
    snapshot_entries = [entry for entry in snapshots.iterdir()]
    if len(snapshot_entries) != 1 or snapshot_entries[0].name != args.revision:
        fail(f"snapshots must contain only revision {args.revision}: {snapshots}")
    snapshot = snapshot_entries[0]
    if not snapshot.is_dir():
        fail(f"snapshot is not a directory: {snapshot}")

    expected = manifest_entries(args.manifest)
    actual = {
        item.relative_to(snapshot).as_posix()
        for item in snapshot.rglob("*")
        if item.is_file()
    }
    extras = sorted(actual - set(expected))
    missing = sorted(set(expected) - actual)
    if extras:
        fail(f"unexpected snapshot file: {extras[0]}")
    if missing:
        fail(f"missing snapshot file: {missing[0]}")
    for relative, digest in expected.items():
        actual_digest = file_sha256(snapshot / relative)
        if actual_digest != digest:
            fail(f"sha256 mismatch: {relative}")
    print(f"model manifest verified: {snapshot}", file=sys.stderr)


if __name__ == "__main__":
    main()
