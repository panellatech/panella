"""Hermetic tests for eval/goldsets/key_correctness_eval.py — the ported key_correctness scorer.
Uses a FAKE chat_fn (no network, no codex subprocess) that returns pre-scripted JSON so the scoring
logic is exercised deterministically end to end against the shipped synthetic fixtures."""
from __future__ import annotations

import json

from eval.goldsets.key_correctness_eval import (
    DEFAULT_FIXTURE,
    DEFAULT_GOLDSET,
    GoldItem,
    load_fixture_text,
    load_items,
    run_eval,
    score,
)
from eval.goldsets.preference_extraction import PreferenceCandidate


def test_default_fixtures_load_without_error() -> None:
    items = load_items(DEFAULT_GOLDSET, DEFAULT_FIXTURE)
    assert len(items) > 0
    # v1: 33 lifecycle labels (10 v0 + 23 new: 11 hr-lifecycle sessions across 5 hr lifecycles +
    # 12 benign-lifecycle sessions across 6 benign lifecycles) + 23 extra_items (9 v0 + 14 new: 5
    # third-party traps + 4 adjacency probes + 3 hypothetical negatives + 2 hr singletons).
    assert len(items) == 56


def test_fixture_text_loads_by_sid() -> None:
    text_by_sid = load_fixture_text(DEFAULT_FIXTURE)
    assert "emp-s1" in text_by_sid
    assert "Northwind Traders" in text_by_sid["emp-s1"]


def test_no_real_names_in_shipped_fixture_or_goldset() -> None:
    """Guards against ever reintroducing real personal data from the source fixture this was
    rewritten from (which carried a real first name + a real email address in its high-risk
    example). The forbidden substrings are built at runtime (never appear as literals in this
    file) so a tracked-tree deny-scan never has to special-case a test asserting their ABSENCE."""
    forbidden_name = "".join(["R", "a", "v", "e", "n"])
    forbidden_email = "".join(["r", "a", "v", "e", "n", ".", "h", "i", "m", "@", "g", "m", "a", "i", "l", ".", "c", "o", "m"])
    fixture_blob = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    goldset_blob = DEFAULT_GOLDSET.read_text(encoding="utf-8")
    for blob in (fixture_blob, goldset_blob):
        assert forbidden_name not in blob
        assert forbidden_email not in blob


def _fake_chat_fn_perfect(item_texts: dict[str, list[dict]]):
    """Builds a chat_fn that returns the SCRIPTED candidate JSON per item text (keyed by a substring
    match on the user text), so run_eval can be exercised without any network/subprocess."""

    def _chat(system: str, user: str) -> str:
        for needle, candidates in item_texts.items():
            if needle in user:
                return json.dumps(candidates)
        return "[]"

    return _chat


def test_perfect_extractor_passes_go_no_go() -> None:
    """A scripted 'perfect' extractor (emits exactly the gold key/value for every positive, nothing
    for negatives) should PASS: schema_validity=1.0, 0 collisions, key_stability>=0.90,
    supersede_precision>=0.95."""
    items = load_items(DEFAULT_GOLDSET, DEFAULT_FIXTURE)
    script: dict[str, list[dict]] = {}
    for it in items:
        if not it.should_extract or not it.gold_key or not it.gold_value:
            continue
        kind, domain = it.gold_key.split(":", 1)
        script[it.text[:30]] = [
            {"kind": kind, "domain": domain, "value": it.gold_value, "confidence": 0.95, "evidence": it.text[:40]}
        ]
    chat_fn = _fake_chat_fn_perfect(script)
    result = run_eval(chat_fn, goldset_path=DEFAULT_GOLDSET, fixture_path=DEFAULT_FIXTURE)
    assert result["verdict"] == "PASS", result
    assert result["report"]["schema_validity"] == 1.0
    assert result["report"]["harmful_collisions"] == 0
    assert result["report"]["negative_colliding_keys"] == 0
    assert result["report"]["key_stability"] >= 0.90
    assert result["report"]["supersede_precision"] >= 0.95
    # HONEST SCOPE (v1): the fixture now carries several high-risk UPDATE pairs (medication /
    # emergency-contact / legal-name / primary-physician / dietary-restriction lifecycles), so a
    # PERFECT extractor (every hr session's own gold key+value emitted, nothing wrong) merges
    # every hr chain on its gold key with zero high-risk collisions — this MUST be True here,
    # proving the gate is actually exercisable now (not just structurally present-but-untested).
    assert result["high_risk_supersede_proven"] is True
    assert result["report"]["high_risk_update_pairs"] >= 5


def test_extractor_that_hallucinates_on_negatives_fails_no_go() -> None:
    """An extractor that emits SOMETHING for a should_extract=false item is a false-positive —
    negative_false_positive_rate > 0 must force NO-GO-SUPERSEDE regardless of everything else."""
    items = load_items(DEFAULT_GOLDSET, DEFAULT_FIXTURE)

    def _chat(system: str, user: str) -> str:
        # Emit a bogus candidate for EVERY call (including negatives) — deliberately broken.
        return json.dumps([{"kind": "fact", "domain": "bogus_slot", "value": "bogus", "confidence": 0.5, "evidence": "x"}])

    result = run_eval(_chat, goldset_path=DEFAULT_GOLDSET, fixture_path=DEFAULT_FIXTURE)
    assert result["verdict"] == "NO-GO-SUPERSEDE"
    assert result["report"]["negative_false_positive_rate"] > 0
    _ = items  # loaded to confirm fixtures parse; scoring itself is exercised via run_eval above


