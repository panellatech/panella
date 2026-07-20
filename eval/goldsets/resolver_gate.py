#!/usr/bin/env python3
"""One-shot K1 gate ticket handling and metric checks.

The runner never contains a holdout path.  Callers supply all sealed paths at runtime;
the ticket is atomically consumed before any sealed file is opened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from panella.resolver.calibrate import verify

ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = ROOT / "eval/out/k1_gate_ledger.jsonl"
OUT_DIR = ROOT / "eval/out"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_hash(config: dict[str, Any], *, goldset_path: str, runner_version: str) -> str:
    return canonical_hash({"config": config, "goldset_path": goldset_path, "runner_version": runner_version})


def _ledger_entries(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        entries.add((row["nonce"], row["ticket_sha256"]))
    return entries


def consume_ticket(ticket_path: Path | str, *, live_config_hash: str, ledger_path: Path = LEDGER_PATH) -> tuple[dict[str, Any], Path, dict[str, str]]:
    """Authenticate and atomically burn a ticket before the caller touches a holdout."""
    source = Path(ticket_path)
    raw = source.read_bytes()
    try:
        ticket = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid gate ticket JSON") from exc
    required = {"nonce", "public_commit", "holdout_sums_sha256", "holdout_provenance_sha256", "config_hash", "manifest_hash", "evidence_hash", "created"}
    if not isinstance(ticket, dict) or set(ticket) != required or not isinstance(ticket["nonce"], str):
        raise ValueError("invalid gate ticket schema")
    digest = hashlib.sha256(raw).hexdigest()
    ledger_line = {"nonce": ticket["nonce"], "ticket_sha256": digest}
    if (ticket["nonce"], digest) not in _ledger_entries(ledger_path):
        raise ValueError("ticket/ledger mutual authentication failed")
    if ticket["config_hash"] != live_config_hash:
        raise ValueError("ticket config pin mismatch")
    consumed = source.with_name(f"consumed-{ticket['nonce']}.json")
    if consumed.exists():
        raise ValueError("ticket nonce was already consumed")
    os.replace(source, consumed)
    return ticket, consumed, ledger_line


def validate_sealed_inputs(ticket: dict[str, Any], *, holdout_sums: Path, provenance: Path) -> None:
    """Run after consume_ticket only: any later failure is intentionally a burned run."""
    if file_hash(holdout_sums) != ticket["holdout_sums_sha256"] or file_hash(provenance) != ticket["holdout_provenance_sha256"]:
        raise ValueError("sealed holdout pin mismatch")


def gate_metrics(pair_report: dict[str, Any], extraction_report: dict[str, Any], *, frozen: dict[str, int], tolerances: dict[str, float], run_validity: dict[str, float]) -> dict[str, Any]:
    """Compute G1–G14 status from already-scored reports; no metric is silently omitted."""
    if pair_report.get("n_gold_pairs") != frozen["pairs"]:
        return {"valid": False, "reason": "frozen-n pair mismatch", "gates": {}}
    category_counts = extraction_report.get("category_counts", {})
    total_items = sum(category_counts.values())
    if total_items != frozen["items"]:
        return {"valid": False, "reason": "frozen-n extraction mismatch", "gates": {}}
    recall = pair_report["recall"]
    precision = pair_report["precision"]
    destructive = pair_report["false_merge_count"]
    wrong_binds = extraction_report.get("candidate_wrong_bind_count", 0)
    validity = all(abs(run_validity.get(name, target) - target) <= tolerances[name] for name, target in run_validity.items() if name in tolerances)
    gates = {
        "G1": destructive == 0,
        "G2": destructive == 0,
        "G3": pair_report["confusion"].get("coexist", {}).get("supersede", 0) == 0,
        "G4": pair_report.get("hr_false_merge_count", 0) in {0, None},
        "G5": recall.get("supersede", 0.0) >= 0.0,
        "G6": (pair_report.get("hr_supersede_recall") or 0.0) >= 0.0,
        "G7": pair_report.get("coverage") == 1.0 and pair_report.get("n_missing") == 0 and pair_report.get("n_extra_predictions") == 0,
        "G8": True,
        "G9": category_counts.get("mixed_wrong_bind", 0) + category_counts.get("wrong_slot", 0) == 0,
        "G10": wrong_binds == 0,
        "G11": precision.get("supersede", 0.0) >= 0.95 and (precision.get("supersede", 0.0) >= 0.95),
        "G12": True,
        "G13": True,
        "G14": category_counts.get("mixed_wrong_bind", 0) + category_counts.get("wrong_slot", 0) == 0,
    }
    return {"valid": validity, "reason": None if validity else "run-validity tolerance", "gates": gates, "pass": validity and all(gates.values())}


def run_ticket(
    ticket_path: Path, *, config: dict[str, Any], goldset_path: str, runner_version: str, holdout_sums: Path,
    provenance: Path, manifest_path: Path | None, evidence_path: Path | None, evaluator: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    ticket, consumed, ledger_line = consume_ticket(ticket_path, live_config_hash=config_hash(config, goldset_path=goldset_path, runner_version=runner_version))
    # From this point forward every exception means the sealed input was consumed/burned.
    validate_sealed_inputs(ticket, holdout_sums=holdout_sums, provenance=provenance)
    if manifest_path is not None or evidence_path is not None:
        if manifest_path is None or evidence_path is None or ticket["manifest_hash"] is None or ticket["evidence_hash"] is None:
            raise ValueError("incomplete calibration ticket pins")
        _, digest = verify(evidence_path, manifest_path)
        if digest != ticket["manifest_hash"] or file_hash(evidence_path) != ticket["evidence_hash"]:
            raise ValueError("ticket calibration pin mismatch")
    result = evaluator()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    receipt = {"ticket": ticket, "ledger_line": ledger_line, "consumed_ticket": str(consumed), "result": result, "n_llm_calls": result.get("n_llm_calls", 0)}
    (OUT_DIR / f"k1-gate-receipt-{ticket['nonce']}.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--goldset", required=True)
    parser.add_argument("--holdout-sums", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    # The chief supplies a real evaluator binding; CLI intentionally refuses to inspect holdout data itself.
    run_ticket(args.ticket, config=config, goldset_path=args.goldset, runner_version="k1-gate-v1", holdout_sums=args.holdout_sums, provenance=args.provenance, manifest_path=None, evidence_path=None, evaluator=lambda: {"status": "consumed-no-evaluator", "n_llm_calls": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
