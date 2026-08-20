from __future__ import annotations

import json
from dataclasses import replace

import pytest
import yaml

from panella.resolver import blocking
from panella.resolver import normalize
from panella.resolver.blocking_constants import BLOCKING_STOPWORDS_NORMALIZED, SCORING_DROP
from panella.resolver.blocking import (
    BLOCKING_RULES_HASH,
    MAX_FORCED,
    THETA,
    V2_INSTRUMENT_SEED_K,
    assemble_blocking,
    blocking_v1_operative_rules_canonical,
    blocking_v1_operative_rules_hash,
    blocking_v2_instrument_rules_canonical,
    blocking_v2_instrument_rules_hash_for,
    request_candidate_tokens,
    score_components,
)
from panella.resolver.registry import default_registry_path, load_registry
from panella.resolver.risk import compute_risk_evidence
from panella.resolver.types import ResolveRequest, RiskEvidence


def request(uid: str, raw_domain: str = "unknown", value: str = "", evidence: str = "") -> ResolveRequest:
    return ResolveRequest(uid, "fact", raw_domain, value, evidence)


def test_reference_vectors_and_canonical_hash_sensitivity() -> None:
    slot = replace(load_registry().slots[0], domain="stream", aliases=(), description="", blocking_terms=())
    assert score_components(slot, {"streaming"})[1:] == (10, 15)
    note = replace(slot, domain="notetaking", aliases=(), description="")
    assert score_components(note, {"note"})[1:] == (6, 14)
    assert score_components(replace(slot, domain="cat"), {"dog"})[1:] == (0, 6)
    assert '"choice_set_k":8' in blocking_v2_instrument_rules_canonical(8, (3, 20))
    assert blocking_v2_instrument_rules_hash_for(8, (3, 20)) != blocking_v2_instrument_rules_hash_for(12, (3, 20))
    assert blocking_v2_instrument_rules_hash_for(8, (3, 20)) != blocking_v2_instrument_rules_hash_for(8, (1, 5))
    assert blocking_v1_operative_rules_hash() == BLOCKING_RULES_HASH
    assert blocking_v1_operative_rules_hash() != blocking_v2_instrument_rules_hash_for(V2_INSTRUMENT_SEED_K, THETA)
    assert json.loads(blocking_v1_operative_rules_canonical()) == {
        "choice_set": {"limit": {"base": 8, "subtract": "forced_count"}, "ranked": {"eligible": "score_gt_zero", "order": ["score_desc", "slot_id_asc"]}},
        "forced": {"deduplicate": True, "order": "slot_id_asc", "prefix": True, "sources": ["risk_matched_hr_slot_ids", "guarded_target_id"]},
        "overflow": {"if_forced_count_gt": 8, "result": {"choices": "empty", "forced_overflow": True, "receipt_choice_set": "forced_tuple", "receipt_forced_ids": "forced_tuple"}},
        "scoring": {"candidate_surfaces": ["raw_domain", "value", "evidence_text"], "eligibility": "score_gt_zero", "weights": [3, 2, 1]},
        "slice": {"hr_if_any": ["risk_evidence_any", "choice_set_has_high_risk"]},
        "tokens": {"blocking_terms": False, "normalizer": "resolver_normalize", "normalizer_rules_hash": normalize.compute_normalizer_rules_hash(), "split": "underscore", "stopwords": False, "trigram": False},
    }
    v2_hash = blocking_v2_instrument_rules_hash_for(8, (3, 20))
    assert len(v2_hash) == 64 and set(v2_hash) <= set("0123456789abcdef")
    assert v2_hash != BLOCKING_RULES_HASH


@pytest.mark.parametrize(
    ("name", "value"),
    (("V1_CHOICE_SET_K", 7), ("MAX_FORCED", 7), ("V1_WEIGHTS", (4, 2, 1))),
)
def test_v1_operative_canonical_is_sensitive_to_live_constants(monkeypatch, name: str, value: object) -> None:
    original = blocking_v1_operative_rules_canonical()
    monkeypatch.setattr(blocking, name, value)
    assert blocking_v1_operative_rules_canonical() != original


def test_instrument_and_v1_canonicals_bind_live_normalizer_and_drop_rules(monkeypatch) -> None:
    v2_original = blocking_v2_instrument_rules_canonical(8, (3, 20))
    v1_original = blocking_v1_operative_rules_canonical()

    monkeypatch.setattr(normalize, "STOPWORDS", normalize.STOPWORDS | {"normalizer_mutation"})

    assert blocking_v2_instrument_rules_canonical(8, (3, 20)) != v2_original
    assert blocking_v1_operative_rules_canonical() != v1_original
    assert json.loads(blocking_v2_instrument_rules_canonical(8, (3, 20)))["normalizer_rules_hash"] == normalize.compute_normalizer_rules_hash()

    normalizer_mutated = blocking_v2_instrument_rules_canonical(8, (3, 20))
    monkeypatch.setattr(
        blocking,
        "BLOCKING_STOPWORDS_NORMALIZED",
        blocking.BLOCKING_STOPWORDS_NORMALIZED | {"blocking_mutation"},
    )

    canonical = json.loads(blocking_v2_instrument_rules_canonical(8, (3, 20)))
    assert blocking_v2_instrument_rules_canonical(8, (3, 20)) != normalizer_mutated
    assert canonical["scoring_drop"] == sorted(blocking.SCORING_DROP)
    assert canonical["surfaces"] == {
        "l1": "domain - scoring_drop",
        "l2": "aliases - scoring_drop",
        "l3": "(description - scoring_drop) ∪ blocking_terms",
        "cand": "raw_domain|value|evidence - blocking_stopwords_normalized",
    }


