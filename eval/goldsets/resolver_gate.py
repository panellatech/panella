#!/usr/bin/env python3
"""One-shot K1 gate ticket handling and fail-closed metric checks.

Receipts embed the full gate verdict: gate failure writes a receipt and exits non-zero,
while structurally invalid evaluator output errors before a receipt is written; the evaluator factory
reference is config-pinned (ticket-bound) and must resolve inside the repository tree, while
deliberate root-level tampering remains outside the protocol threat model.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from panella.resolver.calibrate import DEFAULT_PROBE_PATH, verify

ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = ROOT / "eval/out/k1_gate_ledger.jsonl"
OUT_DIR = ROOT / "eval/out"
_NONCE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_HOLDOUT_MINIMA = {
    "pairs": {"total": 80, "supersede": 25, "hr_supersede": 10, "coexist": 9, "unrelated": 46},
    "items": {"total": 24, "hr_positives": 10, "benign_positives": 6, "update_pairs": 6},
}


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


def _worktree_binding() -> dict[str, Any]:
    """Read the only two git facts a ticket is allowed to bind."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, text=True, capture_output=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, check=True, text=True, capture_output=True).stdout.strip())
    if not _GIT_OID.fullmatch(head):
        raise ValueError("cannot determine a full git HEAD")
    return {"actual_commit": head, "dirty": dirty}


def consume_ticket(ticket_path: Path | str, *, live_config_hash: str, ledger_path: Path = LEDGER_PATH) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """Authenticate and atomically burn a ticket before the caller touches a holdout."""
    source = Path(ticket_path)
    raw = source.read_bytes()
    try:
        ticket = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid gate ticket JSON") from exc
    required = {"nonce", "public_commit", "holdout_sums_sha256", "holdout_provenance_sha256", "holdout_counts_sha256", "config_hash", "manifest_hash", "evidence_hash", "created"}
    if not isinstance(ticket, dict) or set(ticket) != required or not isinstance(ticket["nonce"], str) or not _NONCE.fullmatch(ticket["nonce"]):
        raise ValueError("invalid gate ticket schema")
    digest = hashlib.sha256(raw).hexdigest()
    ledger_line: dict[str, Any] = {"nonce": ticket["nonce"], "ticket_sha256": digest}
    if (ticket["nonce"], digest) not in _ledger_entries(ledger_path):
        raise ValueError("ticket/ledger mutual authentication failed")
    if ticket["config_hash"] != live_config_hash:
        raise ValueError("ticket config pin mismatch")
    binding = _worktree_binding()
    if binding["actual_commit"] != ticket["public_commit"] or binding["dirty"]:
        raise ValueError("ticket public-commit/clean-worktree binding failed")
    # The authenticated ledger's directory is global nonce state, so copied tickets cannot burn twice.
    consumed = Path(ledger_path).parent / f"consumed-{ticket['nonce']}.json"
    # O_EXCL is the consumption point: one concurrent winner; crashes or unlink failures leave the marker fail-closed.
    try:
        claim = os.open(consumed, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError("ticket nonce was already consumed") from None
    with os.fdopen(claim, "wb") as marker:
        marker.write(raw)
    source.unlink()
    ledger_line["worktree_binding"] = binding
    return ticket, consumed, ledger_line


def _sealed_files(holdout_sums: Path) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for line in holdout_sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        pieces = line.split(maxsplit=1)
        if len(pieces) != 2 or not _SHA256.fullmatch(pieces[0]):
            raise ValueError("invalid SHA256SUMS entry")
        name = pieces[1].removeprefix("*")
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe SHA256SUMS path")
        rows.append((pieces[0], holdout_sums.parent / relative))
    if not rows:
        raise ValueError("SHA256SUMS must list holdout files")
    return rows


def validate_sealed_inputs(ticket: dict[str, Any], *, holdout_sums: Path, provenance: Path, holdout_counts: Path) -> None:
    """Run after consume_ticket only: sums, provenance, and counts failures intentionally burn the run."""
    if (
        file_hash(holdout_sums) != ticket["holdout_sums_sha256"]
        or file_hash(provenance) != ticket["holdout_provenance_sha256"]
        or file_hash(holdout_counts) != ticket["holdout_counts_sha256"]
    ):
        raise ValueError("sealed holdout pin mismatch")
    for expected, path in _sealed_files(holdout_sums):
        if not path.is_file() or file_hash(path) != expected:
            raise ValueError(f"sealed holdout file mismatch: {path.name}")


def validate_holdout_minima(counts: dict[str, Any]) -> None:
    """Require the sealed holdout's frozen slice floors before evaluation starts."""
    if not isinstance(counts, dict):
        raise ValueError("holdout counts are required")
    for group, minima in _HOLDOUT_MINIMA.items():
        observed = counts.get(group)
        if not isinstance(observed, dict):
            raise ValueError(f"holdout {group} counts are missing")
        for name, minimum in minima.items():
            value = observed.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"holdout {group}.{name} is below its frozen minimum")


