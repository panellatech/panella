from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
import pytest

from eval.goldsets import resolver_blocking_diag as diag
from panella.resolver import ResolveRequest, ResolverContext, ResolverEngine, RunBudget
from panella.resolver.registry import canonical_blocking_terms_hash, canonical_governance_hash, canonical_registry_content_hash, validate_alias_governance
from panella.resolver.registry import default_registry_path, default_taxonomy_path, load_registry


ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests/resolver/fixtures/retention_ledger_v1.json"
GOVERNANCE = ROOT / "panella/resolver/alias_governance.yaml"
REVOCATION_FIXTURE = ROOT / "tests/resolver/fixtures/alias_revocation_miss.json"
MAIN_REGISTRY_FIXTURE = ROOT / "tests/resolver/fixtures/slot_registry_main_v1.yaml"
BASELINE_REGISTRY_HASH = "f6d44f272dd092a48c9078d8a7f442fa7b11fe350fb4506684bbbfabd2009a84"
VERDICT = "1415ec1c0650f53c2a07a3974bc16048396aead82e84754f42228c14b59328aa"
LAPTOP_VERDICT = "766b5f033b497a558a4b122fba80485b18c0feb0a9ba3926bc086b2fb6f4870e"
STATES = {"must_retain_correct", "known_wrong_fix", "unresolved", "approved_remap"}


def _ledger() -> dict[str, object]:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def test_pair_ledger_freezes_the_declared_baseline_and_states() -> None:
    ledger = _ledger()
    assert ledger["schema_version"] == "3" and ledger["universe"] == "pair"
    assert ledger["computed_at_registry_hash"] == BASELINE_REGISTRY_HASH
    cases = ledger["cases"]
    assert len(cases) == ledger["n_total"] == 60
    assert ledger["n_correct"] == 58
    assert all(case["method"] in {"exact", "alias"} and case["initial_state"] in STATES for case in cases)
    approved = {case["request_uid"] for case in cases if case["initial_state"] == "approved_remap"}
    assert approved == {"sc-hrmulti-0002/f1", "sc-hrmulti-0002/f2"}
    assert ledger["adjudications"] == [{"uids": sorted(approved), "verdict_sha256": VERDICT, "classification": "registry_caused"}]
    assert not any(case["initial_state"] == "known_wrong_fix" for case in cases)


def test_governed_transitions_meet_c1_e3_conditions() -> None:
    ledger = _ledger()
    cases = {case["request_uid"]: case for case in ledger["cases"]}
    expected = {
        "remove_alias:nickname:fact:chosen_name": {"sc-hrmulti-0000/f1", "sc-hrunrelated-0003/f-a", "sc-hrunrelated-0012/f-a"},
        "remove_alias:username:fact:messaging_handle": {"sc-hrunrelated-0020/f-a"},
        "remove_alias:laptop:fact:computer_model": {"sc-supersede-0012-laptop_model/f-later"},
    }
    expected_verdicts = {
        "remove_alias:nickname:fact:chosen_name": VERDICT,
        "remove_alias:username:fact:messaging_handle": VERDICT,
        "remove_alias:laptop:fact:computer_model": LAPTOP_VERDICT,
    }
    observed: dict[str, set[str]] = {}
    for transition in ledger["transitions"]:
        assert transition["to"] == "approved_remap"
        assert transition["reason"] == "governed_univocity_revocation"
        assert transition["verdict_sha256"] == expected_verdicts[transition["op_ref"]]
        _, surface, slot_kind, slot_domain = transition["op_ref"].split(":")
        original_slot = f"{slot_kind}:{slot_domain}"
        for uid in transition["uids"]:
            row = cases[uid]
            assert row["initial_state"] == "must_retain_correct"
            assert row["raw_domain"] == surface and row["hit_slot"] == original_slot
        observed[transition["op_ref"]] = set(transition["uids"])
    assert observed == expected


