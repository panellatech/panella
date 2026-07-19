"""Hermetic invariants for the release-image signing boundary."""

from __future__ import annotations

import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-images.yml"
PYPI_WORKFLOW = ROOT / ".github" / "workflows" / "release-pypi.yml"
TAG_RELEASE_GUARD = "startsWith(github.ref, 'refs/tags/')"
COSIGN_STEP_NAMES = (
    "Install cosign",
    "Sign pushed image digests",
    "Verify image signatures",
)
DISPATCH_STEP_NAMES = (
    "Generate digest-pinned compose file",
    "Upload pinned compose artifact",
)


def _steps_named(name: str) -> list[dict[str, object]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish"]["steps"]
    return [step for step in steps if step.get("name") == name]


def _pypi_publish_steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(PYPI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["publish"]["steps"]


def _pypi_steps_named(name: str) -> list[dict[str, object]]:
    return [step for step in _pypi_publish_steps() if step.get("name") == name]


def _pypi_step_index(name: str) -> int:
    steps = _pypi_publish_steps()
    idx = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert len(idx) == 1, f"expected exactly one step named {name!r}, found {len(idx)}"
    return idx[0]


def test_cosign_steps_run_only_for_tag_releases() -> None:
    for name in COSIGN_STEP_NAMES:
        steps = _steps_named(name)
        assert len(steps) == 1
        assert steps[0].get("if") == TAG_RELEASE_GUARD


def test_digest_pinned_artifact_steps_remain_dispatch_enabled() -> None:
    for name in DISPATCH_STEP_NAMES:
        steps = _steps_named(name)
        assert len(steps) == 1
        assert "if" not in steps[0]


def test_verify_pins_exact_release_tag_identity_not_regex() -> None:
    # terra P1: a loose --certificate-identity-regexp accepting `refs/(tags/v.*|heads/main)` lets a
    # mutable GHCR tag be repointed to a digest signed from any other ref and still pass verify.
    # The verify step must pin the EXACT release-tag identity via --certificate-identity.
    steps = _steps_named("Verify image signatures")
    assert len(steps) == 1
    run = str(steps[0].get("run", ""))
    assert "--certificate-identity-regexp" not in run
    assert "--certificate-identity " in run
    env = steps[0].get("env", {})
    identity = str(env.get("CERT_IDENTITY", ""))
    assert identity.endswith("release-images.yml@${{ github.ref }}")
    assert "heads/main" not in identity
    # the loose regex env var must be gone entirely
    assert "CERT_IDENTITY_RE" not in run and "CERT_IDENTITY_RE" not in str(env)


def test_preflip_testpypi_publish_disables_rekor_attestations() -> None:
    # code-reviewer: pypa/gh-action-pypi-publish defaults attestations=true (v1.11+); with
    # id-token:write + trusted publishing to PEP 740-capable TestPyPI it keyless-signs via Sigstore,
    # writing a PUBLIC, append-only Rekor entry that leaks the private repo's identity + build
    # cadence. TestPyPI is the ONLY pre-flip-reachable publish (target=pypi is blocked by the
    # flip-day interlock), so it must pin attestations:false — the package-flow half of the same
    # pre-flip zero-Rekor invariant this file enforces for the image flow.
    pub = _pypi_steps_named("Publish to TestPyPI")
    assert len(pub) == 1
    assert pub[0].get("with", {}).get("attestations") is False


def test_real_pypi_publish_is_tag_ref_guarded() -> None:
    # FLIPPED 2026-07-19 (the flip-day PR): the pre-flip interlock step is deliberately gone —
    # its presence would re-block every real release, so its absence is now the asserted state.
    # The remaining pre-publish protection is the ref guard: target=pypi may only publish from
    # refs/tags/v<version> (plus the `pypi` environment and PyPI trusted publishing, which live
    # outside this file). Same ordering rule as the old interlock test: the guard must PRECEDE
    # the signing Publish-to-PyPI step, or a reorder could publish before the check runs.
    assert _pypi_steps_named("Real PyPI flip-day interlock") == []
    guard = _pypi_steps_named("Assert publish ref guard")
    assert len(guard) == 1
    assert "refs/tags/v" in str(guard[0].get("run", ""))
    assert _pypi_step_index("Assert publish ref guard") < _pypi_step_index("Publish to PyPI")


def _run_publish_ref_guard(target: str, github_ref: str) -> int:
    """Execute the 'Assert publish ref guard' step's actual shell body and return its exit code.
    Behavioral (not just structural): a future edit that neutered the guard while keeping the
    'refs/tags/v' literal would still pass the structural test above but fail these cases."""
    guard = _pypi_steps_named("Assert publish ref guard")
    assert len(guard) == 1
    script = str(guard[0]["run"])
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        completed = subprocess.run(
            ["bash", script_path],
            cwd=ROOT,  # the guard reads the version from pyproject.toml
            env={**os.environ, "TARGET": target, "GITHUB_REF": github_ref},
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(script_path)
    return completed.returncode


def test_publish_ref_guard_rejects_invalid_refs_behaviorally() -> None:
    # terra P2 (GH-bot): assert the guard actually REJECTS, not just that it contains a literal.
    with open(ROOT / "pyproject.toml", "rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    tag_ref = f"refs/tags/v{version}"

    # target=pypi: only the exact release tag passes; main and a wrong-version tag are rejected.
    assert _run_publish_ref_guard("pypi", tag_ref) == 0
    assert _run_publish_ref_guard("pypi", "refs/heads/main") != 0
    assert _run_publish_ref_guard("pypi", "refs/tags/v0.0.0-not-this") != 0
    # target=testpypi: only main passes; a tag ref is rejected.
    assert _run_publish_ref_guard("testpypi", "refs/heads/main") == 0
    assert _run_publish_ref_guard("testpypi", tag_ref) != 0
    # any unknown target is rejected outright.
    assert _run_publish_ref_guard("bogus", tag_ref) != 0
