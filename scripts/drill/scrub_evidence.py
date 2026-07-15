#!/usr/bin/env python3
"""Scrub the drill evidence bundle and hard-gate on residual secrets.

Pass 1 replaces every collected secret value (exact bytes) with ``[SCRUBBED:<label>]``.
Pass 2 re-scans the whole bundle: any residual exact match fails the gate. The values are
random hex credentials — pattern scanners cannot be trusted to catch them, so the union of
the real minted values is the only honest gate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def load_union(path: Path) -> list[tuple[str, bytes]]:
    rows: list[tuple[str, bytes]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        label, _, value = line.partition("\t")
        if not value:
            raise SystemExit(f"malformed union row (expected label<TAB>value): {label!r}")
        if len(value) < 8:
            raise SystemExit(f"suspiciously short secret for {label!r}; refusing to scrub with it")
        rows.append((label, value.encode("utf-8")))
    if not rows:
        raise SystemExit("empty secret union — nothing to gate against; refusing to pass vacuously")
    return rows


def bundle_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--secrets", type=Path, required=True, help="union file: label<TAB>value per line")
    args = parser.parse_args()

    union = load_union(args.secrets)
    files = bundle_files(args.evidence_root)
    if not files:
        raise SystemExit(f"no evidence files under {args.evidence_root}")

    replaced = 0
    for path in files:
        data = path.read_bytes()
        original = data
        for label, value in union:
            if value in data:
                data = data.replace(value, f"[SCRUBBED:{label}]".encode())
        if data != original:
            path.write_bytes(data)
            replaced += 1

    residual = []
    for path in bundle_files(args.evidence_root):
        data = path.read_bytes()
        residual.extend((str(path), label) for label, value in union if value in data)

    print(f"scrubbed files: {replaced}/{len(files)}; union size: {len(union)}")
    if residual:
        for path, label in residual:
            print(f"RESIDUAL SECRET: {label} in {path}", file=sys.stderr)
        return 1
    print("gate: zero residual exact matches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