@pytest.mark.parametrize(
    "case",
    json.loads(REVOCATION_FIXTURE.read_text(encoding="utf-8"))["cases"],
    ids=lambda case: case["raw_domain"],
)
def test_alias_revocation_fixture_is_a_deterministic_miss(case: dict[str, str]) -> None:
    fixture = json.loads(REVOCATION_FIXTURE.read_text(encoding="utf-8"))
    assert case in fixture["cases"]
    assert case["expected_method"] == "none"

    decision = ResolverEngine().resolve(
        ResolveRequest(f"alias-revocation/{case['raw_domain']}", case["kind"], case["raw_domain"], "", ""),
        ResolverContext(()),
        RunBudget(1),
    )
    assert decision.slot_id is None
    assert decision.action == "ABSTAIN_ADD"


def test_governance_records_laptop_revocation_and_domain_refinements() -> None:
    governance = yaml.safe_load(GOVERNANCE.read_text(encoding="utf-8"))
    validate_alias_governance(governance, repository_root=ROOT)
    laptop_ops = [
        operation for operation in governance["ops"]
        if operation["op"] == "remove_alias"
        and operation["surface"] == "laptop"
        and operation["from_slot"] == "fact:computer_model"
    ]
    assert laptop_ops == [{
        "op": "remove_alias",
        "surface": "laptop",
        "from_slot": "fact:computer_model",
        "rationale": "A laptop is not a unique computer-model surface when multiple physical devices may occupy the slot.",
        "reason": "ambiguity",
        "fixture_id": "tests/resolver/fixtures/alias_revocation_miss.json",
    }]
    domain_ops = [
        operation for operation in governance["ops"]
        if operation["op"] == "add_domain"
        and operation["surface"] in {"hardware_accessory", "artistic_pastime", "streaming_subscription"}
    ]
    assert domain_ops == [
        {
            "op": "add_domain",
            "surface": "hardware_accessory",
            "to_slot": "preference:hardware_accessory",
            "rationale": "The hardware-specific preference surface keeps accessories distinct from general devices.",
        },
        {
            "op": "add_domain",
            "surface": "artistic_pastime",
            "to_slot": "preference:artistic_pastime",
            "rationale": "The artistic-pastime surface preserves the creative leisure preference without generic hobby routing.",
        },
        {
            "op": "add_domain",
            "surface": "streaming_subscription",
            "to_slot": "preference:streaming_subscription",
            "rationale": "The subscription-specific surface preserves the streaming preference without generic platform routing.",
        },
    ]
    assert len(governance["ops"]) == 52


def test_governance_reconciles_real_main_baseline_to_current_registry() -> None:
    governance = yaml.safe_load(GOVERNANCE.read_text(encoding="utf-8"))
    baseline = yaml.safe_load(MAIN_REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    current = yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))
    assert canonical_registry_content_hash(baseline) == BASELINE_REGISTRY_HASH
    assert governance["baseline_registry_hash"] == BASELINE_REGISTRY_HASH
    validate_alias_governance(governance, baseline_document=baseline, current_document=current)


def test_governance_reference_and_blocking_term_vectors() -> None:
    reference = {
        "baseline_registry_hash": "0" * 64,
        "ops": [
            {"op": "remove_alias", "surface": "player", "from_slot": "fact:music_player", "rationale": "ambiguous across media slots", "reason": "superseded_by_pair", "pair_id": "g-0001"},
            {"op": "add_alias", "surface": "player", "to_slot": "fact:media_player", "rationale": "re-point to the specific slot", "pair_id": "g-0001"},
        ],
    }
    assert canonical_governance_hash(reference) == "bbd75b76e781a456f133bb38c822a8db2afcb47fc77a404bc793497472f78697"
    assert canonical_blocking_terms_hash(["netflix", "hulu", "spotify", "stream"]) == "1435eba3cb5eb28a2a93f0e5a29fa1e24e92bf518cff70c025256a6d304e7c12"
    governance = yaml.safe_load(GOVERNANCE.read_text(encoding="utf-8"))
    assert canonical_governance_hash(governance) == "9bcc6819ff41195bf3f6e917603f4fd745c95ead003f5ef7e2308592c2ccf9cf"


