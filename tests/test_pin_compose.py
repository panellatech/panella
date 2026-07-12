"""Hermetic tests for the release compose producer."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release" / "pin_compose.py"
STORE_REF = "example.test/panella-store@sha256:" + "a" * 64
APP_REF = "example.test/panella-app@sha256:" + "b" * 64
VERSION = "0.0.0.dev0"
HEADER = (
    "# managed by panella up — release 0.0.0.dev0 — do not edit "
    "(hand-edits: use the git-clone path; upgrades: docs/UPGRADE.md)"
).encode()


def _load_pin_compose():
    spec = importlib.util.spec_from_file_location("pin_compose", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PIN_COMPOSE = _load_pin_compose()


def _run_pin(tmp_path: Path, compose: Path) -> tuple[Path, Path]:
    out_compose = tmp_path / "compose.pinned.yml"
    out_digests = tmp_path / "digests.json"
    exit_code = PIN_COMPOSE.main(
        [
            "--compose",
            str(compose),
            "--store-ref",
            STORE_REF,
            "--app-ref",
            APP_REF,
            "--version",
            VERSION,
            "--run-id",
            "42",
            "--run-attempt",
            "3",
            "--event",
            "workflow_dispatch",
            "--ref",
            "refs/heads/main",
            "--head-sha",
            "c" * 40,
            "--out-compose",
            str(out_compose),
            "--out-digests",
            str(out_digests),
        ]
    )
    assert exit_code == 0
    return out_compose, out_digests


def test_real_compose_transform_preserves_all_other_bytes(tmp_path: Path) -> None:
    source = ROOT / "docker-compose.yml"
    out_compose, _ = _run_pin(tmp_path, source)

    expected = source.read_bytes()
    expected = expected.replace(
        b"    image: ghcr.io/panellatech/panella-store:v0.2.0\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"      target: store\n",
        b"    image: " + STORE_REF.encode("utf-8") + b"\n",
    )
    expected = expected.replace(
        b"    image: ghcr.io/panellatech/panella-app:v0.2.0\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"      target: app\n",
        b"    image: " + APP_REF.encode("utf-8") + b"\n",
    )

    pinned = out_compose.read_bytes()
    assert pinned == HEADER + b"\n" + expected
    assert pinned.splitlines()[0] == HEADER


def test_digests_schema_uses_final_compose_bytes_and_locked_types(tmp_path: Path) -> None:
    out_compose, out_digests = _run_pin(tmp_path, ROOT / "docker-compose.yml")
    pinned = out_compose.read_bytes()

    digests = json.loads(out_digests.read_text(encoding="utf-8"))
    assert digests == {
        "schema": 1,
        "version": VERSION,
        "run_id": 42,
        "run_attempt": 3,
        "event": "workflow_dispatch",
        "ref": "refs/heads/main",
        "head_sha": "c" * 40,
        "store_ref": STORE_REF,
        "app_ref": APP_REF,
        "compose_sha256": hashlib.sha256(pinned).hexdigest(),
    }
    assert isinstance(digests["schema"], int)
    assert isinstance(digests["run_id"], int)
    assert isinstance(digests["run_attempt"], int)
    assert all(isinstance(digests[key], str) for key in digests if key not in {"schema", "run_id", "run_attempt"})


def test_removes_nested_build_blocks_without_touching_later_nested_values(tmp_path: Path) -> None:
    source = tmp_path / "source.yml"
    source.write_bytes(
        b"services:\n"
        b"  panella:\n"
        b"    image: local-store\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"      args:\n"
        b"        FLAVOR: store\n"
        b"      nested:\n"
        b"        more:\n"
        b"          - hidden\n"
        b"    environment:\n"
        b"      KEEP: store\n"
        b"  panella-http:\n"
        b"    image: local-app\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"      args:\n"
        b"        FLAVOR: app\n"
        b"    environment:\n"
        b"      KEEP: app\n"
    )

    out_compose, _ = _run_pin(tmp_path, source)
    pinned = out_compose.read_bytes()

    assert b"build:" not in pinned
    assert b"pull_policy:" not in pinned
    assert b"context:" not in pinned
    assert b"FLAVOR:" not in pinned
    assert b"nested:" not in pinned
    assert b"hidden" not in pinned
    assert b"    environment:\n      KEEP: store\n" in pinned
    assert b"    environment:\n      KEEP: app\n" in pinned


def test_build_block_skip_survives_blank_and_comment_lines(tmp_path: Path) -> None:
    # terra P1: a blank or comment-only line inside a removed build: block must not terminate the
    # skip — a nested key after the gap (target:) would otherwise leak into the pinned output.
    source = tmp_path / "source.yml"
    source.write_bytes(
        b"services:\n"
        b"  panella:\n"
        b"    image: local-store\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"\n"
        b"      # a same-block comment after a blank line\n"
        b"      target: store\n"
        b"    # a same-indent comment between the removed block and the next key\n"
        b"    environment:\n"
        b"      KEEP: store\n"
        b"  panella-http:\n"
        b"    image: local-app\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"      target: app\n"
    )

    out_compose, _ = _run_pin(tmp_path, source)
    pinned = out_compose.read_bytes()

    assert b"build:" not in pinned
    assert b"context:" not in pinned
    assert b"target:" not in pinned
    assert b"    environment:\n      KEEP: store\n" in pinned
    assert b"image: " + STORE_REF.encode("utf-8") in pinned
    assert b"image: " + APP_REF.encode("utf-8") in pinned


def test_missing_expected_keys_fail_closed(tmp_path: Path) -> None:
    # terra P2: deleting the missing-keys check must not leave this suite green. Each omitted
    # service.property must abort the transform, naming the exact missing key.
    base = (
        b"services:\n"
        b"  panella:\n"
        b"{store_props}"
        b"  panella-http:\n"
        b"{app_props}"
    )
    full_store = b"    image: s\n    pull_policy: build\n    build:\n      context: .\n"
    full_app = b"    image: a\n    pull_policy: build\n    build:\n      context: .\n"
    cases = [
        ("panella.image", full_store.replace(b"    image: s\n", b""), full_app),
        ("panella.pull_policy", full_store.replace(b"    pull_policy: build\n", b""), full_app),
        ("panella.build", full_store.replace(b"    build:\n      context: .\n", b""), full_app),
        ("panella-http.image", full_store, full_app.replace(b"    image: a\n", b"")),
        ("panella-http.pull_policy", full_store, full_app.replace(b"    pull_policy: build\n", b"")),
        ("panella-http.build", full_store, full_app.replace(b"    build:\n      context: .\n", b"")),
    ]
    import pytest

    for missing_name, store_props, app_props in cases:
        compose = base.replace(b"{store_props}", store_props).replace(b"{app_props}", app_props)
        with pytest.raises(ValueError) as excinfo:
            PIN_COMPOSE.transform_compose(compose, store_ref=STORE_REF, app_ref=APP_REF)
        assert missing_name in str(excinfo.value)


def test_duplicate_image_lines_are_all_replaced_for_extraction_fidelity(tmp_path: Path) -> None:
    # terra P2: the retired inline transformer replaced EVERY matching occurrence; duplicate keys
    # are malformed compose owned by the downstream `docker compose config` validation, not by an
    # extra rejection here.
    source = tmp_path / "source.yml"
    source.write_bytes(
        b"services:\n"
        b"  panella:\n"
        b"    image: one\n"
        b"    image: two\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"  panella-http:\n"
        b"    image: a\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
    )

    out_compose, _ = _run_pin(tmp_path, source)
    pinned = out_compose.read_bytes()

    assert pinned.count(b"image: " + STORE_REF.encode("utf-8")) == 2
    assert b"image: one" not in pinned
    assert b"image: two" not in pinned


def test_unrelated_comment_after_build_block_is_preserved(tmp_path: Path) -> None:
    # terra r2 P2: a comment at indent <= the build: key documents what FOLLOWS the removed block
    # (e.g. a root-level comment before volumes:) — it must be emitted verbatim, not swallowed.
    source = tmp_path / "source.yml"
    source.write_bytes(
        b"services:\n"
        b"  panella:\n"
        b"    image: s\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"  panella-http:\n"
        b"    image: a\n"
        b"    pull_policy: build\n"
        b"    build:\n"
        b"      context: .\n"
        b"# root comment documenting the volumes section\n"
        b"      target: app\n"
        b"volumes:\n"
        b"  data:\n"
    )

    out_compose, _ = _run_pin(tmp_path, source)
    pinned = out_compose.read_bytes()

    # terra r3 P3: the nested key AFTER the emitted boundary comment proves the skip stayed armed —
    # a mutant that clears the skip when emitting the comment would leak "target: app" and fail here.
    assert b"target: app" not in pinned
    assert b"# root comment documenting the volumes section\nvolumes:\n  data:\n" in pinned
    assert b"build:" not in pinned
