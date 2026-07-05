#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


ALLOWED_MARKERS = (
    "apache",
    "mit",
    "bsd",
    "python software foundation",
    "psf",
    "isc",
)

INCOMPATIBLE_MARKERS = (
    "agpl",
    "gpl",
    "lgpl",
    "sspl",
    "epl",
    "cddl",
    "proprietary",
    "commercial",
    "unknown",
)

# Reviewed exception kept intentionally small and generic. certifi is a transitive TLS CA-bundle
# dependency commonly reported as MPL-2.0; it is retained unmodified and is not a strong-copyleft
# code dependency.
REVIEWED_COMPATIBLE = {
    "certifi": "MPL-2.0 CA bundle, retained unmodified for TLS trust roots",
    # MPL-2.0 is file-level (weak) copyleft: shipping it as an UNMODIFIED transitive dependency is
    # compatible with an Apache-2.0 product. fqdn is a jsonschema format-checker pulled in via the
    # MCP SDK, retained unmodified. New/unknown licenses still fail the gate for human review.
    "fqdn": "MPL-2.0 jsonschema format checker (via MCP SDK), retained unmodified",
}


def run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=True, text=True, capture_output=capture)
    except FileNotFoundError:
        print(f"license_scan=fail missing command: {args[0]}", file=sys.stderr)
        raise SystemExit(127) from None
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        print(f"license_scan=fail command failed: {' '.join(args)}", file=sys.stderr)
        raise SystemExit(exc.returncode) from None


def normalized(value: str) -> str:
    return " ".join(value.lower().replace("_", "-").split())


def contains_any(value: str, markers: Iterable[str]) -> bool:
    return any(marker in value for marker in markers)


def is_permissive_license(license_name: str) -> bool:
    norm = normalized(license_name)
    return bool(norm) and contains_any(norm, ALLOWED_MARKERS) and not contains_any(norm, INCOMPATIBLE_MARKERS)


def is_incompatible_license(license_name: str) -> bool:
    return contains_any(normalized(license_name), INCOMPATIBLE_MARKERS)


def _requirement_names(path: Path) -> set[str]:
    """Normalized package names (lowercase, ``_``->``-``) from a pip-freeze / requirements file, so
    the license scan evaluates ONLY panella's shipped runtime tree — NOT the scanner tooling
    (pip-licenses + its own deps) that must live in the same env to run at all. Mirrors the
    isolation pip-audit already gets from its frozen ``-r`` list."""
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = re.split(r"[<>=!~;\[ @]", line, maxsplit=1)[0].strip()
        if token:
            names.add(token.lower().replace("_", "-"))
    return names


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requirements",
        type=Path,
        default=None,
        help="Evaluate ONLY the packages listed here (the shipped runtime tree); skip scanner tooling.",
    )
    args = parser.parse_args()
    allowed = _requirement_names(args.requirements) if args.requirements else None

    table_args = [
        "pip-licenses",
        "--from=mixed",
        "--format=markdown",
        "--with-authors",
        "--with-urls",
    ]
    json_args = [
        "pip-licenses",
        "--from=mixed",
        "--format=json",
        "--with-authors",
        "--with-urls",
    ]

    print("license_scan: installed dependency license table")
    run(table_args)

    result = run(json_args, capture=True)
    packages = json.loads(result.stdout)

    failures: list[str] = []
    reviewed: list[str] = []
    for package in packages:
        name = str(package.get("Name", "")).strip()
        license_name = str(package.get("License", "")).strip()
        name_key = name.lower().replace("_", "-")
        if allowed is not None and name_key not in allowed:
            continue  # scanner tooling / not part of panella's shipped runtime tree
        if is_permissive_license(license_name):
            continue
        if name_key in REVIEWED_COMPATIBLE and not is_incompatible_license(license_name):
            reviewed.append(f"{name} ({license_name}): {REVIEWED_COMPATIBLE[name_key]}")
            continue
        failures.append(f"{name} ({license_name or 'NO LICENSE METADATA'})")

    if reviewed:
        print("license_scan: reviewed compatible non-allowlist packages")
        for item in reviewed:
            print(f"  {item}")

    if failures:
        print("license_scan=fail non-allowlisted or incompatible dependency licenses:", file=sys.stderr)
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1

    print("license_scan=pass all dependency licenses are allowlisted or reviewed compatible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
