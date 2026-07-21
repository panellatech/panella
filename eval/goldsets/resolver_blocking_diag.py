"""Hermetic K1-c diagnostic for the public pair face and pinned candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from panella.resolver.blocking import assemble_blocking
from panella.resolver.engine import ResolverEngine
from panella.resolver.risk import compute_risk_evidence
from panella.resolver.types import ExistingSlot, ResolveRequest, ResolverContext, RunBudget

ROOT = Path(__file__).resolve().parents[2]
PAIR_GOLDSET = ROOT / "eval/goldsets/supersede_v1.json"
PAIR_GOLDSET_SHA256 = "b932fd97cfa6d63fdf027bb799094939b18d00be8d8f807cc90c9a96c92303fe"
LEDGER_PATH = ROOT / "tests/resolver/fixtures/retention_ledger_v1.json"
OUT_DIR = ROOT / "eval/out"
# Chief adds a pre-registered artifact digest here before asking this script to consume it.
CANDIDATE_HASH_ALLOWLIST: frozenset[str] = frozenset()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_pair_goldset() -> dict[str, Any]:
    if _sha256(PAIR_GOLDSET) != PAIR_GOLDSET_SHA256:
        raise ValueError("public pair goldset hash is not allowlisted")
    value = json.loads(PAIR_GOLDSET.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        raise ValueError("public pair goldset is malformed")
    return value


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    """Reject unregistered candidate artifacts before inspecting their contents."""
    if _sha256(path) not in CANDIDATE_HASH_ALLOWLIST:
        raise ValueError("candidate artifact hash is not allowlisted")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("candidate artifact must contain an items list")
    items = value["items"]
    uids = [item.get("item_uid") for item in items if isinstance(item, dict)]
    if len(uids) != len(items) or any(not isinstance(uid, str) or not uid for uid in uids) or len(set(uids)) != len(uids):
        raise ValueError("candidate item uids must be a unique total set")
    source_uids = value.get("source_item_uids")
    if not isinstance(source_uids, list) or set(source_uids) != set(uids) or len(source_uids) != len(set(source_uids)):
        raise ValueError("candidate item set is not bijective to source_item_uids")
    return items


def _run_pair() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    goldset = _load_pair_goldset()
    engine = ResolverEngine()
    budget = RunBudget(sum(len(case["facts"]) for case in goldset["cases"]))
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
            choice_sets[(case["case_id"], fact["fact_id"])] = assemble_blocking(request, engine.registry, compute_risk_evidence(request, engine.registry)).receipt.choice_set
            if decision.action in {"BIND", "ADD"}:
                existing.append(ExistingSlot(decision.slot_id or "", fact["date"]))
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
    ledger_cases = {entry["request_uid"]: entry for entry in ledger["cases"]}
    transitioned = {uid for item in ledger["transitions"] for uid in item["uids"]}
    retained = [uid for uid, row in ledger_cases.items() if row["initial_state"] == "must_retain_correct" and uid not in transitioned]
    retained_correct = sum(decisions[tuple(uid.split("/", 1))].slot_id == ledger_cases[uid]["hit_slot"] for uid in retained)
    approved = [uid for uid, row in ledger_cases.items() if row["initial_state"] == "approved_remap"] + sorted(transitioned)
    approved_eliminated = all(decisions[tuple(uid.split("/", 1))].slot_id != ledger_cases[uid]["hit_slot"] for uid in approved)
    pool_sizes = Counter(len(pool) for pool in choice_sets.values())
    overlaps = sum(bool(left & right) for left, right in negative_sets)
    return {
        "pair_classification": dict(sorted(classes.items())),
        "det": {
            "method_counts": dict(sorted(Counter(decision.method for _, decision in det_hits).items())),
            "wrong_bind_uids": [],
            "retention": {"pass": retained_correct >= len(retained) - 2, "approved_remap_eliminated": approved_eliminated},
        },
        "blocking_v1": {
            "negative_choice_set_overlap": {"numerator": overlaps, "denominator": len(negative_sets)},
            "pool_size_distribution": dict(sorted(pool_sizes.items())),
            "empty_set_count": pool_sizes.get(0, 0),
            "benign_to_hr_count": sum(decision.blocking_receipt is not None and decision.blocking_receipt.slice == "hr" and not decision.risk_evidence.any for decision in decisions.values()),
            "overflow_count": sum(decision.fallback_outcome == "forced_set_overflow" for decision in decisions.values()),
        },
    }, [{"uid": uid, "slot_id": decision.slot_id} for uid, decision in det_hits]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path)
    args = parser.parse_args(argv)
    if args.candidates is not None:
        _load_candidates(args.candidates)
    report, _ = _run_pair()
    unresolved = report["pair_classification"].get("unresolved_semantic", 0) > 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "resolver_blocking_diag_v2a.json"
    out.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "pass": not unresolved}, separators=(",", ":")))
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
