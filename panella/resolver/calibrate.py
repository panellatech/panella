"""Deterministic K1 calibration fitting, artifact IO, and independent verification."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from .blocking import assemble_blocking
from .engine import MIN_CAL_SAMPLES, RESOLVER_CODE_VERSION
from .normalize import normalizer_rules_hash
from .registry import load_registry
from .risk import compute_risk_evidence
from .types import CalibrationManifest, CalibrationSlice, ResolveRequest, canonical_manifest_hash

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROBE_PATH = ROOT / "eval/goldsets/calibration_probes_v1.json"


def _six(value: Decimal | float | int) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN))


def _bin_index(confidence: float) -> int:
    return min(9, int(confidence * 10))


def fit_slice(observations: Iterable[Mapping[str, Any]]) -> CalibrationSlice | None:
    """Fit one slice exactly as §5.2; ``None`` denotes the mandated shutdown."""
    rows = sorted((dict(row) for row in observations), key=lambda row: str(row["probe_uid"]))
    if not rows:
        return None
    bins: list[list[dict[str, Any]]] = [[] for _ in range(10)]
    for row in rows:
        confidence = row.get("raw_confidence")
        correct = row.get("correct")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
            raise ValueError("raw_confidence must be finite")
        if not 0.0 <= float(confidence) <= 1.0 or not isinstance(correct, bool):
            raise ValueError("invalid calibration observation")
        bins[_bin_index(float(confidence))].append(row)
    original_counts = tuple(len(bucket) for bucket in bins)
    # Preserve interval boundaries while merging occupied/empty original bins.
    groups: list[tuple[int, int, list[dict[str, Any]]]] = [(index, index + 1, bucket[:]) for index, bucket in enumerate(bins)]
    while len(groups) > 1:
        changed = False
        for index, (_, _, bucket) in enumerate(groups):
            if len(bucket) >= 5:
                continue
            neighbor = index + 1 if index + 1 < len(groups) else index - 1
            left, right = sorted((index, neighbor))
            a, _, a_rows = groups[left]
            _, b, b_rows = groups[right]
            groups[left : right + 1] = [(a, b, a_rows + b_rows)]
            changed = True
            break
        if not changed:
            break
    mapping: list[tuple[float, float, float]] = []
    running = Decimal("0")
    row_calibrated: dict[str, float] = {}
    for start, end, bucket in groups:
        ratio = Decimal(sum(bool(item["correct"]) for item in bucket)) / Decimal(len(bucket))
        calibrated_decimal = max(running, ratio).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
        running = calibrated_decimal
        calibrated = float(calibrated_decimal)
        low, high = _six(Decimal(start) / Decimal(10)), _six(Decimal(end) / Decimal(10))
        mapping.append((low, high, calibrated))
        for item in bucket:
            row_calibrated[str(item["probe_uid"])] = calibrated
    wrong = [row_calibrated[str(row["probe_uid"])] for row in rows if not row["correct"]]
    rungs = sorted({entry[2] for entry in mapping})
    tau = rungs[0] if not wrong else next((value for value in rungs if value > max(wrong)), None)
    if tau is None:
        return None
    return CalibrationSlice(len({str(row["probe_uid"]) for row in rows}), original_counts, tuple(mapping), _six(tau))


def fit(observations: Iterable[Mapping[str, Any]]) -> dict[str, CalibrationSlice | None]:
    grouped: dict[str, list[Mapping[str, Any]]] = {"benign": [], "hr": []}
    for row in observations:
        slice_name = row.get("slice")
        if slice_name not in grouped:
            raise ValueError("unknown calibration slice")
        grouped[slice_name].append(row)
    return {name: fit_slice(rows) for name, rows in grouped.items()}


def evidence_hash(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_evidence(path: Path | str, rows: Iterable[Mapping[str, Any]]) -> str:
    """Write canonical evidence JSONL. The caller can never smuggle a correctness label in."""
    required = {
        "probe_uid", "slice", "choice_set", "choice_set_hash", "raw_choice", "raw_confidence", "model_id", "prompt_template_hash"
    }
    materialized = []
    for input_row in rows:
        if "correct" in input_row or set(input_row) != required:
            raise ValueError("evidence rows must have exactly the public evidence schema and no correct field")
        materialized.append(dict(input_row))
    output = "".join(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for row in materialized)
    Path(path).write_text(output, encoding="utf-8")
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


def manifest_dict(manifest: CalibrationManifest) -> dict[str, Any]:
    return {
        "calibration_version": manifest.calibration_version,
        "model_id": manifest.model_id,
        "prompt_template_hash": manifest.prompt_template_hash,
        "registry_hash": manifest.registry_hash,
        "normalizer_rules_hash": manifest.normalizer_rules_hash,
        "resolver_code_version": manifest.resolver_code_version,
        "fitted_on_goldset_hashes": list(manifest.fitted_on_goldset_hashes),
        "fitted_on_evidence_hash": manifest.fitted_on_evidence_hash,
        "fitted_on_git_commit": manifest.fitted_on_git_commit,
        "slices": {
        name: {"n_samples": value.n_samples, "per_bin": list(value.per_bin), "mapping": [list(x) for x in value.mapping], "tau": value.tau}
        for name, value in manifest.slices.items()
        },
    }


def build_manifest(
    *, model_id: str, prompt_template_hash: str, fitted_on_evidence_hash: str, fitted_on_git_commit: str,
    fitted_on_goldset_hashes: tuple[str, ...] | list[str], slices: Mapping[str, CalibrationSlice | None],
    calibration_version: str = "k1-calibration-v1", registry_hash: str | None = None,
    normalizer_hash: str | None = None, resolver_code_version: str = RESOLVER_CODE_VERSION,
) -> tuple[CalibrationManifest, str]:
    registry = load_registry()
    actual_slices: dict[str, CalibrationSlice] = {}
    for name in ("benign", "hr"):
        fitted = slices.get(name)
        # A shutdown is represented by a structurally valid but insufficient empty slice.
        actual_slices[name] = fitted if fitted is not None else CalibrationSlice(0, (), (), 0.0)
    manifest = CalibrationManifest(
        calibration_version, model_id, prompt_template_hash, registry_hash or registry.content_hash,
        normalizer_hash or normalizer_rules_hash, resolver_code_version, tuple(fitted_on_goldset_hashes),
        fitted_on_evidence_hash, fitted_on_git_commit, actual_slices,
    )
    return manifest, canonical_manifest_hash(manifest)


def dump_manifest(path: Path | str, manifest: CalibrationManifest) -> str:
    digest = canonical_manifest_hash(manifest)
    document = manifest_dict(manifest) | {"manifest_hash": digest}
    Path(path).write_text(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    return digest


def _validate_slice(name: str, value: CalibrationSlice) -> None:
    if value.n_samples < 0 or len(value.per_bin) not in {0, 10}:
        raise ValueError(f"{name}: invalid per_bin")
    if value.n_samples == 0:
        if value.per_bin or value.mapping or value.tau != 0.0:
            raise ValueError(f"{name}: invalid shutdown slice")
        return
    if sum(value.per_bin) != value.n_samples or not value.mapping:
        raise ValueError(f"{name}: counts do not match mapping")
    previous_high = 0.0
    previous_calibrated = -1.0
    for low, high, calibrated in value.mapping:
        if not (0.0 <= low < high <= 1.0 and low == previous_high and calibrated >= previous_calibrated):
            raise ValueError(f"{name}: non-monotone mapping")
        previous_high, previous_calibrated = high, calibrated
    if previous_high != 1.0 or value.tau not in {row[2] for row in value.mapping}:
        raise ValueError(f"{name}: invalid tau")


def load_manifest(path: Path | str) -> tuple[CalibrationManifest, str]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot parse calibration manifest") from exc
    required = {"calibration_version", "model_id", "prompt_template_hash", "registry_hash", "normalizer_rules_hash", "resolver_code_version", "fitted_on_goldset_hashes", "fitted_on_evidence_hash", "fitted_on_git_commit", "slices", "manifest_hash"}
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("manifest has an invalid schema")
    try:
        raw_slices = document["slices"]
        if not isinstance(raw_slices, dict) or set(raw_slices) != {"benign", "hr"}:
            raise ValueError("manifest must include benign and hr slices")
        slices = {
            name: CalibrationSlice(item["n_samples"], tuple(item["per_bin"]), tuple(tuple(row) for row in item["mapping"]), item["tau"])
            for name, item in raw_slices.items()
            if isinstance(item, dict) and set(item) == {"n_samples", "per_bin", "mapping", "tau"}
        }
        if set(slices) != {"benign", "hr"} or not isinstance(document["fitted_on_goldset_hashes"], list):
            raise ValueError("invalid manifest fields")
        manifest = CalibrationManifest(
            document["calibration_version"], document["model_id"], document["prompt_template_hash"], document["registry_hash"],
            document["normalizer_rules_hash"], document["resolver_code_version"], tuple(document["fitted_on_goldset_hashes"]),
            document["fitted_on_evidence_hash"], document["fitted_on_git_commit"], slices,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("manifest has invalid fields") from exc
    for name, slice_value in manifest.slices.items():
        _validate_slice(name, slice_value)
    digest = canonical_manifest_hash(manifest)
    if document["manifest_hash"] != digest:
        raise ValueError("manifest canonical hash mismatch")
    return manifest, digest


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_probes(path: Path) -> list[dict[str, Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    probes = document["probes"] if isinstance(document, dict) else document
    if not isinstance(probes, list):
        raise ValueError("probe universe must be a list or contain probes")
    return probes


def verify(
    evidence_path: Path | str,
    manifest_path: Path | str,
    *, probe_path: Path | str = DEFAULT_PROBE_PATH,
) -> tuple[CalibrationManifest, str]:
    """Execute the six deterministic verifier steps from §5.1 or raise ``ValueError``."""
    manifest, digest = load_manifest(manifest_path)
    evidence_file, probes_file = Path(evidence_path), Path(probe_path)
    if evidence_hash(evidence_file) != manifest.fitted_on_evidence_hash:
        raise ValueError("evidence hash mismatch")
    if _file_hash(probes_file) not in manifest.fitted_on_goldset_hashes:
        raise ValueError("probe universe hash is not bound by manifest")
    try:
        rows = [json.loads(line) for line in evidence_file.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError as exc:
        raise ValueError("invalid evidence JSONL") from exc
    probes = _load_probes(probes_file)
    probe_by_uid = {probe.get("probe_uid"): probe for probe in probes}
    if len(probe_by_uid) != len(probes) or {row.get("probe_uid") for row in rows} != set(probe_by_uid) or len(rows) != len(probes):
        raise ValueError("evidence and probe universe are not a bijection")
    registry = load_registry()
    recomputed: list[dict[str, Any]] = []
    for row in rows:
        required = {"probe_uid", "slice", "choice_set", "choice_set_hash", "raw_choice", "raw_confidence", "model_id", "prompt_template_hash"}
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("evidence row schema mismatch")
        probe = probe_by_uid[row["probe_uid"]]
        request = ResolveRequest(probe["probe_uid"], probe["kind"], probe["raw_domain"], probe["value"], probe["evidence_text"])
        risk = compute_risk_evidence(request, registry)
        blocked = assemble_blocking(request, registry, risk)
        if blocked.forced_overflow or row["slice"] != probe["slice"] or tuple(row["choice_set"]) != blocked.receipt.choice_set or row["choice_set_hash"] != blocked.receipt.choice_set_hash:
            raise ValueError("choice-set replay mismatch")
        if row["raw_choice"] not in set(blocked.receipt.choice_set) | {"ABSTAIN"}:
            raise ValueError("raw choice outside closed choice set")
        confidence = row["raw_confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("invalid raw confidence")
        if row["model_id"] != manifest.model_id or row["prompt_template_hash"] != manifest.prompt_template_hash:
            raise ValueError("provider identity mismatch")
        recomputed.append({"probe_uid": row["probe_uid"], "slice": row["slice"], "raw_confidence": confidence, "correct": row["raw_choice"] == probe["expected_slot_id"]})
    fitted = fit(recomputed)
    for name in ("benign", "hr"):
        expected = fitted[name] if fitted[name] is not None else CalibrationSlice(0, (), (), 0.0)
        if manifest.slices[name] != expected:
            raise ValueError(f"refit mismatch for {name}")
    if manifest.registry_hash != registry.content_hash or manifest.normalizer_rules_hash != normalizer_rules_hash or manifest.resolver_code_version != RESOLVER_CODE_VERSION:
        raise ValueError("live component binding mismatch")
    return manifest, digest
