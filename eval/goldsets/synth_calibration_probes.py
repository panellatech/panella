#!/usr/bin/env python3
"""Generate the public, synthetic K1 calibration probe universe.

The intentionally non-registry surfaces are drawn from three families: long-tail
role synonyms (``latent_<domain>_signal``), compound descriptions
(``unmapped_<domain>_attribute``), and empty domains.  A generation sweep imports
the shipped registry, normalizer, and alias maps to prove that every probe misses
both deterministic layers.  High-risk probes carry only the owning slot's
lexicon evidence, so their declared slices are mechanically checked as well.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from panella.resolver.normalize import resolver_normalize
from panella.resolver.blocking import assemble_blocking
from panella.resolver.registry import load_registry
from panella.resolver.risk import compute_risk_evidence
from panella.resolver.types import ResolveRequest

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "calibration_probes_v1.json"


def _raw_surface(domain: str, index: int) -> str:
    patterns = ("", f"latent_{domain}_signal", f"unmapped_{domain}_attribute", f"{domain}_contextual_marker")
    return patterns[index % len(patterns)]


def generate() -> dict[str, Any]:
    registry = load_registry()
    probes: list[dict[str, str]] = []
    routed: dict[str, int] = {"benign": 0, "hr": 0}
    # Candidate surfaces are deliberately oversampled.  The retained pools are selected by the
    # *live blocking receipt*, not by the expected slot's risk bit: choice-set top-up can route a
    # superficially benign probe to hr.
    index = 0
    while routed["benign"] < 60 or routed["hr"] < 36:
        if index >= 10_000:
            raise RuntimeError("unable to fill the routed calibration probe floors")
        slot = registry.slots[index % len(registry.slots)]
        uid = f"cal-{index + 1:04d}"
        lexeme = slot.hr_lexicon[0] if slot.high_risk else "ordinary synthetic preference"
        probe = {
            "probe_uid": uid,
            "kind": slot.kind,
            "raw_domain": _raw_surface(slot.domain, index),
            "value": f"fictional {slot.domain} calibration value {uid}",
            "evidence_text": f"fictional calibration evidence {lexeme} {uid}",
            "expected_slot_id": slot.slot_id,
        }
        request = ResolveRequest(uid, slot.kind, probe["raw_domain"], probe["value"], probe["evidence_text"])
        blocked = assemble_blocking(request, registry, compute_risk_evidence(request, registry))
        slice_name = blocked.receipt.slice
        if not blocked.forced_overflow and slot.slot_id in blocked.receipt.choice_set and routed[slice_name] < {"benign": 60, "hr": 36}[slice_name]:
            probes.append(probe | {"slice": slice_name})
            routed[slice_name] += 1
        index += 1
    document = {"version": "v1", "probes": probes}
    _sweep(document)
    return document


def _sweep(document: dict[str, Any]) -> None:
    registry = load_registry()
    probes = document["probes"]
    if len(probes) < 96 or len({p["probe_uid"] for p in probes}) != len(probes):
        raise ValueError("probe universe must have unique uid coverage")
    counts = {"benign": 0, "hr": 0}
    for probe in probes:
        request = ResolveRequest(probe["probe_uid"], probe["kind"], probe["raw_domain"], probe["value"], probe["evidence_text"])
        normalized = resolver_normalize(request.raw_domain)
        if f"{request.kind}:{normalized}" in registry.by_id or request.raw_domain in registry.alias_raw or normalized in registry.alias_folded:
            raise ValueError(f"probe {request.request_uid} hits a deterministic resolver layer")
        target = registry.by_id.get(probe["expected_slot_id"])
        if target is None:
            raise ValueError(f"probe {request.request_uid} has unknown expected slot")
        evidence = compute_risk_evidence(request, registry)
        blocked = assemble_blocking(request, registry, evidence)
        if blocked.forced_overflow or probe["slice"] != blocked.receipt.slice:
            raise ValueError(f"probe {request.request_uid} has inconsistent routed slice")
        if target.high_risk and target.slot_id not in evidence.matched_hr_slot_ids:
            raise ValueError(f"probe {request.request_uid} lacks target high-risk evidence")
        counts[blocked.receipt.slice] += 1
    if counts["benign"] < 60 or counts["hr"] < 36:
        raise ValueError("probe universe is below the frozen calibration floor")


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = _canonical(generate())
    if args.check:
        if not args.out.exists() or args.out.read_text(encoding="utf-8") != content:
            raise SystemExit("calibration probe universe is not byte-identical to deterministic generation")
        print("calibration probes: OK")
        return 0
    args.out.write_text(content, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
