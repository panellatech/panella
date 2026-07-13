#!/usr/bin/env python3
"""Assert that the release distributions embed the validated pinned-compose bundle."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tarfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

ASSET_SUFFIX = "panella_selfhost/_assets/compose.pinned.yml"
# Exact member locations, not endswith: a stray nested copy (e.g. under another package dir) must
# not satisfy the assertion while importlib.resources loads nothing at runtime.
_SDIST_MEMBER_RE = re.compile(r"^[^/]+/" + re.escape(ASSET_SUFFIX) + r"$")
_REF_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_KEYS = ("schema", "version", "run_id", "run_attempt", "event", "ref", "head_sha", "store_ref", "app_ref", "compose_sha256")
_STRING_KEYS = ("version", "event", "ref", "head_sha", "store_ref", "app_ref", "compose_sha256")


class AssetError(ValueError):
    """A distribution asset failed the release integrity contract."""


def _wheel_asset(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        matches = [name for name in archive.namelist() if name == ASSET_SUFFIX]
        if len(matches) != 1:
            raise AssetError(f"expected exactly one wheel member {ASSET_SUFFIX}, found {len(matches)}")
        return archive.read(matches[0])


def _sdist_asset(path: Path) -> bytes:
    with tarfile.open(path) as archive:
        matches = [name for name in archive.getnames() if _SDIST_MEMBER_RE.fullmatch(name)]
        if len(matches) != 1:
            raise AssetError(f"expected exactly one sdist member <root>/{ASSET_SUFFIX}, found {len(matches)}")
        file = archive.extractfile(matches[0])
        if file is None:
            raise AssetError("sdist compose asset is not a regular file")
        return file.read()


def validate_digests(digests: Mapping[str, Any]) -> None:
    if tuple(digests) != _KEYS:
        raise AssetError("digests schema keys or insertion order do not match schema v1")
    for key in _STRING_KEYS:
        if not isinstance(digests[key], str) or not digests[key]:
            raise AssetError(f"digests.{key} must be a non-empty string")
    for key in ("schema", "run_id", "run_attempt"):
        if not isinstance(digests[key], int) or isinstance(digests[key], bool):
            raise AssetError(f"digests.{key} must be an integer")
    if digests["schema"] != 1:
        raise AssetError("digests.schema must be 1")
    for key in ("store_ref", "app_ref"):
        if not _REF_RE.fullmatch(digests[key]):
            raise AssetError(f"digests.{key} must be an image@sha256:<64hex> reference")
    if not re.fullmatch(r"[0-9a-f]{64}", digests["compose_sha256"]):
        raise AssetError("digests.compose_sha256 must be lowercase sha256")


def validate_compose(compose: bytes, digests: Mapping[str, Any]) -> None:
    if hashlib.sha256(compose).hexdigest() != digests["compose_sha256"]:
        raise AssetError("embedded compose sha256 does not match digests.json")
    document = yaml.safe_load(compose)
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise AssetError("embedded compose has no services mapping")
    services = document["services"]
    target_images: list[tuple[str, Any]] = []
    for name, service in services.items():
        if isinstance(service, dict) and "image" in service:
            target_images.append((str(name), service["image"]))
    if len(target_images) != 2:
        raise AssetError("embedded compose must contain exactly two service image lines")
    for service, digest_key in (("panella", "store_ref"), ("panella-http", "app_ref")):
        expected = digests[digest_key]
        value = services.get(service, {}).get("image") if isinstance(services.get(service), dict) else None
        if value != expected:
            raise AssetError(f"embedded compose {service}.image does not match digests.{digest_key}")


def assert_release_asset(dist: Path, digests: Mapping[str, Any]) -> None:
    validate_digests(digests)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AssetError("dist must contain exactly one wheel and one sdist")
    wheel_asset = _wheel_asset(wheels[0])
    sdist_asset = _sdist_asset(sdists[0])
    if wheel_asset != sdist_asset:
        raise AssetError("wheel and sdist embedded compose assets differ")
    validate_compose(wheel_asset, digests)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print("usage: assert-release-asset.py DIST_DIR DIGESTS_JSON", file=sys.stderr)
        return 2
    try:
        digests = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        if not isinstance(digests, dict):
            raise AssetError("digests JSON must be an object")
        assert_release_asset(Path(args[0]), digests)
    except (OSError, json.JSONDecodeError, AssetError, yaml.YAMLError) as exc:
        print(f"assert-release-asset: {exc}", file=sys.stderr)
        return 1
    print("release_asset=pass embedded compose matches validated release digests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
