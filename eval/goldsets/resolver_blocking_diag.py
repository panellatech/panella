"""Hermetic K1-c diagnostic for the public pair face and pinned candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Iterable, Literal, Mapping

from eval.goldsets.key_correctness_eval import load_items
from panella.resolver.blocking import (
    assemble_blocking,
    blocking_v1_operative_rules_hash,
    blocking_v2_instrument_rules_hash_for,
)
from panella.resolver.engine import ResolverEngine, prepare_guard
from panella.resolver.registry import SlotRegistry, load_registry
from panella.resolver.risk import compute_risk_evidence
from panella.resolver.types import ExistingSlot, LLM_LEGS_RESERVED, ResolveRequest, ResolverContext, RunBudget

ROOT = Path(__file__).resolve().parents[2]
PAIR_GOLDSET = ROOT / "eval/goldsets/supersede_v1.json"
PAIR_GOLDSET_SHA256 = "b932fd97cfa6d63fdf027bb799094939b18d00be8d8f807cc90c9a96c92303fe"
LEDGER_PATH = ROOT / "tests/resolver/fixtures/retention_ledger_v1.json"
OUT_DIR = ROOT / "eval/out"
# Chief adds a pre-registered artifact digest here before asking this script to consume it.
CANDIDATE_HASH_ALLOWLIST: frozenset[str] = frozenset({
    # k1c_pinned_candidates_v1.json — c1-e2.1 first-valid output, ledger-pinned 2026-07-22.
    "5aec8521eee58cebf5f518e17ed97f14bb9172af31150bf3b89d705539c71fa3",
})
EXTRACTION_SOURCES = {
    "source_items": ROOT / "eval/goldsets/fixtures/extraction_goldset_v1.json",
    "source_fixture": ROOT / "eval/goldsets/fixtures/continuity_set_v1.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_pair_goldset() -> dict[str, Any]:
    if _sha256(PAIR_GOLDSET) != PAIR_GOLDSET_SHA256:
        raise ValueError("public pair goldset hash is not allowlisted")
    value = json.loads(PAIR_GOLDSET.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise ValueError("public pair goldset is malformed")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("candidate artifact has duplicate keys")
        value[key] = item
    return value


def _load_candidates(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Admit only the ledger-pinned candidates artifact (c1-e2.1): identity hash,
    pre-pinned source hashes, declared item count, and item-set shape."""
    if _sha256(path) not in CANDIDATE_HASH_ALLOWLIST:
        raise ValueError("candidate artifact hash is not allowlisted")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    candidates = value.get("candidates") if isinstance(value, dict) else None
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError("candidate artifact must contain a candidates mapping")
    if any(not isinstance(uid, str) or not uid for uid in candidates):
        raise ValueError("candidate item uids must be non-empty strings")
    if value.get("n_items") != len(candidates):
        raise ValueError("candidate item count does not match the declared n_items")
    for source_key, source_path in EXTRACTION_SOURCES.items():
        declared = value.get(source_key)
        if not isinstance(declared, dict) or declared.get("sha256") != _sha256(source_path):
            raise ValueError(f"candidate artifact {source_key} hash does not match the pinned source")
    if any(not isinstance(rows, list) for rows in candidates.values()):
        raise ValueError("candidate rows must be lists")
    source_uids = {item.item_id for item in load_items(EXTRACTION_SOURCES["source_items"])}
    if set(candidates) != source_uids:
        raise ValueError("candidate item set is not an exact bijection to the pinned source items")
    return candidates


def _retention_report(
    ledger: Mapping[str, Any], decision_slots: Mapping[str, str | None]
) -> dict[str, bool]:
    ledger_cases = {entry["request_uid"]: entry for entry in ledger["cases"]}
    missing = set(ledger_cases) - set(decision_slots)
    if missing:
        raise ValueError("retention ledger contains request uids absent from pair decisions")
    transitioned = {uid for item in ledger["transitions"] for uid in item["uids"]}
    retained = [
        uid
        for uid, row in ledger_cases.items()
        if row["initial_state"] == "must_retain_correct" and uid not in transitioned
    ]
    retained_correct = sum(decision_slots[uid] == ledger_cases[uid]["hit_slot"] for uid in retained)
    approved = [
        uid for uid, row in ledger_cases.items() if row["initial_state"] == "approved_remap"
    ] + sorted(transitioned)
    approved_eliminated = all(decision_slots[uid] != ledger_cases[uid]["hit_slot"] for uid in approved)
    return {
        "pass": retained_correct >= len(retained) - 2,
        "approved_remap_eliminated": approved_eliminated,
    }