def test_v1_default_locks_original_surface_scoring_and_ordering() -> None:
    registry = load_registry()
    result = assemble_blocking(request("v1-lock", "streaming", "", ""), registry, RiskEvidence((), False, False))
    assert result.receipt.choice_set == ("preference:streaming_subscription",)
    assert result.receipt.forced_ids == ()


def test_scoring_mode_parameter_contract_fails_closed() -> None:
    registry = load_registry()
    risk = RiskEvidence((), False, False)
    with pytest.raises(ValueError, match="v1 scoring"):
        assemble_blocking(request("v1-instrument-args"), registry, risk, choice_set_k=8)
    with pytest.raises(ValueError, match="v2_instrument scoring"):
        assemble_blocking(request("v2-missing-instrument-args"), registry, risk, scoring_mode="v2_instrument", choice_set_k=8)


@pytest.mark.parametrize("k", (8, 12, 16))
def test_forced_overflow_is_decoupled_from_choice_set_k(k: int) -> None:
    registry = load_registry()
    ids = tuple(slot.slot_id for slot in registry.slots[: MAX_FORCED + 1])
    result = assemble_blocking(request(f"overflow-{k}"), registry, RiskEvidence(ids, True, True), scoring_mode="v2_instrument", choice_set_k=k, theta=(3, 20))
    assert result.forced_overflow and len(result.receipt.forced_ids) == MAX_FORCED + 1
    exact = assemble_blocking(request(f"exact-{k}", value=" ".join(slot.domain for slot in registry.slots)), registry, RiskEvidence(ids[:MAX_FORCED], True, True), scoring_mode="v2_instrument", choice_set_k=k, theta=(3, 20))
    assert not exact.forced_overflow and exact.receipt.forced_ids == tuple(sorted(ids[:MAX_FORCED]))
    if k == 8:
        assert len(exact.receipt.choice_set) == 8
    if k == 16:
        assert len(exact.receipt.choice_set) == 16


def test_empty_set_and_forced_prefix_use_explicit_sweep_values() -> None:
    registry = load_registry()
    empty = assemble_blocking(request("empty"), registry, RiskEvidence((), False, False), scoring_mode="v2_instrument", choice_set_k=8, theta=(3, 20))
    assert empty.receipt.choice_set == () and empty.receipt.forced_ids == ()
    risk = compute_risk_evidence(request("forced", value="allergic"), registry)
    blocked = assemble_blocking(request("forced", value="allergic"), registry, risk, scoring_mode="v2_instrument", choice_set_k=8, theta=(3, 20))
    assert blocked.receipt.choice_set[: len(blocked.receipt.forced_ids)] == blocked.receipt.forced_ids


@pytest.mark.parametrize("description", ("brief", "the and with", "domain useful"))
def test_registry_description_content_lint_rejects_underfilled_descriptions(tmp_path, description: str) -> None:
    document = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    document["slots"][0]["description"] = description
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="description has fewer than four content tokens"):
        load_registry(path, expected_hash=None)


@pytest.mark.parametrize("value", ("use actual purposeful implementation words", "clear durable semantic content here", "alpha beta gamma delta"))
def test_registry_description_content_lint_accepts_four_content_tokens(tmp_path, value: str) -> None:
    document = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    document["slots"][0]["description"] = value
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    assert load_registry(path, expected_hash=None).slots[0].description == value


def test_candidate_tokens_remove_blocking_stopwords_only() -> None:
    assert request_candidate_tokens(request("tokens", "useful", "the durable", "with signal")) == {"useful", "durable", "signal"}


def test_v2_stopword_filter_uses_normalized_terms_for_candidates_and_l3() -> None:
    slot = replace(load_registry().slots[0], domain="unmatched", aliases=(), description="this durable signal", blocking_terms=())

    assert "thi" in BLOCKING_STOPWORDS_NORMALIZED
    assert "thi" in SCORING_DROP
    assert "thi" not in request_candidate_tokens(request("normalized-stopword", evidence="this durable"))
    assert score_components(slot, {"thi"})[0] == 0


def test_registry_content_token_lint_does_not_count_normalized_blocking_stopwords(tmp_path) -> None:
    document = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    document["slots"][0]["description"] = "this archive catalog preference"
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="fewer than four content tokens"):
        load_registry(path, expected_hash=None)
