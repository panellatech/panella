from __future__ import annotations

import hashlib
import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate = _load("validate_images_run", "scripts/ci/validate_images_run.py")
asset = _load("assert_release_asset", "scripts/ci/assert-release-asset.py")


def _run(*, event="workflow_dispatch", branch="main", sha="commit"):
    return {
        "id": 123,
        "run_attempt": 2,
        "path": ".github/workflows/release-images.yml@refs/heads/main",
        "conclusion": "success",
        "head_sha": sha,
        "event": event,
        "head_branch": branch,
    }


def _digests(*, event="workflow_dispatch", ref="refs/heads/main", sha="commit", compose=b"compose"):
    digest = hashlib.sha256(compose).hexdigest()
    return {
        "schema": 1,
        "version": "0.2.0",
        "run_id": 123,
        "run_attempt": 2,
        "event": event,
        "ref": ref,
        "head_sha": sha,
        "store_ref": "registry.test/store@sha256:" + "a" * 64,
        "app_ref": "registry.test/app@sha256:" + "b" * 64,
        "compose_sha256": digest,
    }


def test_validate_images_run_metadata_matrix_and_content_binding():
    run = _run()
    artifact_id, warnings = validate.validate_metadata(
        run, [{"name": "compose-pinned", "id": 77}], target="testpypi", version="0.2.0", checkout_sha="commit"
    )
    assert artifact_id == 77
    assert warnings == []
    validate.validate_content(run, _digests(), target="testpypi", version="0.2.0")
    assert validate.expected_ref(target="pypi", event="push", head_branch="v0.2.0", version="0.2.0") == "refs/tags/v0.2.0"
    assert validate.expected_ref(target="dryrun", event="workflow_dispatch", head_branch="main", version="0.2.0") == "refs/heads/main"


def test_validate_images_run_rejects_invalid_metadata_and_only_dryrun_warns():
    with pytest.raises(validate.ValidationError, match="head_sha"):
        validate.validate_metadata(_run(), [{"name": "compose-pinned", "id": 1}], target="testpypi", version="0.2.0", checkout_sha="other")
    _, warnings = validate.validate_metadata(
        _run(), [{"name": "compose-pinned", "id": 1}], target="dryrun", version="0.2.0", checkout_sha="other"
    )
    assert warnings
    with pytest.raises(validate.ValidationError, match="exactly one"):
        validate.validate_metadata(_run(), [], target="testpypi", version="0.2.0", checkout_sha="commit")
    bad = _digests()
    bad["run_id"] = "123"
    with pytest.raises(validate.ValidationError, match="run_id"):
        validate.validate_content(_run(), bad, target="testpypi", version="0.2.0")


def _compose(digests):
    return (
        b"# managed by panella up \xe2\x80\x94 release 0.2.0 \xe2\x80\x94 do not edit (hand-edits: use the git-clone path; upgrades: docs/UPGRADE.md)\n"
        + f"services:\n  panella:\n    image: {digests['store_ref']}\n  panella-http:\n    image: {digests['app_ref']}\n".encode()
    )