def _resolve_pair_goldset() -> tuple[
    dict[str, Any], dict[tuple[str, str], Any], dict[tuple[str, str], tuple[str, ...]]
]:
    goldset = _load_pair_goldset()
    engine = ResolverEngine()
    budget = RunBudget(2 * sum(len(case["facts"]) for case in goldset["cases"]))
    decisions: dict[tuple[str, str], Any] = {}
    choice_sets: dict[tuple[str, str], tuple[str, ...]] = {}
    for case in goldset["cases"]:
        existing: list[ExistingSlot] = []
        for fact in sorted(case["facts"], key=lambda fact: (fact["date"], fact["fact_id"])):
            probe = fact["probe"]
            uid = f"{case['case_id']}/{fact['fact_id']}"
            request = ResolveRequest(uid, probe["kind"], probe["raw_domain"], probe["value"], fact["content"], fact["date"])
            decision = engine.resolve(request, ResolverContext(tuple(existing)), budget)
            decisions[(case["case_id"], fact["fact_id"])] = decision
            choice_sets[(case["case_id"], fact["fact_id"])] = assemble_blocking(request, engine.registry, compute_risk_evidence(request, engine.registry), scoring_mode="v1").receipt.choice_set
            if decision.action in {"BIND", "ADD"}:
                existing.append(ExistingSlot(decision.slot_id or "", fact["date"]))
    return goldset, decisions, choice_sets


def _run_pair() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    goldset, decisions, choice_sets = _resolve_pair_goldset()
    classes: Counter[str] = Counter()
    negative_sets: list[tuple[set[str], set[str]]] = []
    det_methods = {"exact", "alias"}
    det_hits: list[tuple[str, Any]] = []
    for case in goldset["cases"]:
        facts = {fact["fact_id"]: fact for fact in case["facts"]}
        for fact_id, _fact in facts.items():
            decision = decisions[(case["case_id"], fact_id)]
            if decision.method in det_methods:
                det_hits.append((f"{case['case_id']}/{fact_id}", decision))
        for pair in case["pairs"]:
            first_key, second_key = (case["case_id"], pair["earlier_id"]), (case["case_id"], pair["later_id"])
            first, second = decisions[first_key], decisions[second_key]
            both_det = first.method in det_methods and second.method in det_methods
            if both_det and first.slot_id == second.slot_id:
                category = "both_det_hit_same"
            elif both_det:
                category = "registry_caused" if case["case_id"] == "sc-hrmulti-0002" else "unresolved_semantic"
            elif set(choice_sets[first_key]) & set(choice_sets[second_key]):
                category = "llm_reachable"
            else:
                category = "STRUCTURAL"
            classes[category] += 1
            if pair.get("label") != "supersede":
                negative_sets.append((set(choice_sets[first_key]), set(choice_sets[second_key])))
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    retention = _retention_report(
        ledger,
        {
            f"{case_id}/{fact_id}": decision.slot_id
            for (case_id, fact_id), decision in decisions.items()
        },
    )
    pool_sizes = Counter(len(pool) for pool in choice_sets.values())
    overlaps = sum(bool(left & right) for left, right in negative_sets)
    return {
        "pair_classification": dict(sorted(classes.items())),
        "det": {
            "method_counts": dict(sorted(Counter(decision.method for _, decision in det_hits).items())),
            "retention": retention,
        },
        "blocking_v1": {
            "negative_choice_set_overlap": {"numerator": overlaps, "denominator": len(negative_sets)},
            "pool_size_distribution": dict(sorted(pool_sizes.items())),
            "empty_set_count": pool_sizes.get(0, 0),
            "benign_to_hr_count": sum(decision.blocking_receipt is not None and decision.blocking_receipt.slice == "hr" and not decision.risk_evidence.any for decision in decisions.values()),
            "overflow_count": sum(decision.fallback_outcome == "forced_set_overflow" for decision in decisions.values()),
        },
    }, [{"uid": uid, "slot_id": decision.slot_id} for uid, decision in det_hits]


_PRODUCTION_CARDINALITIES = {"facts": 461, "pairs": 340, "sup_pairs": 80, "negative_pairs": 260}
_INPUT_HASH_KEYS = (
    "pair_goldset",
    "candidates",
    "extraction_source_items",
    "extraction_source_fixture",
    "retention_ledger",
)
_CORE_METRIC_KEYS = (
    "negative_overlap",
    "migration",
    "overflow",
    "empty_pair_face",
    "empty_extraction_face",
    "cohort",
    "pool_size_distribution",
)


@dataclass(frozen=True)
class DiagnosticInputs:
    """All v2b inputs are explicit so tests can never fall through to real corpora."""

    pair_goldset_path: Path
    pair_goldset_sha256: str
    candidate_path: Path
    candidate_allowlist: frozenset[str]
    extraction_source_items_path: Path
    extraction_source_items_sha256: str
    extraction_source_fixture_path: Path
    extraction_source_fixture_sha256: str
    retention_ledger_path: Path
    retention_ledger_sha256: str
    out_dir: Path
    expected_cardinalities: Mapping[str, int]

    def input_hashes(self) -> dict[str, str]:
        return {
            "pair_goldset": self.pair_goldset_sha256,
            "candidates": _sha256(self.candidate_path),
            "extraction_source_items": self.extraction_source_items_sha256,
            "extraction_source_fixture": self.extraction_source_fixture_sha256,
            "retention_ledger": self.retention_ledger_sha256,
        }


def _load_json(path: Path, *, label: str, expected_hash: str) -> Any:
    if _sha256(path) != expected_hash:
        raise ValueError(f"{label} hash does not match its pin")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is malformed JSON") from exc