def _split(reports: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = reports.get(name) if isinstance(reports, dict) else None
    return value if isinstance(value, dict) else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _matches_frozen_count(observed: Any, frozen: Any) -> bool:
    observed_count, frozen_count = _integer(observed), _integer(frozen)
    return observed_count is not None and frozen_count is not None and observed_count == frozen_count


def _pair_count(report: dict[str, Any] | None, gold: str, predicted: str) -> int | None:
    if report is None:
        return None
    confusion = report.get("confusion")
    row = confusion.get(gold) if isinstance(confusion, dict) else None
    return _integer(row.get(predicted)) if isinstance(row, dict) else None


def _row_total(report: dict[str, Any] | None, gold: str) -> int | None:
    if report is None:
        return None
    confusion = report.get("confusion")
    row = confusion.get(gold) if isinstance(confusion, dict) else None
    if not isinstance(row, dict) or not row or any(_integer(value) is None for value in row.values()):
        return None
    return sum(row.values())


def _category_correct(report: dict[str, Any] | None, slice_name: str, denominator: int) -> bool:
    categories = report.get("category_counts_by_slice") if report else None
    counts = categories.get(slice_name) if isinstance(categories, dict) else None
    if not isinstance(counts, dict) or any(_integer(value) is None for value in counts.values()):
        return False
    return sum(counts.values()) == denominator and _integer(counts.get("correct")) is not None


def _abstention_ok(report: dict[str, Any] | None, *, hr_bound: float) -> bool:
    if report is None:
        return False
    rates, counts = report.get("abstention_rates"), report.get("abstention_item_counts")
    if not isinstance(rates, dict) or not isinstance(counts, dict):
        return False
    for name, maximum in (("overall", 0.20), ("benign", 0.15), ("hr", hr_bound)):
        rate = _number(rates.get(name))
        values = counts.get(name)
        if rate is None or not isinstance(values, dict):
            return False
        numerator, denominator = _integer(values.get("abstained")), _integer(values.get("eligible"))
        if numerator is None or denominator is None or denominator < 0 or numerator < 0 or numerator > denominator:
            return False
        calculated = numerator / denominator if denominator else 0.0
        if rate != calculated or rate > maximum:
            return False
    return True


def _gate_metrics_for_split(pair: dict[str, Any] | None, extraction: dict[str, Any] | None, *, split: str) -> dict[str, bool]:
    sup_total = 80 if split == "public" else 25
    sup_allowed_misses = 2 if split == "public" else 1
    hr_total = 16 if split == "public" else 10
    stability_total = 17 if split == "public" else 6
    stability_min = 16 if split == "public" else 6
    hr_items = 18 if split == "public" else 10
    hr_min = 17 if split == "public" else 9
    benign_items = 24 if split == "public" else 6
    benign_min = 22 if split == "public" else 6
    sup_correct, hr_correct = _pair_count(pair, "supersede", "supersede"), _integer(pair.get("hr_supersede_correct")) if pair else None
    hr_reported_total = _integer(pair.get("hr_supersede_total")) if pair else None
    stability_correct = _integer(extraction.get("key_stability_correct")) if extraction else None
    stability_reported_total = _integer(extraction.get("key_stability_total")) if extraction else None
    hr_categories = extraction.get("category_counts_by_slice", {}).get("hr", {}) if extraction else {}
    benign_categories = extraction.get("category_counts_by_slice", {}).get("benign", {}) if extraction else {}
    sup_precision = _number(extraction.get("supersede_precision")) if extraction else None
    hr_precision_value = extraction.get("hr_supersede_precision") if extraction else None
    hr_precision = _number(hr_precision_value)
    hr_precision_is_none = extraction is not None and "hr_supersede_precision" in extraction and hr_precision_value is None
    hr_zero = extraction.get("hr_merged_pairs_zero") if extraction else None
    hr_merged = _integer(extraction.get("counts", {}).get("hr_merged_pairs")) if extraction and isinstance(extraction.get("counts"), dict) else None
    hr_precision_consistent = hr_merged is not None and (hr_merged == 0) == hr_precision_is_none
    hr_precision_ok = (hr_precision is not None and hr_precision == 1.0) or (hr_precision_is_none and hr_zero is True and hr_merged == 0)
    return {
        "G1": _pair_count(pair, "unrelated", "supersede") == 0,
        "G2": _pair_count(pair, "unrelated", "coexist") == 0,
        "G3": _pair_count(pair, "coexist", "supersede") == 0,
        "G4": _integer(pair.get("hr_false_merge_count")) == 0 if pair else False,
        "G5": sup_correct is not None and _row_total(pair, "supersede") == sup_total and sup_correct >= sup_total - sup_allowed_misses,
        "G6": (
            hr_correct is not None and hr_reported_total == hr_total
            and (hr_correct == 16 if split == "public" else hr_correct >= 9)
        ),
        "G7": pair is not None and pair.get("coverage") == 1.0 and _integer(pair.get("n_missing")) == 0 and _integer(pair.get("n_extra_predictions")) == 0 and _integer(pair.get("n_duplicate_predictions")) == 0 and _integer(pair.get("n_covered")) == _integer(pair.get("n_gold_pairs")),
        "G8": stability_correct is not None and stability_reported_total == stability_total and stability_correct >= stability_min,
        "G9": _category_correct(extraction, "hr", hr_items) and _integer(hr_categories.get("correct")) is not None and _integer(hr_categories.get("correct")) >= hr_min,
        "G10": extraction is not None and _integer(extraction.get("harmful_collisions")) == 0 and _integer(extraction.get("high_risk_collisions")) == 0,
        "G11": sup_precision is not None and sup_precision >= 0.95 and hr_precision_consistent and hr_precision_ok,
        "G12": extraction is not None and extraction.get("high_risk_supersede_proven") is True,
        "G13": extraction is not None and extraction.get("schema_validity") == 1.0,
        "G14": _category_correct(extraction, "benign", benign_items) and _integer(benign_categories.get("correct")) is not None and _integer(benign_categories.get("correct")) >= benign_min,
    }


def gate_metrics(pair_report: dict[str, Any], extraction_report: dict[str, Any], *, frozen: dict[str, Any], run_validity: dict[str, Any]) -> dict[str, Any]:
    """Apply the frozen K1 bars.  Missing inputs fail their gate; no metric is skipped."""
    public_pair, holdout_pair = _split(pair_report, "public"), _split(pair_report, "holdout")
    public_extraction, holdout_extraction = _split(extraction_report, "public"), _split(extraction_report, "holdout")
    by_split = {
        "public": _gate_metrics_for_split(public_pair, public_extraction, split="public"),
        "holdout": _gate_metrics_for_split(holdout_pair, holdout_extraction, split="holdout"),
    }
    gates = {name: by_split["public"][name] and by_split["holdout"][name] for name in by_split["public"]}
    public_sup, holdout_sup = _pair_count(public_pair, "supersede", "supersede"), _pair_count(holdout_pair, "supersede", "supersede")
    gates["G5"] = gates["G5"] and public_sup is not None and holdout_sup is not None and public_sup + holdout_sup >= 102
    public_hr, holdout_hr = _integer(public_pair.get("hr_supersede_correct")) if public_pair else None, _integer(holdout_pair.get("hr_supersede_correct")) if holdout_pair else None
    gates["G6"] = gates["G6"] and public_hr is not None and holdout_hr is not None and public_hr + holdout_hr >= 25
    public_stability = _integer(public_extraction.get("key_stability_correct")) if public_extraction else None
    holdout_stability = _integer(holdout_extraction.get("key_stability_correct")) if holdout_extraction else None
    gates["G8"] = gates["G8"] and public_stability is not None and holdout_stability is not None and public_stability + holdout_stability >= 22
    for gate, slice_name, minimum in (("G9", "hr", 26), ("G14", "benign", 28)):
        public_counts = public_extraction.get("category_counts_by_slice", {}).get(slice_name, {}) if public_extraction else {}
        holdout_counts = holdout_extraction.get("category_counts_by_slice", {}).get(slice_name, {}) if holdout_extraction else {}
        public_correct, holdout_correct = _integer(public_counts.get("correct")), _integer(holdout_counts.get("correct"))
        gates[gate] = gates[gate] and public_correct is not None and holdout_correct is not None and public_correct + holdout_correct >= minimum
    wrong_binds = all(_integer(report.get("candidate_wrong_bind_count")) == 0 for report in (public_extraction, holdout_extraction) if report is not None) and public_extraction is not None and holdout_extraction is not None
    hr_disabled = frozen.get("hr_llm_disabled") if isinstance(frozen, dict) else None
    hr_bound = 0.50 if hr_disabled is True else 0.35
    abstention = isinstance(hr_disabled, bool) and _abstention_ok(public_extraction, hr_bound=hr_bound) and _abstention_ok(holdout_extraction, hr_bound=hr_bound)
    observed = run_validity.get("observed") if isinstance(run_validity, dict) else None
    targets = run_validity.get("targets") if isinstance(run_validity, dict) else None
    validity = isinstance(observed, dict) and isinstance(targets, dict) and bool(targets) and set(observed) == set(targets) and all(
        _number(observed[name]) is not None and _number(targets[name]) is not None and abs(float(observed[name]) - float(targets[name])) <= 0.05
        for name in targets
    )
    frozen_ok = all(
        isinstance(frozen.get(split), dict)
        and pair is not None
        and extraction is not None
        and _matches_frozen_count(pair.get("n_gold_pairs"), frozen[split].get("pairs"))
        and _matches_frozen_count(extraction.get("n_items"), frozen[split].get("items"))
        for split, pair, extraction in (("public", public_pair, public_extraction), ("holdout", holdout_pair, holdout_extraction))
    )
    bars = {"candidate_wrong_binds": wrong_binds, "abstention": abstention}
    return {
        "valid": validity and frozen_ok,
        "reason": None if validity and frozen_ok else "run-validity or frozen-count mismatch",
        "gates": gates,
        "bars": bars,
        "abstention_receipt": {"hr_llm_disabled": hr_disabled, "hr_bound": hr_bound},
        "pass": validity and frozen_ok and all(gates.values()) and all(bars.values()),
    }


def run_ticket(
    ticket_path: Path, *, config: dict[str, Any], goldset_path: str, runner_version: str, holdout_sums: Path,
    provenance: Path, holdout_counts: Path, manifest_path: Path | None, evidence_path: Path | None,
    evaluator: Callable[[], dict[str, Any]], evaluator_ref: str | None = None, probe_path: Path | str = DEFAULT_PROBE_PATH,
    ledger_path: Path = LEDGER_PATH, out_dir: Path = OUT_DIR,
) -> dict[str, Any]:
    """Run a burned ticket and write a receipt embedding the full gate verdict.

    Gate failure writes a receipt and returns it for a non-zero CLI exit; structurally
    invalid evaluator output raises before a receipt is written, preserving burned-run semantics.
    """
    ticket, consumed, ledger_line = consume_ticket(ticket_path, live_config_hash=config_hash(config, goldset_path=goldset_path, runner_version=runner_version), ledger_path=ledger_path)
    # From this point forward every exception means the sealed input was consumed/burned.
    validate_sealed_inputs(ticket, holdout_sums=holdout_sums, provenance=provenance, holdout_counts=holdout_counts)
    validate_holdout_minima(json.loads(holdout_counts.read_text(encoding="utf-8")))
    if manifest_path is not None or evidence_path is not None or ticket["manifest_hash"] is not None or ticket["evidence_hash"] is not None:
        if manifest_path is None or evidence_path is None or ticket["manifest_hash"] is None or ticket["evidence_hash"] is None:
            raise ValueError("incomplete calibration ticket pins")
        _, digest = verify(evidence_path, manifest_path, probe_path=probe_path)
        if digest != ticket["manifest_hash"] or file_hash(evidence_path) != ticket["evidence_hash"]:
            raise ValueError("ticket calibration pin mismatch")
    result = evaluator()
    if not isinstance(result, dict):
        raise ValueError("evaluator result must be a dict")
    for section in ("pair_report", "extraction_report", "frozen", "run_validity"):
        if section not in result:
            raise ValueError(f"evaluator result is missing required gate sections: {section}")
        if not isinstance(result[section], dict):
            raise ValueError(f"evaluator result has malformed required gate sections: {section}")
    if "n_llm_calls" not in result:
        raise ValueError("evaluator result is missing required field: n_llm_calls")
    if isinstance(result["n_llm_calls"], bool) or not isinstance(result["n_llm_calls"], int) or result["n_llm_calls"] < 0:
        raise ValueError("evaluator result has malformed required field: n_llm_calls")
    verdict = gate_metrics(result["pair_report"], result["extraction_report"], frozen=result["frozen"], run_validity=result["run_validity"])
    out_dir.mkdir(parents=True, exist_ok=True)
    receipt = {"ticket": ticket, "ledger_line": ledger_line, "worktree_binding": ledger_line["worktree_binding"], "consumed_ticket": str(consumed), "evaluator_reference": evaluator_ref, "result": result, "n_llm_calls": result["n_llm_calls"], "gate_verdict": verdict, "status": "PASSED" if verdict["pass"] else "FAILED"}
    (out_dir / f"k1-gate-receipt-{ticket['nonce']}.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return receipt


def _load_evaluator(reference: str) -> Callable[..., Any]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("evaluator reference must be module:callable")
    # Verify every package prefix shallowest-first BEFORE any import executes:
    # find_spec("a.b") implicitly imports parent "a", so proving "a" in-tree first
    # guarantees implicit parent imports only ever run verified in-tree code.
    parts = module_name.split(".")
    for depth in range(1, len(parts) + 1):
        prefix = ".".join(parts[:depth])
        try:
            spec = importlib.util.find_spec(prefix)
        except (ImportError, ValueError) as exc:
            raise ValueError(f"evaluator module cannot be resolved: {prefix}") from exc
        if spec is None or not isinstance(spec.origin, str) or not spec.origin:
            raise ValueError("evaluator module has no resolvable source file")
        origin = Path(spec.origin)
        # Origin must be an absolute, existing file: built-in/frozen modules report
        # bare strings like "built-in", which Path.resolve() would otherwise anchor
        # at the current working directory and wrongly accept when CWD == ROOT.
        if not origin.is_absolute() or not origin.is_file():
            raise ValueError("evaluator module has no resolvable source file")
        if not origin.resolve().is_relative_to(ROOT):
            raise ValueError("evaluator module must live inside the repository tree")
    module = importlib.import_module(module_name)
    evaluator_factory = getattr(module, attribute, None)
    if not callable(evaluator_factory):
        raise ValueError("evaluator reference is not callable")
    return evaluator_factory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--goldset", required=True)
    parser.add_argument("--holdout-sums", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--holdout-counts", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--probe-path", type=Path, default=DEFAULT_PROBE_PATH)
    args = parser.parse_args()
    if (args.manifest is None) != (args.evidence is None):
        raise SystemExit("--manifest and --evidence must be supplied together")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    evaluator_ref = config.get("evaluator")
    if not isinstance(evaluator_ref, str) or not evaluator_ref:
        raise SystemExit("config must pin the evaluator as 'module:callable'")
    evaluator_factory = _load_evaluator(evaluator_ref)
    # This pre-flight is convenience only; run_ticket remains the authoritative check.
    try:
        pinned = json.loads(args.ticket.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pinned = None
    if isinstance(pinned, dict):
        manifest_pin, evidence_pin = pinned.get("manifest_hash"), pinned.get("evidence_hash")
        if (manifest_pin is None) != (evidence_pin is None):
            raise SystemExit("this ticket pins only one calibration artifact; regenerate the ticket")
        has_pins = manifest_pin is not None
        if has_pins and (args.manifest is None or args.evidence is None):
            raise SystemExit("this ticket pins calibration artifacts; --manifest and --evidence are required")
        if not has_pins and (args.manifest is not None or args.evidence is not None):
            raise SystemExit("this ticket has no calibration pins; omit --manifest/--evidence")
    evaluator = evaluator_factory(
        holdout_sums=args.holdout_sums,
        provenance=args.provenance,
        holdout_counts=args.holdout_counts,
        probe_path=args.probe_path,
        manifest_path=args.manifest,
        evidence_path=args.evidence,
        config=config,
    )
    if not callable(evaluator):
        raise SystemExit("evaluator factory must return a zero-argument evaluator")
    receipt = run_ticket(args.ticket, config=config, goldset_path=args.goldset, runner_version="k1-gate-v1", holdout_sums=args.holdout_sums, provenance=args.provenance, holdout_counts=args.holdout_counts, manifest_path=args.manifest, evidence_path=args.evidence, evaluator=evaluator, evaluator_ref=evaluator_ref, probe_path=args.probe_path)
    receipt_path = OUT_DIR / f"k1-gate-receipt-{receipt['ticket']['nonce']}.json"
    print(f"gate {receipt['status']} — receipt: {receipt_path}")
    return 0 if receipt["gate_verdict"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