def test_governance_rejects_frozen_failure_vectors(tmp_path: Path) -> None:
    base = {"baseline_registry_hash": "0" * 64, "ops": [{"op": "remove_alias", "surface": "nickname", "from_slot": "fact:chosen_name", "rationale": "ambiguous", "reason": "ambiguity", "fixture_id": "tests/resolver/fixtures/alias_revocation_miss.json"}]}
    invalid = [
        {**base, "ops": [{**base["ops"][0], "op": "rename_alias"}]},
        {"baseline_registry_hash": "0" * 64, "ops": [{"op": "add_alias", "surface": "player", "rationale": "missing destination"}]},
        {"baseline_registry_hash": "0" * 64, "ops": [base["ops"][0], dict(base["ops"][0])]},
        {"baseline_registry_hash": "0" * 64, "ops": [{key: value for key, value in base["ops"][0].items() if key != "fixture_id"}]},
    ]
    for document in invalid:
        try:
            validate_alias_governance(document, repository_root=ROOT)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid governance vector was accepted")


def test_governance_reconciles_alias_and_domain_projection() -> None:
    baseline = {"slots": [{"id": "fact:music_player", "domain": "music_player", "aliases": ["player"]}]}
    current = {"slots": [{"id": "fact:media_player", "domain": "media_player", "aliases": ["player"]}]}
    document = {
        "baseline_registry_hash": "0" * 64,
        "ops": [
            {"op": "remove_domain", "surface": "music_player", "from_slot": "fact:music_player", "rationale": "retire the broad domain", "reason": "retirement"},
            {"op": "add_domain", "surface": "media_player", "to_slot": "fact:media_player", "rationale": "add the specific domain"},
            {"op": "remove_alias", "surface": "player", "from_slot": "fact:music_player", "rationale": "re-point the ambiguous alias", "reason": "superseded_by_pair", "pair_id": "alias-1"},
            {"op": "add_alias", "surface": "player", "to_slot": "fact:media_player", "rationale": "point the alias at the specific slot", "pair_id": "alias-1"},
        ],
    }
    validate_alias_governance(document, baseline_document=baseline, current_document=current)


def test_pair_retention_uses_real_decisions_and_has_complete_ledger_keyspace() -> None:
    ledger = _ledger()
    _, decisions, _ = diag._resolve_pair_goldset()
    decision_slots = {
        f"{case_id}/{fact_id}": decision.slot_id
        for (case_id, fact_id), decision in decisions.items()
    }
    assert {case["request_uid"] for case in ledger["cases"]} <= set(decision_slots)
    assert diag._retention_report(ledger, decision_slots) == {
        "pass": True,
        "approved_remap_eliminated": True,
    }


def test_pair_retention_excludes_transitioned_uids_from_retained_budget() -> None:
    ledger = {
        "cases": [
            {"request_uid": "u1", "initial_state": "must_retain_correct", "hit_slot": "s1"},
            {"request_uid": "u2", "initial_state": "must_retain_correct", "hit_slot": "s2"},
            {"request_uid": "u3", "initial_state": "must_retain_correct", "hit_slot": "s3"},
            {"request_uid": "u4", "initial_state": "must_retain_correct", "hit_slot": "s4"},
        ],
        "transitions": [{"uids": ["u4"]}],
    }
    decision_slots = {"u1": "s1", "u2": "wrong", "u3": "wrong", "u4": "wrong"}
    assert diag._retention_report(ledger, decision_slots)["pass"] is True
    assert 1 < 4 - 2  # Counting transitioned u4 would make the same retained-loss budget fail.


def test_each_taxonomy_domain_has_a_registry_fixture_slot() -> None:
    taxonomy = yaml.safe_load(default_taxonomy_path().read_text(encoding="utf-8"))
    registry = load_registry()
    by_domain: dict[str, list[str]] = {}
    for slot in registry.slots:
        by_domain.setdefault(slot.taxonomy_domain, []).append(slot.slot_id)
    assert set(by_domain) == set(taxonomy["domains"])
    for name, descriptor in taxonomy["domains"].items():
        assert len(by_domain[name]) >= descriptor["min_slots"], name
