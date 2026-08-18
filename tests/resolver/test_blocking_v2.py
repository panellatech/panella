from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from panella.resolver.blocking import (
    BLOCKING_RULES_HASH,
    CHOICE_SET_K,
    MAX_FORCED,
    THETA,
    assemble_blocking,
    blocking_rules_canonical,
    blocking_rules_hash_for,
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
    assert '"choice_set_k":8' in blocking_rules_canonical(8, (3, 20))
    assert blocking_rules_hash_for(CHOICE_SET_K, THETA) == BLOCKING_RULES_HASH
    assert blocking_rules_hash_for(8, (3, 20)) != blocking_rules_hash_for(12, (3, 20))
    assert blocking_rules_hash_for(8, (3, 20)) != blocking_rules_hash_for(8, (1, 5))


@pytest.mark.parametrize("k", (8, 12, 16))
def test_forced_overflow_is_decoupled_from_choice_set_k(k: int) -> None:
    registry = load_registry()
    ids = tuple(slot.slot_id for slot in registry.slots[: MAX_FORCED + 1])
    result = assemble_blocking(request(f"overflow-{k}"), registry, RiskEvidence(ids, True, True), choice_set_k=k, theta=(3, 20))
    assert result.forced_overflow and len(result.receipt.forced_ids) == MAX_FORCED + 1
    exact = assemble_blocking(request(f"exact-{k}", value=" ".join(slot.domain for slot in registry.slots)), registry, RiskEvidence(ids[:MAX_FORCED], True, True), choice_set_k=k, theta=(3, 20))
    assert not exact.forced_overflow and exact.receipt.forced_ids == tuple(sorted(ids[:MAX_FORCED]))
    if k == 8:
        assert len(exact.receipt.choice_set) == 8
    if k == 16:
        assert len(exact.receipt.choice_set) == 16


def test_empty_set_and_forced_prefix_use_explicit_sweep_values() -> None:
    registry = load_registry()
    empty = assemble_blocking(request("empty"), registry, RiskEvidence((), False, False), choice_set_k=8, theta=(3, 20))
    assert empty.receipt.choice_set == () and empty.receipt.forced_ids == ()
    risk = compute_risk_evidence(request("forced", value="allergic"), registry)
    blocked = assemble_blocking(request("forced", value="allergic"), registry, risk, choice_set_k=8, theta=(3, 20))
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
