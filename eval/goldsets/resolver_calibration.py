#!/usr/bin/env python3
"""Run the one-to-one public calibration universe through the resolver.

Production supplies a ChatFn binding outside this module.  ``--fake`` is deliberately
deterministic and exists solely for hermetic tests and artifact-format verification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from panella.resolver.calibrate import build_manifest, dump_manifest, verify, write_evidence
from panella.resolver.engine import RESOLVER_CODE_VERSION, ResolverEngine
from panella.resolver.fallback import FallbackProvider
from panella.resolver.normalize import normalizer_rules_hash
from panella.resolver.registry import load_registry
from panella.resolver.types import CalibrationSlice, ResolveRequest, ResolverConfig, ResolverContext, RunBudget

HERE = Path(__file__).resolve().parent
DEFAULT_PROBES = HERE / "calibration_probes_v1.json"


def _load(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["probes"] if isinstance(document, dict) else document


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bootstrap_manifest(provider: FallbackProvider) -> tuple[object, str]:
    registry = load_registry()
    permissive = CalibrationSlice(100, (100, 0, 0, 0, 0, 0, 0, 0, 0, 0), ((0.0, 1.0, 1.0),), 1.0)
    return build_manifest(
        model_id=provider.model_id,
        prompt_template_hash=provider.prompt_template_hash,
        fitted_on_evidence_hash="bootstrap-only",
        fitted_on_git_commit="bootstrap-only",
        fitted_on_goldset_hashes=("bootstrap-only",),
        slices={"benign": permissive, "hr": permissive},
        registry_hash=registry.content_hash,
        normalizer_hash=normalizer_rules_hash,
        resolver_code_version=RESOLVER_CODE_VERSION,
    )


def run(
    probes: list[dict[str, Any]], *, provider: FallbackProvider, git_commit: str, evidence_path: Path, manifest_path: Path, probe_path: Path,
) -> tuple[Path, Path]:
    by_uid = {probe["probe_uid"]: probe for probe in probes}
    if len(by_uid) != len(probes):
        raise ValueError("calibration probes must have unique IDs")
    bootstrap, bootstrap_hash = _bootstrap_manifest(provider)
    engine = ResolverEngine(ResolverConfig(True, 1000, bootstrap, bootstrap_hash, "bootstrap-only"), provider=provider)
    budget = RunBudget(len(probes))
    rows: list[dict[str, Any]] = []
    for probe in probes:
        decision = engine.resolve(
            ResolveRequest(probe["probe_uid"], probe["kind"], probe["raw_domain"], probe["value"], probe["evidence_text"]),
            ResolverContext(()), budget,
        )
        if decision.llm_receipt is None or decision.blocking_receipt is None:
            raise RuntimeError(f"probe {probe['probe_uid']} did not make exactly one fallback attempt")
        receipt = decision.llm_receipt
        rows.append(
            {
                "probe_uid": probe["probe_uid"],
                "slice": decision.blocking_receipt.slice,
                "choice_set": list(receipt.blocking.choice_set),
                "choice_set_hash": receipt.blocking.choice_set_hash,
                "raw_choice": receipt.raw_choice,
                "raw_confidence": receipt.raw_confidence,
                "model_id": receipt.model_id,
                "prompt_template_hash": receipt.prompt_template_hash,
            }
        )
    if budget.calls_made != len(probes) or len(rows) != len(probes):
        raise RuntimeError("calibration run violated one-to-one probe coverage")
    evidence_digest = write_evidence(evidence_path, rows)
    observations = [
        {"probe_uid": row["probe_uid"], "slice": row["slice"], "raw_confidence": row["raw_confidence"], "correct": row["raw_choice"] == by_uid[row["probe_uid"]]["expected_slot_id"]}
        for row in rows
    ]
    from panella.resolver.calibrate import fit

    manifest, _ = build_manifest(
        model_id=provider.model_id,
        prompt_template_hash=provider.prompt_template_hash,
        fitted_on_evidence_hash=evidence_digest,
        fitted_on_git_commit=git_commit,
        fitted_on_goldset_hashes=(_file_hash(probe_path),),
        slices=fit(observations),
    )
    dump_manifest(manifest_path, manifest)
    verify(evidence_path, manifest_path, probe_path=probe_path)
    return evidence_path, manifest_path


def fake_provider(probes: list[dict[str, Any]]) -> FallbackProvider:
    expected_by_uid = {probe["probe_uid"]: probe["expected_slot_id"] for probe in probes}

    def chat(_: str, user: str) -> str:
        request = json.loads(user.split("\n\n", 1)[1])["request"]
        uid = str(request["evidence_text"]).rsplit(" ", 1)[-1]
        return json.dumps({"choice": expected_by_uid[uid], "confidence": 1.0})

    return FallbackProvider(chat, model_id="fake-calibration-v1")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probes", type=Path, default=DEFAULT_PROBES)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--git-commit", default="chief-supplied-at-real-run")
    parser.add_argument("--fake", action="store_true")
    args = parser.parse_args()
    if not args.fake:
        raise SystemExit("real ChatFn binding is chief-run by design; use --fake only for hermetic verification")
    probes = _load(args.probes)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    run(probes, provider=fake_provider(probes), git_commit=args.git_commit, evidence_path=args.out_dir / "calibration_evidence.jsonl", manifest_path=args.out_dir / "calibration_manifest.json", probe_path=args.probes)
    print(f"wrote calibration artifacts under {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