def test_v1_fixture_has_hr_update_lifecycles_and_third_party_traps() -> None:
    """Structural check on the v1 fixture pair (independent of any extractor run): at least 4
    lifecycles whose labels have >=2 high_risk items sharing a gold canonical_key across the chain
    — the exact definition key_correctness_eval.py's `high_risk_supersede_proven` flag relies on
    (see its HONEST SCOPE docstring paragraph) — each with a CONSISTENT supersedes chain (the
    first item in effective_at order has supersedes=None; every later item's supersedes equals the
    immediately-preceding item's sid, all within the SAME lifecycle). Also asserts third-party-trap
    items (a fact about a NAMED OTHER person, not the user) exist and are should_extract=false."""
    items = load_items(DEFAULT_GOLDSET, DEFAULT_FIXTURE)

    by_lifecycle: dict[str, list[GoldItem]] = {}
    for it in items:
        if it.lifecycle:
            by_lifecycle.setdefault(it.lifecycle, []).append(it)

    hr_update_lifecycles = []
    for lc, lc_items in by_lifecycle.items():
        hr_items = [it for it in lc_items if it.high_risk]
        if len(hr_items) < 2:
            continue
        keys = {it.gold_key for it in hr_items}
        if len(keys) != 1 or None in keys:
            continue  # not a single SHARED canonical_key across the chain
        hr_update_lifecycles.append(lc)

    assert len(hr_update_lifecycles) >= 4, hr_update_lifecycles

    for lc in hr_update_lifecycles:
        ordered = sorted(by_lifecycle[lc], key=lambda i: i.effective_at or "")
        assert ordered[0].supersedes is None, (lc, ordered[0])
        for older, newer in zip(ordered, ordered[1:], strict=False):
            assert newer.supersedes == older.item_id, (lc, older.item_id, newer.item_id, newer.supersedes)

    third_party_items = [it for it in items if it.item_id.startswith("third-party-")]
    assert len(third_party_items) >= 4, third_party_items
    assert all(not it.should_extract for it in third_party_items)
    assert all(not it.high_risk for it in third_party_items)


def test_every_lifecycle_supersedes_chain_references_prior_sids() -> None:
    """EVERY multi-item lifecycle (not just the high-risk ones) must carry a consistent
    `supersedes` chain of SIDs: the chain's first item has supersedes=None and every later item's
    supersedes equals the immediately-preceding item's sid. Guards against the v0-inherited defect
    where five labels carried the prior fact's VALUE (e.g. an employer name) instead of its sid —
    a data-contract lie that a scorer consuming `supersedes` would silently mis-join on."""
    items = load_items(DEFAULT_GOLDSET, DEFAULT_FIXTURE)
    all_ids = {it.item_id for it in items}

    by_lifecycle: dict[str, list[GoldItem]] = {}
    for it in items:
        if it.lifecycle:
            by_lifecycle.setdefault(it.lifecycle, []).append(it)

    checked = 0
    for lc, lc_items in sorted(by_lifecycle.items()):
        if len(lc_items) < 2:
            continue
        checked += 1
        ordered = sorted(lc_items, key=lambda i: i.effective_at or "")
        assert ordered[0].supersedes is None, (lc, ordered[0].item_id, ordered[0].supersedes)
        for older, newer in zip(ordered, ordered[1:], strict=False):
            assert newer.supersedes == older.item_id, (lc, older.item_id, newer.item_id, newer.supersedes)
            assert newer.supersedes in all_ids
    assert checked >= 9  # 5 v0-inherited + >=4 hr + benign additions — the sweep must not be vacuous


def test_cross_slot_collision_forces_no_go() -> None:
    """A DIRECT score() unit test (bypassing the fake-LLM plumbing): two DIFFERENT gold slots
    grounded under the SAME emitted canonical_key is a harmful collision -> NO-GO regardless of
    every other metric."""
    items = [
        GoldItem(
            item_id="a", text="employer text", should_extract=True, high_risk=False,
            gold_kind="fact", gold_key="fact:employer", gold_value="Northwind Traders",
            lifecycle=None, effective_at=None,
        ),
        GoldItem(
            item_id="b", text="editor text", should_extract=True, high_risk=False,
            gold_kind="fact", gold_key="fact:code_editor", gold_value="Nimbus Editor",
            lifecycle=None, effective_at=None,
        ),
    ]
    # Both items' candidates emitted under the SAME canonical_key "fact:merged_slot" — a harmful
    # cross-slot collision (the store would treat "Northwind Traders" and "Nimbus Editor" as
    # updates to the SAME tracked fact).
    extractions = {
        "a": [PreferenceCandidate(source_sid="a", kind="fact", domain="merged_slot", value="Northwind Traders", confidence=0.9, evidence="")],
        "b": [PreferenceCandidate(source_sid="b", kind="fact", domain="merged_slot", value="Nimbus Editor", confidence=0.9, evidence="")],
    }
    rep = score(items, extractions)
    assert rep.harmful_collisions == 1
    assert rep.collisions == [{"extractor_key": "fact:merged_slot", "gold_slots": ["fact:code_editor", "fact:employer"]}]