def _require_cardinalities(observed: Mapping[str, int], expected: Mapping[str, int]) -> None:
    if set(expected) != set(_PRODUCTION_CARDINALITIES) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in expected.values()
    ):
        raise ValueError("expected cardinalities have an invalid schema")
    if dict(observed) != dict(expected):
        raise ValueError(f"diagnostic cardinality mismatch: observed={dict(observed)} expected={dict(expected)}")


def _candidate_rows(inputs: DiagnosticInputs) -> dict[str, list[dict[str, Any]]]:
    digest = _sha256(inputs.candidate_path)
    if digest not in inputs.candidate_allowlist:
        raise ValueError("candidate artifact hash is not allowlisted")
    document = _load_json(inputs.candidate_path, label="candidate artifact", expected_hash=digest)
    rows_by_uid = document.get("candidates") if isinstance(document, dict) else None
    if not isinstance(rows_by_uid, dict) or not rows_by_uid:
        raise ValueError("candidate artifact must contain a non-empty candidates mapping")
    if document.get("n_items") != len(rows_by_uid):
        raise ValueError("candidate artifact n_items does not match candidates")
    if not all(isinstance(uid, str) and uid and isinstance(rows, list) for uid, rows in rows_by_uid.items()):
        raise ValueError("candidate artifact has invalid uid rows")
    return rows_by_uid


def _request_from_candidate(uid: str, index: int, row: Mapping[str, Any]) -> ResolveRequest:
    if row.get("source_sid") != uid:
        raise ValueError("candidate row source_sid does not match its item uid")
    candidate = row.get("candidate", row)
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate row has no candidate mapping")
    raw_domain = candidate.get("raw_domain", candidate.get("domain"))
    evidence_text = candidate.get("evidence_text", candidate.get("evidence"))
    if not all(isinstance(value, str) for value in (candidate.get("kind"), raw_domain, candidate.get("value"), evidence_text)):
        raise ValueError("candidate row lacks resolver request fields")
    effective_at = candidate.get("effective_at")
    if effective_at is not None and not isinstance(effective_at, str):
        raise ValueError("candidate effective_at must be a string or null")
    return ResolveRequest(
        f"{uid}/c{index}",
        candidate["kind"],
        raw_domain,
        candidate["value"],
        evidence_text,
        effective_at,
    )


def _pair_requests(document: Mapping[str, Any]) -> tuple[list[ResolveRequest], list[dict[str, Any]]]:
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("pair goldset must contain cases")
    requests: list[ResolveRequest] = []
    pairs: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, Mapping) or not isinstance(case.get("case_id"), str) or not isinstance(case.get("facts"), list) or not isinstance(case.get("pairs"), list):
            raise ValueError("pair goldset case is malformed")
        case_id = case["case_id"]
        seen: set[str] = set()
        for fact in case["facts"]:
            if not isinstance(fact, Mapping) or not isinstance(fact.get("fact_id"), str) or not isinstance(fact.get("probe"), Mapping):
                raise ValueError("pair goldset fact is malformed")
            fact_id, probe = fact["fact_id"], fact["probe"]
            if fact_id in seen or not all(isinstance(probe.get(name), str) for name in ("kind", "raw_domain", "value")) or not isinstance(fact.get("content"), str):
                raise ValueError("pair goldset fact is invalid")
            seen.add(fact_id)
            requests.append(ResolveRequest(f"{case_id}/{fact_id}", probe["kind"], probe["raw_domain"], probe["value"], fact["content"], fact.get("date")))
        for pair in case["pairs"]:
            if not isinstance(pair, Mapping) or not isinstance(pair.get("earlier_id"), str) or not isinstance(pair.get("later_id"), str) or pair["earlier_id"] not in seen or pair["later_id"] not in seen:
                raise ValueError("pair goldset pair is invalid")
            pairs.append({"case_id": case_id, **dict(pair)})
    return requests, pairs


