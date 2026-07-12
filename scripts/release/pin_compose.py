"""Create the digest-pinned compose asset for a Panella release."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path


MANAGED_HEADER = (
    "# managed by panella up — release {version} — do not edit "
    "(hand-edits: use the git-clone path; upgrades: docs/UPGRADE.md)"
)
_TARGET_SERVICES = ("panella", "panella-http")
_EXPECTED_PROPERTIES = ("image", "pull_policy", "build")


def _indentation(line: bytes) -> int:
    return len(line) - len(line.lstrip(b" "))


def _line_ending(line: bytes) -> bytes:
    if line.endswith(b"\r\n"):
        return b"\r\n"
    if line.endswith(b"\n"):
        return b"\n"
    return b""


def _missing_properties(seen: dict[str, dict[str, bool]]) -> list[str]:
    return [
        f"{service}.{property_name}"
        for service in _TARGET_SERVICES
        for property_name in _EXPECTED_PROPERTIES
        if not seen[service][property_name]
    ]


def transform_compose(compose: bytes, *, store_ref: str, app_ref: str) -> bytes:
    """Replace release images and remove local-build settings without parsing YAML."""
    refs = {"panella": store_ref.encode("utf-8"), "panella-http": app_ref.encode("utf-8")}
    seen = {
        service: {property_name: False for property_name in _EXPECTED_PROPERTIES}
        for service in _TARGET_SERVICES
    }
    output: list[bytes] = []
    in_services = False
    current_service: str | None = None
    build_block_indent: int | None = None

    for line in compose.splitlines(keepends=True):
        if build_block_indent is not None:
            # The whole build: block is removed. Blank and comment-only lines inside it carry no
            # YAML content and must not terminate the skip — otherwise a nested key placed after a
            # blank line (e.g. "target:") would leak into the pinned output as a stray top-level
            # service key. Only a real, non-comment line at indent <= the build: key ends the block.
            block_stripped = line.strip()
            if not block_stripped or block_stripped.startswith(b"#"):
                continue
            if _indentation(line) > build_block_indent:
                continue
            build_block_indent = None

        stripped = line.strip()
        indent = _indentation(line)

        if not in_services:
            if indent == 0 and stripped == b"services:":
                in_services = True
        elif stripped and indent == 0:
            in_services = False
            current_service = None
        elif indent == 2 and stripped.endswith(b":"):
            current_service = stripped[:-1].decode("utf-8")

        if current_service in refs and indent == 4:
            # Extraction fidelity with the retired inline transformer: EVERY matching occurrence is
            # replaced/removed (a duplicate key is malformed compose the downstream `config`
            # validation owns; this transform does not add its own rejection semantics).
            if stripped.startswith(b"image:"):
                output.append(b" " * indent + b"image: " + refs[current_service] + _line_ending(line))
                seen[current_service]["image"] = True
                continue
            if stripped.startswith(b"pull_policy:"):
                seen[current_service]["pull_policy"] = True
                continue
            if stripped.startswith(b"build:"):
                seen[current_service]["build"] = True
                build_block_indent = indent
                continue

        output.append(line)

    missing = _missing_properties(seen)
    if missing:
        raise ValueError(f"compose transform did not see expected keys: {', '.join(missing)}")
    return b"".join(output)


def _header(version: str) -> bytes:
    return (MANAGED_HEADER.format(version=version) + "\n").encode("utf-8")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", required=True, type=Path)
    parser.add_argument("--store-ref", required=True)
    parser.add_argument("--app-ref", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--out-compose", required=True, type=Path)
    parser.add_argument("--out-digests", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    run_id = int(args.run_id)
    run_attempt = int(args.run_attempt)
    final_compose = _header(args.version) + transform_compose(
        args.compose.read_bytes(), store_ref=args.store_ref, app_ref=args.app_ref
    )
    if final_compose.splitlines(keepends=True)[:1] != [_header(args.version)]:
        raise AssertionError("managed header must be line 1")

    args.out_compose.write_bytes(final_compose)
    digests = {
        "schema": 1,
        "version": args.version,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "event": args.event,
        "ref": args.ref,
        "head_sha": args.head_sha,
        "store_ref": args.store_ref,
        "app_ref": args.app_ref,
        "compose_sha256": hashlib.sha256(final_compose).hexdigest(),
    }
    args.out_digests.write_text(json.dumps(digests, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
