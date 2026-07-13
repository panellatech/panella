#!/usr/bin/env python3
"""Validate the release-images run bound to a package-release dispatch."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

WORKFLOW_PATH = ".github/workflows/release-images.yml"


class ValidationError(ValueError):
    """A producer run or its pinned-artifact provenance did not meet the release contract."""


def _require(mapping: Mapping[str, Any], key: str) -> Any:
    if key not in mapping:
        raise ValidationError(f"run is missing {key!r}")
    return mapping[key]


def expected_ref(*, target: str, event: str, head_branch: str, version: str) -> str:
    tagged = event == "push" and head_branch == f"v{version}"
    main_dispatch = event == "workflow_dispatch" and head_branch == "main"
    if target == "pypi" and tagged:
        return f"refs/tags/v{version}"
    if target == "testpypi" and (tagged or main_dispatch):
        return f"refs/tags/v{version}" if tagged else "refs/heads/main"
    if target == "dryrun" and main_dispatch:
        return "refs/heads/main"
    raise ValidationError(
        f"invalid producer event matrix for target={target!r}: event={event!r}, head_branch={head_branch!r}"
    )


def validate_metadata(
    run: Mapping[str, Any], artifacts: Sequence[Mapping[str, Any]], *, target: str, version: str, checkout_sha: str
) -> tuple[int, list[str]]:
    path = _require(run, "path")
    if not isinstance(path, str) or path.split("@", 1)[0] != WORKFLOW_PATH:
        raise ValidationError(f"producer path must be {WORKFLOW_PATH!r}")
    if run.get("conclusion") != "success":
        raise ValidationError("producer run conclusion is not success")
    head_sha = _require(run, "head_sha")
    if not isinstance(head_sha, str):
        raise ValidationError("producer run head_sha must be a string")
    warnings: list[str] = []
    if head_sha != checkout_sha:
        if target == "dryrun":
            warnings.append(f"dryrun permits producer head_sha {head_sha} != checkout {checkout_sha}")
        else:
            raise ValidationError("producer head_sha does not match this checkout")
    event = _require(run, "event")
    branch = _require(run, "head_branch")
    if not isinstance(event, str) or not isinstance(branch, str):
        raise ValidationError("producer event and head_branch must be strings")
    expected_ref(target=target, event=event, head_branch=branch, version=version)
    matches = [artifact for artifact in artifacts if artifact.get("name") == "compose-pinned"]
    if len(matches) != 1:
        raise ValidationError(f"expected exactly one compose-pinned artifact, found {len(matches)}")
    artifact_id = matches[0].get("id")
    if not isinstance(artifact_id, int) or isinstance(artifact_id, bool):
        raise ValidationError("compose-pinned artifact id must be an integer")
    return artifact_id, warnings


def validate_content(run: Mapping[str, Any], digests: Mapping[str, Any], *, target: str, version: str) -> None:
    expected = expected_ref(
        target=target,
        event=_require(run, "event"),
        head_branch=_require(run, "head_branch"),
        version=version,
    )
    for digest_key, run_key in (("run_id", "id"), ("run_attempt", "run_attempt"), ("event", "event"), ("head_sha", "head_sha")):
        if _require(digests, digest_key) != _require(run, run_key):
            raise ValidationError(f"digests.{digest_key} does not match producer run {run_key}")
    if not isinstance(digests["run_id"], int) or isinstance(digests["run_id"], bool):
        raise ValidationError("digests.run_id must be an integer")
    if digests.get("version") != version:
        raise ValidationError("digests.version does not match pyproject version")
    if digests.get("ref") != expected:
        raise ValidationError(f"digests.ref must be {expected!r}")


def _github_get(path: str) -> Mapping[str, Any]:
    token = os.environ.get("GH_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        raise ValidationError("GH_TOKEN and GITHUB_REPOSITORY are required for metadata validation")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}{path}",
        headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed GitHub API origin.
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValidationError("GitHub API returned a non-object response")
    return payload


def _write_step_output(path: Path, artifact_id: int) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(f"artifact_id={artifact_id}\n")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("metadata", "content"))
    parser.add_argument("--run-id")
    parser.add_argument("--target", choices=("testpypi", "pypi", "dryrun"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-json-out", type=Path)
    parser.add_argument("--artifact-id-out", type=Path)
    parser.add_argument("--run-json", type=Path)
    parser.add_argument("--digests", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.phase == "metadata":
            if not all((args.run_id, args.target, args.run_json_out, args.artifact_id_out)):
                raise ValidationError("metadata phase requires --run-id, --target, --run-json-out, and --artifact-id-out")
            if not args.run_id.isdigit():
                # Also keeps the value from splicing the REST path below.
                raise ValidationError(f"--run-id must be a purely numeric workflow run id, got {args.run_id!r}")
            run = _github_get(f"/actions/runs/{args.run_id}")
            artifact_payload = _github_get(f"/actions/runs/{args.run_id}/artifacts?per_page=100")
            artifacts = artifact_payload.get("artifacts")
            if not isinstance(artifacts, list):
                raise ValidationError("GitHub artifacts response is missing artifacts")
            total = artifact_payload.get("total_count")
            if isinstance(total, int) and total > len(artifacts):
                # The uniqueness check below is only meaningful over the FULL list; fail closed
                # instead of paginating past what a release run should ever produce.
                raise ValidationError(f"artifact list truncated ({len(artifacts)} of {total}); refusing partial uniqueness check")
            artifact_id, warnings = validate_metadata(
                run, artifacts, target=args.target, version=args.version, checkout_sha=os.environ.get("GITHUB_SHA", "")
            )
            args.run_json_out.write_text(json.dumps(run, sort_keys=True) + "\n", encoding="utf-8")
            _write_step_output(args.artifact_id_out, artifact_id)
            for warning in warnings:
                print(f"WARNING: {warning}", file=sys.stderr)
        else:
            if not all((args.run_json, args.digests, args.target)):
                raise ValidationError("content phase requires --run-json, --digests, and --target")
            run = json.loads(args.run_json.read_text(encoding="utf-8"))
            digests = json.loads(args.digests.read_text(encoding="utf-8"))
            if not isinstance(run, dict) or not isinstance(digests, dict):
                raise ValidationError("run JSON and digests JSON must be objects")
            validate_content(run, digests, target=args.target, version=args.version)
    except (OSError, json.JSONDecodeError, ValidationError, urllib.error.URLError) as exc:
        print(f"validate_images_run: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
