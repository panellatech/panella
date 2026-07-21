from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from panella.resolver.registry import canonical_blocking_terms_hash, canonical_governance_hash, validate_alias_governance
from panella.resolver.registry import default_taxonomy_path, load_registry


ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests/resolver/fixtures/retention_ledger_v1.json"
GOVERNANCE = ROOT / "panella/resolver/alias_governance.yaml"
BASELINE_REGISTRY_HASH = "f6d44f272dd092a48c9078d8a7f442fa7b11fe350fb4506684bbbfabd2009a84"
VERDICT = "1415ec1c0650f53c2a07a3974bc16048396aead82e84754f42228c14b59328aa"
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
    }
    observed: dict[str, set[str]] = {}
    for transition in ledger["transitions"]:
        assert transition["to"] == "approved_remap"
        assert transition["reason"] == "governed_univocity_revocation"
        assert transition["verdict_sha256"] == VERDICT
        _, surface, slot_kind, slot_domain = transition["op_ref"].split(":")
        original_slot = f"{slot_kind}:{slot_domain}"
        for uid in transition["uids"]:
            row = cases[uid]
            assert row["initial_state"] == "must_retain_correct"
            assert row["raw_domain"] == surface and row["hit_slot"] == original_slot
        observed[transition["op_ref"]] = set(transition["uids"])
    assert observed == expected


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


def test_each_taxonomy_domain_has_a_registry_fixture_slot() -> None:
    taxonomy = yaml.safe_load(default_taxonomy_path().read_text(encoding="utf-8"))
    registry = load_registry()
    by_domain: dict[str, list[str]] = {}
    for slot in registry.slots:
        by_domain.setdefault(slot.taxonomy_domain, []).append(slot.slot_id)
    assert set(by_domain) == set(taxonomy["domains"])
    for name, descriptor in taxonomy["domains"].items():
        assert len(by_domain[name]) >= descriptor["min_slots"], name
