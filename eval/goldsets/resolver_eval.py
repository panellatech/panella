"""Offline dual-face resolver harness: pair simulation and extraction adaptation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from eval.goldsets import key_correctness_eval
from eval.goldsets.key_correctness_eval import GoldItem, high_risk_value_match, value_match
from eval.goldsets.preference_extraction import ChatFn, PreferenceCandidate, extract_preferences
from eval.goldsets.resolver_gate import _sealed_files
from eval.goldsets.score_supersede import score as score_supersede
from panella.resolver.calibrate import load_manifest
from panella.resolver.engine import ResolverEngine
from panella.resolver.fallback import FallbackProvider
from panella.resolver.types import ExistingSlot, ResolveRequest, ResolverConfig, ResolverContext, RunBudget, split_slot_id

ROOT = Path(__file__).resolve().parents[2]

_SCORE_METRIC_NAMES = (
    "extraction_recall",
    "value_match_rate",
    "key_correctness",
    "key_stability",
    "supersede_precision",
    "hr_supersede_precision",
    "high_risk_recall",
    "high_risk_value_recall",
    "high_risk_slot_recall",
    "high_risk_key_correctness",
    "high_risk_update_pairs",
    "high_risk_collisions",
    "harmful_collisions",
    "negative_false_positive_rate",
    "negative_colliding_keys",
    "schema_validity",
)


def _required_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in value:
        raise ValueError(f"missing config key: {key}")
    nested = value[key]
    if not isinstance(nested, dict):
        raise ValueError(f"config key must be a dict: {key}")
    return nested


def _required_string(value: dict[str, Any], key: str, *, parent: str) -> str:
    if key not in value:
        raise ValueError(f"missing config key: {parent}.{key}")
    item = value[key]
    if not isinstance(item, str) or not item:
        raise ValueError(f"config key must be a non-empty string: {parent}.{key}")
    return item


def _require_file(path: Path, *, name: str) -> Path:
    if not path.is_file():
        raise ValueError(f"required file is missing: {name}")
    return path


def _repo_file(value: str, *, key: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise ValueError(f"config path must be relative to the repository root: {key}")
    return _require_file(ROOT / path, name=key)


def _holdout_file(value: str, *, key: str, sealed: dict[str, Path]) -> Path:
    path = Path(value)
    if path.name != value or value in {"", ".", ".."}:
        raise ValueError(f"config holdout file must be a bare filename: {key}")
    try:
        return sealed[value]
    except KeyError as exc:
        raise ValueError(f"holdout file is not listed in SHA256SUMS: {value}") from exc


def _extraction_score_report(items: list[GoldItem], extracted: dict[str, list[PreferenceCandidate]], parse_stats: dict[str, dict[str, int]]) -> dict[str, Any]:
    score_report = key_correctness_eval.score(items, extracted, parse_stats=parse_stats)
    report = {name: getattr(score_report, name) for name in _SCORE_METRIC_NAMES}
    report.update(
        {
            "key_stability_correct": sum(row["merged_pairs"] for row in score_report.per_lifecycle),
            "key_stability_total": sum(row["gold_pairs"] for row in score_report.per_lifecycle),
            "hr_merged_pairs_zero": score_report.counts["hr_merged_pairs"] == 0,
            "high_risk_supersede_proven": score_report.high_risk_supersede_proven,
            "counts": score_report.counts,
        }
    )
    return report


def make_gate_evaluator(
    *,
    holdout_sums: Path,
    provenance: Path,
    holdout_counts: Path,
    probe_path: Path | str,
    manifest_path: Path | None,
    evidence_path: Path | None,
    config: dict[str, Any],
    chat_fn: ChatFn | None = None,
) -> Callable[[], dict[str, Any]]:
    """Build the zero-argument evaluator pinned by a K1 gate configuration."""
    if not isinstance(config, dict):
        raise ValueError("config must be a dict")
    public = _required_mapping(config, "public")
    holdout_files = _required_mapping(config, "holdout_files")
    frozen = _required_mapping(config, "frozen")
    targets = _required_mapping(config, "run_validity_targets")
    chat = _required_mapping(config, "chat")
    llm_enabled = config.get("llm_enabled")
    if "llm_enabled" not in config:
        raise ValueError("missing config key: llm_enabled")
    if not isinstance(llm_enabled, bool):
        raise ValueError("config key must be bool: llm_enabled")
    timeout_ms = config.get("timeout_ms")
    if "timeout_ms" not in config:
        raise ValueError("missing config key: timeout_ms")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise ValueError("config key must be an int: timeout_ms")

    public_pairs = _repo_file(_required_string(public, "pairs_goldset", parent="public"), key="public.pairs_goldset")
    public_items = _repo_file(_required_string(public, "items_goldset", parent="public"), key="public.items_goldset")
    public_fixture = _repo_file(_required_string(public, "fixture", parent="public"), key="public.fixture")
    sealed_entries = _sealed_files(_require_file(Path(holdout_sums), name="holdout_sums"))
    sealed = {str(path.relative_to(holdout_sums.parent)): path for _, path in sealed_entries}
    if len(sealed) != len(sealed_entries):
        raise ValueError("SHA256SUMS must not contain duplicate holdout filenames")
    holdout_pairs = _holdout_file(_required_string(holdout_files, "pairs", parent="holdout_files"), key="holdout_files.pairs", sealed=sealed)
    holdout_items = _holdout_file(_required_string(holdout_files, "items_goldset", parent="holdout_files"), key="holdout_files.items_goldset", sealed=sealed)
    holdout_fixture = _holdout_file(_required_string(holdout_files, "fixture", parent="holdout_files"), key="holdout_files.fixture", sealed=sealed)
    for path, name in ((holdout_pairs, "holdout_files.pairs"), (holdout_items, "holdout_files.items_goldset"), (holdout_fixture, "holdout_files.fixture")):
        _require_file(path, name=name)
    _require_file(Path(provenance), name="provenance")
    _require_file(Path(holdout_counts), name="holdout_counts")
    _require_file(Path(probe_path), name="probe_path")

    for name in targets:
        if name not in _SCORE_METRIC_NAMES:
            valid = ", ".join(_SCORE_METRIC_NAMES)
            raise ValueError(f"run_validity_targets.{name} is not a score field; valid names: {valid}")
    model = chat.get("model")
    if "model" not in chat:
        raise ValueError("missing config key: chat.model")
    if model is not None and not isinstance(model, str):
        raise ValueError("config key must be str or null: chat.model")
    timeout_s = chat.get("timeout_s")
    if "timeout_s" not in chat:
        raise ValueError("missing config key: chat.timeout_s")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise ValueError("config key must be a number: chat.timeout_s")
    retries = chat.get("retries")
    if "retries" not in chat:
        raise ValueError("missing config key: chat.retries")
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise ValueError("config key must be an int: chat.retries")

    transport = chat_fn or key_correctness_eval._codex_chat_fn(model=model, timeout=float(timeout_s), retries=retries)
    manifest = manifest_digest = evidence_digest = None
    if llm_enabled:
        if manifest_path is None:
            raise ValueError("llm_enabled requires manifest_path")
        if evidence_path is None:
            raise ValueError("llm_enabled requires evidence_path")
        manifest, manifest_digest = load_manifest(_require_file(manifest_path, name="manifest_path"))
        evidence_digest = hashlib.sha256(_require_file(evidence_path, name="evidence_path").read_bytes()).hexdigest()

    def new_engine(chat: ChatFn) -> ResolverEngine:
        if not llm_enabled:
            return ResolverEngine(ResolverConfig(False, timeout_ms, None, None, None))
        assert manifest is not None and manifest_digest is not None and evidence_digest is not None
        return ResolverEngine(
            ResolverConfig(True, timeout_ms, manifest, manifest_digest, evidence_digest),
            provider=FallbackProvider(chat, model_id=manifest.model_id),
        )

    def evaluate_split(pairs_path: Path, items_path: Path, fixture_path: Path, *, chat: ChatFn) -> tuple[dict[str, Any], dict[str, Any]]:
        pair_result = pair_face(json.loads(pairs_path.read_text(encoding="utf-8")), new_engine(chat))
        items = key_correctness_eval.load_items(items_path, fixture_path)
        extracted: dict[str, list[PreferenceCandidate]] = {}
        parse_stats: dict[str, dict[str, int]] = {}
        for item in items:
            stats: dict[str, int] = {}
            extracted[item.item_id] = extract_preferences(item.text, item.item_id, chat_fn=chat, stats=stats)
            parse_stats[item.item_id] = stats
        extraction_result = extraction_face(items, extracted, new_engine(chat))
        extraction_result.update(_extraction_score_report(items, extracted, parse_stats))
        return pair_result, extraction_result

    def evaluator() -> dict[str, Any]:
        """Run both faces over both splits.

        n_llm_calls counts PHYSICAL transport-boundary invocations: extractor calls and
        resolver fallback attempts (including suggest-level retries) all pass through the
        same counting wrapper. Retries inside the injected ChatFn itself (e.g. the codex
        subprocess transport's internal retries) are below this boundary by design.
        """
        transport_calls = 0

        def counting_chat(system: str, user: str) -> str:
            nonlocal transport_calls
            transport_calls += 1
            return transport(system, user)

        public_pair, public_extraction = evaluate_split(public_pairs, public_items, public_fixture, chat=counting_chat)
        holdout_pair, holdout_extraction = evaluate_split(holdout_pairs, holdout_items, holdout_fixture, chat=counting_chat)
        observed = {name: public_extraction[name] for name in targets}
        return {
            "pair_report": {"public": public_pair["report"], "holdout": holdout_pair["report"]},
            "extraction_report": {"public": public_extraction, "holdout": holdout_extraction},
            "frozen": frozen,
            "run_validity": {"observed": observed, "targets": targets},
            "n_llm_calls": transport_calls,
        }

    return evaluator


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
            # Spec-fixed derivation (K1 spec §7.2): same slot + both bound => supersede,
            # otherwise unrelated. K1 NEVER predicts coexist — coexist/restatement semantics
            # are an explicit K1 non-goal deferred to K2 (spec §1, §9.3). Gold coexist pairs
            # landing in confusion[coexist][unrelated] is the intended safe direction: they
            # count toward no false-merge bar, coexist recall is not a K1 gate metric, and
            # predicting coexist for same-aspect distinct-slot pairs would itself violate
            # the G2 bar (gold unrelated predicted coexist must stay zero).
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
    abstention_items: dict[str, Counter[str]] = {
        "overall": Counter(), "benign": Counter(), "hr": Counter(),
    }
    category_counts_by_slice: dict[str, Counter[str]] = {"benign": Counter(), "hr": Counter()}
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
        if item.should_extract:
            slice_name = "hr" if item.high_risk else "benign"
            category_counts_by_slice[slice_name][category] += 1
            match = high_risk_value_match if item.high_risk else value_match
            grounded_decisions = [
                decision for candidate, decision in zip(candidates, item_decisions, strict=True)
                if item.gold_value and match(item.gold_value, candidate.value)
            ]
            if grounded_decisions:
                for name in ("overall", slice_name):
                    abstention_items[name]["eligible"] += 1
                    abstention_items[name]["abstained"] += int(
                        all(decision.action == "ABSTAIN_ADD" for decision in grounded_decisions)
                    )
    counts = Counter(categories.values())
    abstention_rates = {
        name: (values["abstained"] / values["eligible"] if values["eligible"] else 0.0)
        for name, values in abstention_items.items()
    }
    return {
        "adapted": adapted,
        "categories": categories,
        "category_counts": dict(counts),
        "candidate_wrong_bind_count": wrong_bind_count,
        "category_counts_by_slice": {name: dict(values) for name, values in category_counts_by_slice.items()},
        "abstention_by_slice_method_outcome": {"|".join(key): value for key, value in abstention.items()},
        "abstention_rates": abstention_rates,
        "abstention_item_counts": {name: dict(values) for name, values in abstention_items.items()},
        "n_items": len(items),
        "n_llm_calls": budget.calls_made,
    }