def _run_deterministic_by_uid(
    requests: Iterable[ResolveRequest], registry: SlotRegistry, ledger_cases: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Run deterministic resolution once over the universe with isolated request state."""
    engine = ResolverEngine(registry=registry)
    rows: dict[str, dict[str, Any]] = {}
    for request in requests:
        decision = engine.resolve(request, ResolverContext(()), RunBudget(LLM_LEGS_RESERVED))
        bound = decision.method in {"exact", "alias"} and decision.slot_id is not None
        target_id = decision.slot_id if bound else None
        ledger = ledger_cases.get(request.request_uid)
        retained = isinstance(ledger, Mapping) and ledger.get("hit_slot") == target_id
        rows[request.request_uid] = {
            "slot_id": target_id,
            "method": decision.method if bound else "none",
            "guard_fired": decision.guard_fired,
            "retention_ledger": retained,
        }
    return rows


def _run_requests(
    requests: Iterable[ResolveRequest], registry: SlotRegistry, deterministic_by_uid: Mapping[str, Mapping[str, Any]], *, scoring_mode: Literal["v1", "v2_instrument"], choice_set_k: int | None = None, theta: tuple[int, int] | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for request in requests:
        risk = compute_risk_evidence(request, registry)
        prepared = prepare_guard(request, registry, risk)
        kwargs: dict[str, Any] = {}
        if choice_set_k is not None:
            kwargs["choice_set_k"] = choice_set_k
        if theta is not None:
            kwargs["theta"] = theta
        blocked = assemble_blocking(
            request,
            registry,
            risk,
            guarded_target_id=prepared.target.slot_id if prepared.guard_fired and prepared.target is not None else None,
            scoring_mode=scoring_mode,
            **kwargs,
        )
        rows[request.request_uid] = {
            "request": request,
            "risk_any": risk.any,
            "receipt": blocked.receipt,
            "forced_overflow": blocked.forced_overflow,
            "det": deterministic_by_uid[request.request_uid],
        }
    return rows, {"empty": sum(not row["receipt"].choice_set for row in rows.values()), "total": len(rows)}


def _cohort_metrics(pairs: Iterable[Mapping[str, Any]], pair_rows: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, Any]]:
    cohort = {"both_det_same": 0, "det_diff": 0, "reachable": 0, "structural": 0}
    mrr_num = Fraction(0, 1)
    mrr_den = 0
    for pair in pairs:
        if pair.get("label") != "supersede":
            continue
        earlier = pair_rows[f"{pair['case_id']}/{pair['earlier_id']}"]
        later = pair_rows[f"{pair['case_id']}/{pair['later_id']}"]
        dets = [row for row in (earlier, later) if row["det"]["slot_id"] is not None and not row["det"]["guard_fired"]]
        if len(dets) == 2:
            if dets[0]["det"]["slot_id"] == dets[1]["det"]["slot_id"]:
                cohort["both_det_same"] += 1
            else:
                cohort["det_diff"] += 1
            continue
        if len(dets) == 1:
            mrr_den += 1
            anchor = dets[0]["det"]["slot_id"]
            missed = later if dets[0] is earlier else earlier
            choices = missed["receipt"].choice_set
            if anchor in choices:
                cohort["reachable"] += 1
                mrr_num += Fraction(1, choices.index(anchor) + 1)
            else:
                cohort["structural"] += 1
            continue
        if set(earlier["receipt"].choice_set) & set(later["receipt"].choice_set):
            cohort["reachable"] += 1
        else:
            cohort["structural"] += 1
    if not mrr_den:
        return cohort, {"num": 0, "den": 0, "gold_source": "det_anchor_proxy"}
    mrr = mrr_num / mrr_den
    return cohort, {"num": mrr.numerator, "den": mrr.denominator, "gold_source": "det_anchor_proxy"}


def _metrics(pair_rows: Mapping[str, Mapping[str, Any]], extraction_rows: Mapping[str, Mapping[str, Any]], pairs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    pairs = list(pairs)
    negative = [pair for pair in pairs if pair.get("label") != "supersede"]
    overlap = sum(
        bool(set(pair_rows[f"{pair['case_id']}/{pair['earlier_id']}"]["receipt"].choice_set) & set(pair_rows[f"{pair['case_id']}/{pair['later_id']}"]["receipt"].choice_set))
        for pair in negative
    )
    all_rows = [*pair_rows.values(), *extraction_rows.values()]
    cohort, mrr = _cohort_metrics(pairs, pair_rows)
    return {
        "negative_overlap": {"n": overlap, "d": len(negative)},
        "migration": {"n": sum(row["receipt"].slice == "hr" and not row["risk_any"] for row in pair_rows.values()), "d": len(pair_rows)},
        "overflow": {"n": sum(row["forced_overflow"] for row in pair_rows.values()), "d": len(pair_rows)},
        "empty_pair_face": {"n": sum(not row["receipt"].choice_set for row in pair_rows.values()), "d": len(pair_rows)},
        "empty_extraction_face": {"n": sum(not row["receipt"].choice_set for row in extraction_rows.values()), "d": len(extraction_rows)},
        "cohort": cohort,
        "det_miss_recall": {"n": cohort["reachable"], "d": cohort["reachable"] + cohort["structural"]},
        "det_anchor_mrr": mrr,
        "pool_size_distribution": {str(size): count for size, count in sorted(Counter(len(row["receipt"].choice_set) for row in all_rows).items())},
    }


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_core_metrics(metrics: Any, *, expected_cardinalities: Mapping[str, int], expected_extraction_rows: int) -> None:
    if not isinstance(metrics, Mapping) or set(metrics) != set(_CORE_METRIC_KEYS):
        raise ValueError("baseline bundle metrics are invalid")
    for name in ("negative_overlap", "migration", "overflow", "empty_pair_face", "empty_extraction_face"):
        value = metrics[name]
        if not isinstance(value, Mapping) or set(value) != {"n", "d"} or not _is_count(value["n"]) or not _is_count(value["d"]) or value["n"] > value["d"]:
            raise ValueError(f"baseline bundle {name} metric is invalid")
    if (
        metrics["negative_overlap"]["d"] != expected_cardinalities["negative_pairs"]
        or metrics["migration"]["d"] != expected_cardinalities["facts"]
        or metrics["overflow"]["d"] != expected_cardinalities["facts"]
        or metrics["empty_pair_face"]["d"] != expected_cardinalities["facts"]
        or metrics["empty_extraction_face"]["d"] != expected_extraction_rows
    ):
        raise ValueError("baseline bundle fixed pair denominator mismatch")
    cohort = metrics["cohort"]
    if not isinstance(cohort, Mapping) or set(cohort) != {"both_det_same", "det_diff", "reachable", "structural"} or not all(_is_count(value) for value in cohort.values()) or sum(cohort.values()) != expected_cardinalities["sup_pairs"]:
        raise ValueError("baseline bundle cohort metric is invalid")
    pool = metrics["pool_size_distribution"]
    if not isinstance(pool, Mapping) or not pool or not all(isinstance(key, str) and key.isdecimal() and _is_count(value) for key, value in pool.items()):
        raise ValueError("baseline bundle pool-size distribution is invalid")
    if sum(pool.values()) != expected_cardinalities["facts"] + expected_extraction_rows:
        raise ValueError("baseline bundle pool-size distribution cardinality mismatch")


def _validate_baseline_bundle(bundle: Any, *, input_hashes: Mapping[str, str], registry_hash: str, expected_cardinalities: Mapping[str, int], expected_extraction_rows: int, expected_deterministic_uids: set[str]) -> dict[str, Any]:
    if not isinstance(bundle, Mapping) or set(bundle) != {"c1_merged", "cbfd01c"}:
        raise ValueError("baseline bundle must contain exactly c1_merged and cbfd01c")
    c1 = bundle["c1_merged"]
    if not isinstance(c1, Mapping) or set(c1) != {"commit", "registry_hash", "produced_by", "description_remediation_commit", "input_hashes", "deterministic_by_uid", "metrics"}:
        raise ValueError("baseline bundle c1_merged has an invalid schema")
    if not all(isinstance(c1[name], str) and c1[name] for name in ("commit", "registry_hash", "produced_by", "description_remediation_commit")):
        raise ValueError("baseline bundle c1_merged provenance is invalid")
    if c1["registry_hash"] != registry_hash or c1["input_hashes"] != dict(input_hashes):
        raise ValueError("baseline bundle c1_merged identity mismatch")
    deterministic_by_uid = c1["deterministic_by_uid"]
    if not isinstance(deterministic_by_uid, Mapping) or set(deterministic_by_uid) != expected_deterministic_uids:
        raise ValueError("baseline bundle deterministic map is invalid")
    for row in deterministic_by_uid.values():
        if not isinstance(row, Mapping) or set(row) != {"slot_id", "method", "guard_fired", "retention_ledger"}:
            raise ValueError("baseline bundle deterministic map row is invalid")
        method, slot_id = row["method"], row["slot_id"]
        if method not in {"exact", "alias", "none"} or not isinstance(row["guard_fired"], bool) or not isinstance(row["retention_ledger"], bool):
            raise ValueError("baseline bundle deterministic map row is invalid")
        if (method == "none" and slot_id is not None) or (method != "none" and (not isinstance(slot_id, str) or not slot_id)):
            raise ValueError("baseline bundle deterministic map row is invalid")
    _validate_core_metrics(c1["metrics"], expected_cardinalities=expected_cardinalities, expected_extraction_rows=expected_extraction_rows)
    cbfd = bundle["cbfd01c"]
    if not isinstance(cbfd, Mapping) or "commit" not in cbfd or not isinstance(cbfd["commit"], str) or not cbfd["commit"]:
        raise ValueError("baseline bundle cbfd01c is invalid")
    if set(cbfd) == {"commit", "absent"}:
        if not isinstance(cbfd["absent"], str) or not cbfd["absent"]:
            raise ValueError("baseline bundle cbfd01c absent reason is invalid")
    elif set(cbfd) == {"commit", "metrics"}:
        _validate_core_metrics(cbfd["metrics"], expected_cardinalities=expected_cardinalities, expected_extraction_rows=expected_extraction_rows)
    else:
        raise ValueError("baseline bundle cbfd01c has an invalid schema")
    return dict(bundle)


def _assert_baseline_recomputed(bundle: Mapping[str, Any], recomputed: Mapping[str, Any]) -> None:
    """Refuse a sweep unless its v1 provenance record matches a fresh engine run."""
    c1 = bundle["c1_merged"]
    for name in _CORE_METRIC_KEYS:
        if c1["metrics"][name] != recomputed["metrics"][name]:
            raise ValueError(f"baseline bundle c1_merged metrics differ at {name}")
    for uid in sorted(recomputed["deterministic_by_uid"]):
        expected = recomputed["deterministic_by_uid"][uid]
        actual = c1["deterministic_by_uid"][uid]
        for field in ("slot_id", "method", "guard_fired", "retention_ledger"):
            if actual[field] != expected[field]:
                raise ValueError(f"baseline bundle c1_merged deterministic map differs at {uid}.{field}")


def build_baseline(
    inputs: DiagnosticInputs, *, registry: SlotRegistry | None = None, commit: str, produced_by: str, description_remediation_commit: str
) -> dict[str, Any]:
    """Create the c1 v1 baseline only; no v2 score helper is imported on this path."""
    pair_doc = _load_json(inputs.pair_goldset_path, label="pair goldset", expected_hash=inputs.pair_goldset_sha256)
    source_items = _load_json(inputs.extraction_source_items_path, label="extraction source items", expected_hash=inputs.extraction_source_items_sha256)
    _load_json(inputs.extraction_source_fixture_path, label="extraction source fixture", expected_hash=inputs.extraction_source_fixture_sha256)
    ledger = _load_json(inputs.retention_ledger_path, label="retention ledger", expected_hash=inputs.retention_ledger_sha256)
    if not isinstance(pair_doc, Mapping) or not isinstance(source_items, (list, Mapping)) or not isinstance(ledger, Mapping) or not isinstance(ledger.get("cases"), list):
        raise ValueError("diagnostic inputs have an invalid shape")
    pair_requests, pairs = _pair_requests(pair_doc)
    candidates = _candidate_rows(inputs)
    source_uids = {item.item_id for item in load_items(inputs.extraction_source_items_path, inputs.extraction_source_fixture_path)}
    if set(candidates) != source_uids:
        raise ValueError("candidate item set is not an exact bijection to the pinned source items")
    ledger_cases = {item.get("request_uid"): item for item in ledger["cases"] if isinstance(item, Mapping) and isinstance(item.get("request_uid"), str)}
    if len(ledger_cases) != len(ledger["cases"]):
        raise ValueError("retention ledger has invalid cases")
    extraction_requests = [_request_from_candidate(uid, index, row) for uid, rows in candidates.items() for index, row in enumerate(rows)]
    observed = {"facts": len(pair_requests), "pairs": len(pairs), "sup_pairs": sum(pair.get("label") == "supersede" for pair in pairs), "negative_pairs": sum(pair.get("label") != "supersede" for pair in pairs)}
    _require_cardinalities(observed, inputs.expected_cardinalities)
    live_registry = registry or load_registry()
    deterministic_by_uid = _run_deterministic_by_uid([*pair_requests, *extraction_requests], live_registry, ledger_cases)
    pair_rows, _ = _run_requests(pair_requests, live_registry, deterministic_by_uid, scoring_mode="v1")
    extraction_rows, _ = _run_requests(extraction_requests, live_registry, deterministic_by_uid, scoring_mode="v1")
    metrics = _metrics(pair_rows, extraction_rows, pairs)
    return {
        "commit": commit,
        "registry_hash": live_registry.content_hash,
        "produced_by": produced_by,
        "description_remediation_commit": description_remediation_commit,
        "input_hashes": inputs.input_hashes(),
        "deterministic_by_uid": dict(sorted(deterministic_by_uid.items())),
        "metrics": {name: metrics[name] for name in _CORE_METRIC_KEYS},
    }


def _rate_at_most(value: Mapping[str, int], baseline: Mapping[str, int], *, allowance_num: int, allowance_den: int) -> bool:
    n, d, bn, bd = value["n"], value["d"], baseline["n"], baseline["d"]
    return d > 0 and bd > 0 and allowance_den * n * bd <= allowance_den * bn * d + allowance_num * bd * d


def _theta_sort_key(theta: tuple[int, int]) -> Fraction:
    if len(theta) != 2 or theta[0] <= 0 or theta[1] <= 0:
        raise ValueError("theta must be a positive rational pair")
    return Fraction(theta[0], theta[1])


def evaluate_predicates(metrics: Mapping[str, Any], baseline_metrics: Mapping[str, Any], *, det_zero_delta: bool) -> dict[str, bool]:
    """Evaluate frozen sweep predicates using only integer cross multiplication."""
    negative = _rate_at_most(metrics["negative_overlap"], baseline_metrics["negative_overlap"], allowance_num=1, allowance_den=50)
    migration = _rate_at_most(metrics["migration"], baseline_metrics["migration"], allowance_num=1, allowance_den=50)
    overflow = metrics["overflow"]["d"] > 0 and metrics["overflow"]["n"] * baseline_metrics["overflow"]["d"] <= baseline_metrics["overflow"]["n"] * metrics["overflow"]["d"]
    pollution = metrics["pollution"]["d"] > 0 and 4 * metrics["pollution"]["n"] <= metrics["pollution"]["d"]
    return {"negative_overlap_ok": negative, "migration_ok": migration, "overflow_ok": overflow, "pollution_ok": pollution, "det_zero_delta_ok": det_zero_delta, "feasible": negative and migration and overflow and pollution and det_zero_delta}


def select_grid(cells: list[dict[str, Any]]) -> tuple[dict[str, int | tuple[int, int]] | None, list[str]]:
    """Select maximum recall, then smaller K, then greater θ; all comparisons are rational."""
    ordered = sorted(cells, key=lambda cell: (cell["k"], _theta_sort_key(tuple(cell["theta"]))))
    trace: list[str] = []
    viable = []
    for cell in ordered:
        predicates = cell["predicates"]
        failed = next((name for name in ("negative_overlap_ok", "migration_ok", "overflow_ok", "pollution_ok", "det_zero_delta_ok") if not predicates[name]), None)
        predicates["first_failure"] = failed
        if failed is None:
            viable.append(cell)
        else:
            trace.append(f"reject k={cell['k']} theta={cell['theta']}: {failed}")
    if not viable:
        return None, trace
    chosen = viable[0]
    for cell in viable[1:]:
        lhs, rhs = cell["metrics"]["det_miss_recall"], chosen["metrics"]["det_miss_recall"]
        better = lhs["n"] * rhs["d"] > rhs["n"] * lhs["d"] if lhs["d"] and rhs["d"] else lhs["d"] > rhs["d"]
        tied = lhs["n"] * rhs["d"] == rhs["n"] * lhs["d"] if lhs["d"] and rhs["d"] else lhs["d"] == rhs["d"]
        if better or (tied and (cell["k"] < chosen["k"] or (cell["k"] == chosen["k"] and _theta_sort_key(tuple(cell["theta"])) > _theta_sort_key(tuple(chosen["theta"]))))):
            chosen = cell
    trace.append(f"select k={chosen['k']} theta={chosen['theta']}")
    return {"k": chosen["k"], "theta": tuple(chosen["theta"])}, trace


def sweep_document(*, baseline_bundle: Mapping[str, Any], baseline_bundle_sha256: str, input_hashes: Mapping[str, str], cells: list[dict[str, Any]], produced_at_commit: str) -> dict[str, Any]:
    """Materialize the frozen v2b schema from already measured hermetic or chief-run cells."""
    if not isinstance(baseline_bundle_sha256, str) or len(baseline_bundle_sha256) != 64 or any(char not in "0123456789abcdef" for char in baseline_bundle_sha256):
        raise ValueError("sweep baseline bundle SHA-256 is invalid")
    for cell in cells:
        k, theta = cell.get("k"), cell.get("theta")
        if not isinstance(k, int) or isinstance(k, bool) or not isinstance(theta, list) or len(theta) != 2:
            raise ValueError("sweep cell has an invalid instrument grid")
        expected_hash = blocking_v2_instrument_rules_hash_for(k, tuple(theta))
        if cell.get("v2_instrument_rules_hash") != expected_hash:
            raise ValueError("sweep cell v2 instrument rules hash does not match its grid")
    selected, trace = select_grid(cells)
    return {"schema_version": "v2b-sweep-3", "v1_operative_rules_hash": blocking_v1_operative_rules_hash(), "produced_at_commit": produced_at_commit, "baseline_recomputed": True, "baseline_bundle_sha256": baseline_bundle_sha256, "input_hashes": dict(input_hashes), "cells": sorted(cells, key=lambda cell: (cell["k"], _theta_sort_key(tuple(cell["theta"])))), "selected_grid": selected, "selection_trace": trace}


def build_sweep_cells(inputs: DiagnosticInputs, baseline_bundle: Mapping[str, Any], *, registry: SlotRegistry | None = None) -> list[dict[str, Any]]:
    """Run the frozen v2 grid.  The score-v2 import is intentionally isolated here.

    This route is unavailable until P2 supplies score_components; baseline generation
    above therefore remains executable against the shipped v1 blocker.
    """
    from panella.resolver.blocking import score_components  # P2-only function-level dependency.

    live_registry = registry or load_registry()
    pair_doc = _load_json(inputs.pair_goldset_path, label="pair goldset", expected_hash=inputs.pair_goldset_sha256)
    ledger = _load_json(inputs.retention_ledger_path, label="retention ledger", expected_hash=inputs.retention_ledger_sha256)
    if not isinstance(pair_doc, Mapping) or not isinstance(ledger, Mapping) or not isinstance(ledger.get("cases"), list):
        raise ValueError("diagnostic inputs have an invalid shape")
    pair_requests, pairs = _pair_requests(pair_doc)
    candidates = _candidate_rows(inputs)
    source_uids = {item.item_id for item in load_items(inputs.extraction_source_items_path, inputs.extraction_source_fixture_path)}
    if set(candidates) != source_uids:
        raise ValueError("candidate item set is not an exact bijection to the pinned source items")
    extraction_requests = [_request_from_candidate(uid, index, row) for uid, rows in candidates.items() for index, row in enumerate(rows)]
    observed = {"facts": len(pair_requests), "pairs": len(pairs), "sup_pairs": sum(pair.get("label") == "supersede" for pair in pairs), "negative_pairs": sum(pair.get("label") != "supersede" for pair in pairs)}
    _require_cardinalities(observed, inputs.expected_cardinalities)
    recomputed = build_baseline(
        inputs,
        registry=live_registry,
        commit="sweep-recompute",
        produced_by="sweep",
        description_remediation_commit="sweep-recompute",
    )
    deterministic_by_uid = recomputed["deterministic_by_uid"]
    bundle = _validate_baseline_bundle(
        baseline_bundle,
        input_hashes=inputs.input_hashes(),
        registry_hash=live_registry.content_hash,
        expected_cardinalities=inputs.expected_cardinalities,
        expected_extraction_rows=len(extraction_requests),
        expected_deterministic_uids=set(deterministic_by_uid),
    )
    _assert_baseline_recomputed(bundle, recomputed)
    cells: list[dict[str, Any]] = []
    for k in (8, 12, 16):
        for theta in sorted(((3, 20), (1, 5), (3, 10)), key=_theta_sort_key):
            pair_rows, _ = _run_requests(pair_requests, live_registry, deterministic_by_uid, scoring_mode="v2_instrument", choice_set_k=k, theta=theta)
            extraction_rows, _ = _run_requests(extraction_requests, live_registry, deterministic_by_uid, scoring_mode="v2_instrument", choice_set_k=k, theta=theta)
            metrics = _metrics(pair_rows, extraction_rows, pairs)
            pollution_n = pollution_d = 0
            for row in [*pair_rows.values(), *extraction_rows.values()]:
                receipt = row["receipt"]
                forced_ids = receipt.forced_ids
                cand_tokens = row.get("candidate_tokens", None)
                if cand_tokens is None:
                    from panella.resolver.blocking import request_candidate_tokens
                    cand_tokens = request_candidate_tokens(row["request"])
                for slot_id in receipt.choice_set[len(forced_ids):]:
                    pollution_d += 1
                    if score_components(live_registry.by_id[slot_id], cand_tokens)[0] == 0:
                        pollution_n += 1
            metrics["pollution"] = {"n": pollution_n, "d": pollution_d}
            predicates = evaluate_predicates(metrics, bundle["c1_merged"]["metrics"], det_zero_delta=deterministic_by_uid == bundle["c1_merged"]["deterministic_by_uid"])
            cells.append({"k": k, "theta": list(theta), "v2_instrument_rules_hash": blocking_v2_instrument_rules_hash_for(k, theta), "metrics": metrics, "predicates": predicates})
    return cells


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--baseline-out", type=Path)
    parser.add_argument("--baseline-bundle", type=Path)
    parser.add_argument("--baseline-bundle-sha256")
    parser.add_argument("--sweep-out", type=Path)
    parser.add_argument("--produced-by")
    parser.add_argument("--commit")
    parser.add_argument("--description-remediation-commit")
    args = parser.parse_args(argv)
    if args.sweep_out is not None:
        if args.candidates is None or args.baseline_bundle is None or args.baseline_bundle_sha256 is None or not args.commit:
            raise SystemExit("--sweep-out requires --candidates, --baseline-bundle, --baseline-bundle-sha256, and --commit")
        inputs = DiagnosticInputs(PAIR_GOLDSET, PAIR_GOLDSET_SHA256, args.candidates, CANDIDATE_HASH_ALLOWLIST, EXTRACTION_SOURCES["source_items"], _sha256(EXTRACTION_SOURCES["source_items"]), EXTRACTION_SOURCES["source_fixture"], _sha256(EXTRACTION_SOURCES["source_fixture"]), LEDGER_PATH, _sha256(LEDGER_PATH), args.sweep_out.parent, _PRODUCTION_CARDINALITIES)
        raw_bundle = args.baseline_bundle.read_bytes()
        actual_bundle_sha256 = hashlib.sha256(raw_bundle).hexdigest()
        if actual_bundle_sha256 != args.baseline_bundle_sha256:
            raise ValueError(
                "baseline bundle SHA-256 mismatch: "
                f"expected={args.baseline_bundle_sha256[:12]} actual={actual_bundle_sha256[:12]}"
            )
        bundle = json.loads(raw_bundle)
        cells = build_sweep_cells(inputs, bundle)
        document = sweep_document(baseline_bundle=bundle, baseline_bundle_sha256=actual_bundle_sha256, input_hashes=inputs.input_hashes(), cells=cells, produced_at_commit=args.commit)
        args.sweep_out.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(args.sweep_out), "selected_grid": document["selected_grid"]}, separators=(",", ":")))
        return 0 if document["selected_grid"] is not None else 1
    if args.baseline_out is None:
        if args.candidates is not None:
            _load_candidates(args.candidates)
        report, _ = _run_pair()
        unresolved = report["pair_classification"].get("unresolved_semantic", 0) > 0
        retention = report["det"]["retention"]
        passed = not unresolved and retention["pass"] and retention["approved_remap_eliminated"]
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / "resolver_blocking_diag_v2a.json"
        out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(out), "pass": passed}, separators=(",", ":")))
        return 0 if passed else 1
    if args.candidates is None or not all((args.produced_by, args.commit, args.description_remediation_commit)):
        raise SystemExit("--baseline-out requires --candidates, --produced-by, --commit, and --description-remediation-commit")
    inputs = DiagnosticInputs(PAIR_GOLDSET, PAIR_GOLDSET_SHA256, args.candidates, CANDIDATE_HASH_ALLOWLIST, EXTRACTION_SOURCES["source_items"], _sha256(EXTRACTION_SOURCES["source_items"]), EXTRACTION_SOURCES["source_fixture"], _sha256(EXTRACTION_SOURCES["source_fixture"]), LEDGER_PATH, _sha256(LEDGER_PATH), args.baseline_out.parent, _PRODUCTION_CARDINALITIES)
    baseline = build_baseline(inputs, commit=args.commit, produced_by=args.produced_by, description_remediation_commit=args.description_remediation_commit)
    args.baseline_out.write_text(json.dumps({"c1_merged": baseline}, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.baseline_out)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
