#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path


# hatchling force-includes a root .gitignore into the sdist even with an explicit include allowlist,
# and a .gitignore is a benign, conventional source-distribution file (it never reaches the wheel).
# So it is NOT forbidden. The forbidden set below stays focused on secrets/local-machine leakage and
# genuinely-unshipped trees (.env, CI config, tests, caches).
FORBIDDEN_PARTS = {
    ".env",
    ".github",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "tests",
}

FORBIDDEN_SUBSTRINGS = (
    "/Users/",
    "/home/",
    "\\Users\\",
)


def fail(message: str) -> None:
    print(f"wheel_contents=fail {message}", file=sys.stderr)
    raise SystemExit(1)


def reject_common(name: str) -> None:
    path = name.rstrip("/")
    if not path:
        fail("archive contains empty path")
    if path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", path):
        fail(f"archive contains absolute/local path: {name}")
    parts = [part for part in path.split("/") if part]
    if any(part in FORBIDDEN_PARTS for part in parts):
        fail(f"archive contains forbidden path: {name}")
    if any(fragment in path for fragment in FORBIDDEN_SUBSTRINGS):
        fail(f"archive contains local path fragment: {name}")


def check_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
    dist_info_re = re.compile(r"^panella-[^/]+\.dist-info/")
    for name in names:
        reject_common(name)
        if name.endswith("/"):
            continue
        if name.startswith(("panella/", "panella_selfhost/", "config/")):
            continue
        if dist_info_re.match(name):
            continue
        fail(f"wheel contains unexpected file: {name}")
    print(f"wheel_contents: checked wheel {path} ({len(names)} entries)")


def check_sdist(path: Path) -> None:
    with tarfile.open(path) as sdist:
        names = sdist.getnames()
    if not names:
        fail(f"sdist is empty: {path}")
    roots = {name.split("/", 1)[0] for name in names if name}
    if len(roots) != 1:
        fail(f"sdist must have one archive root, got: {sorted(roots)}")
    root = next(iter(roots))
    metadata_files = {
        f"{root}/LICENSE",
        # NOTICE ships in the sdist for Apache-2.0 attribution compliance (hatchling includes it
        # like LICENSE); tolerate it as a metadata file.
        f"{root}/NOTICE",
        f"{root}/PKG-INFO",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
        # hatchling force-includes a root .gitignore into every sdist (VCS integration) even with an
        # explicit include allowlist; it is benign and never reaches the wheel, so tolerate it here.
        f"{root}/.gitignore",
    }
    for name in names:
        reject_common(name)
        if name == root or name.endswith("/"):
            continue
        if name.startswith((f"{root}/panella/", f"{root}/panella_selfhost/", f"{root}/config/")):
            continue
        if name in metadata_files:
            continue
        fail(f"sdist contains unexpected file: {name}")
    print(f"wheel_contents: checked sdist {path} ({len(names)} entries)")


def main() -> int:
    dist_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1:
        fail(f"expected exactly one wheel under {dist_dir}, found {len(wheels)}")
    if len(sdists) != 1:
        fail(f"expected exactly one sdist under {dist_dir}, found {len(sdists)}")
    check_wheel(wheels[0])
    check_sdist(sdists[0])
    print("wheel_contents=pass artifacts contain only intended package/config files and metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
