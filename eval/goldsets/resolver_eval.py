"""Offline dual-face resolver harness: pair simulation and extraction adaptation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any

from eval.goldsets.key_correctness_eval import GoldItem, high_risk_value_match, value_match
from eval.goldsets.preference_extraction import PreferenceCandidate
from eval.goldsets.score_supersede import score as score_supersede
from panella.resolver.engine import ResolverEngine
from panella.resolver.types import ExistingSlot, ResolveRequest, ResolverContext, RunBudget, split_slot_id


def pair_face(goldset: dict[str, Any], engine: ResolverEngine) -> dict[str, Any]:
    """Resolve each case chronologically and apply the frozen derived classifier."""
    predictions: list[dict[str, str]] = []
    decisions: dict[tuple[str, str], Any] = {}
    budget = RunBudget(sum(len(case["facts"]) for case in goldset["cases"]))
    for case in goldset["cases"]:
        existing: list[ExistingSlot] = []
        facts = sorted(case["facts"], key=lambda fact: (fact["date"], fact["fact_id"]))
        for fact in facts:
            probe = fact["probe"]
            decision = engine.resolve(
                ResolveRequest(f"{case['case_id']}/{fact['fact_id']}", probe["kind"], probe["raw_domain"], probe["value"], fact["content"], fact["date"]),
                ResolverContext(tuple(existing)), budget,
            )
            decisions[(case["case_id"], fact["fact_id"])] = decision
            if decision.action in {"BIND", "ADD"}:
                existing.append(ExistingSlot(decision.slot_id or "", fact["date"]))
        for pair in case["pairs"]:
            first, second = decisions[(case["case_id"], pair["earlier_id"])], decisions[(case["case_id"], pair["later_id"])]
            label = "supersede" if first.action != "ABSTAIN_ADD" and second.action != "ABSTAIN_ADD" and first.slot_id == second.slot_id else "unrelated"
            predictions.append({"case_id": case["case_id"], "earlier_id": pair["earlier_id"], "later_id": pair["later_id"], "predicted_label": label})
    expected = {(case["case_id"], pair["earlier_id"], pair["later_id"]) for case in goldset["cases"] for pair in case["pairs"]}
    actual = {(row["case_id"], row["earlier_id"], row["later_id"]) for row in predictions}
    if actual != expected or len(actual) != len(predictions):
        raise ValueError("pair predictions are not a bijection to gold pairs")
    report = score_supersede(goldset, predictions).to_dict()
    return {"predictions": predictions, "report": report, "decisions": decisions, "n_llm_calls": budget.calls_made}


def reduce_item(item: GoldItem, candidates: list[PreferenceCandidate], decisions: list[Any]) -> tuple[str, int, int]:
    """Return §7.4a-r category, grounded candidate count, and wrong-bind count."""
    if not candidates:
        return "extraction_miss", 0, 0
    match = high_risk_value_match if item.high_risk else value_match
    grounded = [(candidate, decision) for candidate, decision in zip(candidates, decisions, strict=True) if item.gold_value and match(item.gold_value, candidate.value)]
    if not grounded:
        return "no_grounded", 0, 0
    wrong = sum(1 for _, decision in grounded if decision.action in {"BIND", "ADD"} and decision.slot_id != item.gold_key)
    if wrong:
        return "mixed_wrong_bind", len(grounded), wrong
    correct = any(decision.action in {"BIND", "ADD"} and decision.slot_id == item.gold_key for _, decision in grounded)
    return ("correct" if correct else "wrong_slot"), len(grounded), 0


def extraction_face(items: list[GoldItem], extracted: dict[str, list[PreferenceCandidate]], engine: ResolverEngine) -> dict[str, Any]:
    """Adapt candidate keys through resolver decisions without mutating candidate properties."""
    contexts: dict[str, list[ExistingSlot]] = defaultdict(list)
    budget = RunBudget(sum(len(value) for value in extracted.values()))
    categories: dict[str, str] = {}
    adapted: dict[str, list[PreferenceCandidate]] = {}
    wrong_bind_count = 0
    abstention = Counter()
    for item in items:
        candidates = extracted.get(item.item_id, [])
        result: list[PreferenceCandidate] = []
        item_decisions: list[Any] = []
        lifecycle = item.lifecycle or item.item_id
        for index, candidate in enumerate(candidates):
            decision = engine.resolve(
                ResolveRequest(f"{item.item_id}/c{index}", candidate.kind, candidate.domain, candidate.value, candidate.evidence, item.effective_at),
                ResolverContext(tuple(contexts[lifecycle])), budget,
            )
            item_decisions.append(decision)
            if decision.action in {"BIND", "ADD"}:
                kind, domain = split_slot_id(decision.slot_id or "")
                result.append(replace(candidate, kind=kind, domain=domain))
                contexts[lifecycle].append(ExistingSlot(decision.slot_id or "", item.effective_at))
            else:
                result.append(replace(candidate, domain=decision.unresolved_domain or candidate.domain))
            abstention[("hr" if item.high_risk else "benign", decision.method, decision.fallback_outcome)] += int(decision.action == "ABSTAIN_ADD")
        category, _, wrong = reduce_item(item, candidates, item_decisions)
        categories[item.item_id] = category
        wrong_bind_count += wrong
        adapted[item.item_id] = result
    counts = Counter(categories.values())
    return {
        "adapted": adapted,
        "categories": categories,
        "category_counts": dict(counts),
        "candidate_wrong_bind_count": wrong_bind_count,
        "abstention_by_slice_method_outcome": {"|".join(key): value for key, value in abstention.items()},
        "n_llm_calls": budget.calls_made,
    }