def _archives(tmp_path: Path, compose: bytes) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "panella-0.2.0-py3-none-any.whl", "w") as archive:
        archive.writestr("panella_selfhost/_assets/compose.pinned.yml", compose)
    with tarfile.open(dist / "panella-0.2.0.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("panella-0.2.0/panella_selfhost/_assets/compose.pinned.yml")
        info.size = len(compose)
        archive.addfile(info, io.BytesIO(compose))
    return dist


def test_assert_release_asset_checks_schema_raw_bytes_and_both_distributions(tmp_path):
    template = _digests(compose=b"placeholder")
    compose = _compose(template)
    digests = _digests(compose=compose)
    dist = _archives(tmp_path, compose)
    asset.assert_release_asset(dist, digests)

    digests["compose_sha256"] = "0" * 64
    with pytest.raises(asset.AssetError, match="sha256"):
        asset.assert_release_asset(dist, digests)

def test_expected_ref_locks_all_four_matrix_rows_and_rejects_off_matrix():
    assert validate.expected_ref(target="pypi", event="push", head_branch="v0.2.0", version="0.2.0") == "refs/tags/v0.2.0"
    assert validate.expected_ref(target="testpypi", event="workflow_dispatch", head_branch="main", version="0.2.0") == "refs/heads/main"
    assert validate.expected_ref(target="testpypi", event="push", head_branch="v0.2.0", version="0.2.0") == "refs/tags/v0.2.0"
    assert validate.expected_ref(target="dryrun", event="workflow_dispatch", head_branch="main", version="0.2.0") == "refs/heads/main"
    off_matrix = [
        ("pypi", "workflow_dispatch", "main"),  # pypi never publishes from a dispatch
        ("dryrun", "push", "v0.2.0"),  # dryrun never consumes a tag push
        ("testpypi", "workflow_dispatch", "develop"),  # dispatch only from main
        ("pypi", "push", "v9.9.9"),  # tag != pyproject version
        ("testpypi", "pull_request", "main"),  # event outside the domain
    ]
    for target, event, branch in off_matrix:
        with pytest.raises(validate.ValidationError):
            validate.expected_ref(target=target, event=event, head_branch=branch, version="0.2.0")


def test_validate_metadata_rejects_duplicate_artifacts_and_non_integer_ids():
    duplicated = [{"name": "compose-pinned", "id": 1}, {"name": "compose-pinned", "id": 2}]
    with pytest.raises(validate.ValidationError, match="exactly one"):
        validate.validate_metadata(_run(), duplicated, target="testpypi", version="0.2.0", checkout_sha="commit")
    with pytest.raises(validate.ValidationError, match="integer"):
        validate.validate_metadata(_run(), [{"name": "compose-pinned", "id": True}], target="testpypi", version="0.2.0", checkout_sha="commit")


def test_validate_content_binds_ref_attempt_and_version():
    stale_attempt = _digests()
    stale_attempt["run_attempt"] = 1  # rerun substituted another attempt's provenance
    with pytest.raises(validate.ValidationError, match="run_attempt"):
        validate.validate_content(_run(), stale_attempt, target="testpypi", version="0.2.0")
    wrong_ref = _digests(ref="refs/heads/develop")
    with pytest.raises(validate.ValidationError, match="ref"):
        validate.validate_content(_run(), wrong_ref, target="testpypi", version="0.2.0")
    wrong_version = _digests()
    wrong_version["version"] = "0.2.1"
    with pytest.raises(validate.ValidationError, match="version"):
        validate.validate_content(_run(), wrong_version, target="testpypi", version="0.2.0")


def test_validate_digests_schema_negatives():
    good = _digests(compose=b"x")
    reordered = dict(reversed(list(good.items())))
    with pytest.raises(asset.AssetError, match="order"):
        asset.validate_digests(reordered)
    missing = dict(good)
    missing.pop("ref")
    with pytest.raises(asset.AssetError):
        asset.validate_digests(missing)
    negatives = [
        ("schema", 2),
        ("schema", "1"),
        ("run_id", "123"),
        ("run_attempt", True),
        ("version", ""),
        ("store_ref", "registry.test/store@sha1:" + "a" * 40),
        ("compose_sha256", "A" * 64),
    ]
    for key, bad_value in negatives:
        broken = dict(good)
        broken[key] = bad_value
        with pytest.raises(asset.AssetError):
            asset.validate_digests(broken)


def test_assert_release_asset_requires_exact_member_location_and_identical_copies(tmp_path):
    template = _digests(compose=b"placeholder")
    compose = _compose(template)
    digests = _digests(compose=compose)
    dist = tmp_path / "dist"
    dist.mkdir()
    # nested copy at the WRONG location: importlib.resources would load nothing at runtime
    with zipfile.ZipFile(dist / "panella-0.2.0-py3-none-any.whl", "w") as archive:
        archive.writestr("panella_selfhost/other/panella_selfhost/_assets/compose.pinned.yml", compose)
    with tarfile.open(dist / "panella-0.2.0.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("panella-0.2.0/panella_selfhost/_assets/compose.pinned.yml")
        info.size = len(compose)
        archive.addfile(info, io.BytesIO(compose))
    with pytest.raises(asset.AssetError, match="wheel member"):
        asset.assert_release_asset(dist, digests)
    # wheel and sdist byte divergence must be refused
    (dist / "panella-0.2.0-py3-none-any.whl").unlink()
    with zipfile.ZipFile(dist / "panella-0.2.0-py3-none-any.whl", "w") as archive:
        archive.writestr("panella_selfhost/_assets/compose.pinned.yml", compose + b"# extra\n")
    with pytest.raises(asset.AssetError, match="differ"):
        asset.assert_release_asset(dist, digests)

def test_metadata_phase_rejects_non_numeric_run_id(tmp_path, capsys):
    # Injection surface: the run id reaches a REST path and (via the workflow) a shell line; a
    # non-numeric value must die before any network or output side effect.
    rc = validate.main([
        "--phase", "metadata",
        "--run-id", "123; python -c 'x'",
        "--target", "testpypi",
        "--version", "0.2.0",
        "--run-json-out", str(tmp_path / "run.json"),
        "--artifact-id-out", str(tmp_path / "out.txt"),
    ])
    assert rc == 1
    assert "numeric" in capsys.readouterr().err
    assert not (tmp_path / "run.json").exists()
