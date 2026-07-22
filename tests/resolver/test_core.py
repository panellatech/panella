from __future__ import annotations

import copy
import dataclasses
import hashlib
import math
from pathlib import Path

import pytest
import yaml

from panella.resolver import (
    CalibrationManifest,
    CalibrationSlice,
    canonical_manifest_hash,
    ExistingSlot,
    FallbackSuggestion,
    ResolveRequest,
    ResolverConfig,
    ResolverContext,
    ResolverEngine,
    RunBudget,
    TransportAttempt,
)
from panella.resolver.blocking import CHOICE_SET_K, assemble_blocking
from panella.resolver.normalize import NORMALIZER_VERSION, compute_normalizer_rules_hash, normalizer_rules_hash, resolver_normalize
from panella.resolver.registry import (
    PINNED_REGISTRY_HASH,
    canonical_registry_content_hash,
    canonical_taxonomy_content_hash,
    composite_registry_hash,
    default_registry_path,
    default_taxonomy_path,
    load_registry,
)
from panella.resolver.risk import compute_risk_evidence
from panella.resolver.types import split_slot_id


class FakeProvider:
    def __init__(self, suggestion: FallbackSuggestion) -> None:
        self.suggestion = suggestion
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "test-model"

    @property
    def prompt_template_hash(self) -> str:
        return "test-prompt"

    def suggest(self, *args: object, **kwargs: object) -> FallbackSuggestion:
        self.calls += 1
        return self.suggestion


def request(uid: str = "req-1", raw_domain: str = "unknown", value: str = "code editor") -> ResolveRequest:
    return ResolveRequest(uid, "fact", raw_domain, value, "")


def valid_manifest() -> CalibrationManifest:
    calibration = CalibrationSlice(50, (25, 25), ((0.0, 0.5, 0.0), (0.5, 1.0, 1.0)), 1.0)
    return CalibrationManifest(
        "cal-1", "test-model", "test-prompt", PINNED_REGISTRY_HASH, normalizer_rules_hash, "1.0.0",
        ("public-hash",), "evidence", "commit", {"benign": calibration, "hr": calibration},
    )


def llm_engine(suggestion: FallbackSuggestion) -> tuple[ResolverEngine, FakeProvider]:
    provider = FakeProvider(suggestion)
    manifest = valid_manifest()
    engine = ResolverEngine(ResolverConfig(True, 20, manifest, canonical_manifest_hash(manifest), "evidence"), provider=provider)
    return engine, provider


def registry_data() -> dict[str, object]:
    return yaml.safe_load(default_registry_path().read_text(encoding="utf-8"))


def write_registry(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("raw_domain", "value", "expected_guard"),
    [("employer", "ordinary workplace", False), ("employer", "allergic reaction", True)],
)
def test_deterministic_pass_for_empty_risk_and_escalation_for_other_hr_evidence(
    raw_domain: str, value: str, expected_guard: bool
) -> None:
    engine = ResolverEngine()
    decision = engine.resolve(request(raw_domain=raw_domain, value=value), ResolverContext(()), RunBudget(1))
    assert decision.guard_fired is expected_guard
    if expected_guard:
        assert decision.action == "ABSTAIN_ADD"
    else:
        assert decision.action == "ADD"
        assert decision.method == "exact"
        assert decision.fallback_outcome == "not_attempted_deterministic_hit"


def test_hr_deterministic_self_passes_without_transport() -> None:
    provider = FakeProvider(FallbackSuggestion(None, None, ()))
    engine = ResolverEngine(ResolverConfig(False, 20, None, None, None), provider=provider)
    decision = engine.resolve(request(raw_domain="allergy", value=""), ResolverContext(()), RunBudget(1))
    assert decision.slot_id == "fact:medical_allergy"
    assert decision.high_risk is True
    assert decision.guard_fired is False
    assert provider.calls == 0


def test_competing_hr_evidence_escalates_to_forced_hr_choice_set() -> None:
    engine, provider = llm_engine(FallbackSuggestion("fact:medication", 1.0, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(uid="competing-hr", raw_domain="allergy", value="medication"), ResolverContext(()), RunBudget(1))

    assert decision.guard_fired is True
    assert decision.method == "llm_choice"
    assert decision.slot_id == "fact:medication"
    assert decision.risk_evidence.matched_hr_slot_ids == ("fact:medical_allergy", "fact:medication")
    assert decision.blocking_receipt is not None
    assert decision.blocking_receipt.choice_set == ("fact:medical_allergy", "fact:medication")
    assert decision.blocking_receipt.slice == "hr"
    assert provider.calls == 1


def test_competing_hr_evidence_abstains_when_llm_is_disabled() -> None:
    decision = ResolverEngine().resolve(
        request(uid="competing-hr-disabled", raw_domain="allergy", value="medication"), ResolverContext(()), RunBudget(1)
    )

    assert decision.action == "ABSTAIN_ADD"
    assert decision.slot_id is None
    assert decision.guard_fired is True
    assert decision.high_risk is True
    assert decision.risk_evidence.matched_hr_slot_ids == ("fact:medical_allergy", "fact:medication")


def test_short_circuit_hr_alias_propagates_risk_with_llm_disabled() -> None:
    decision = ResolverEngine().resolve(request(raw_domain="food_allergy", value="", uid="risk-alias"), ResolverContext(()), RunBudget(1))
    assert decision.action == "ADD"
    assert decision.high_risk is True
    assert decision.risk_evidence.matched_hr_slot_ids == ("fact:medical_allergy",)


@pytest.mark.parametrize(
    ("manifest", "evidence_hash", "mismatch"),
    (
        (dataclasses.replace(valid_manifest(), model_id="different-model"), "evidence", "model_id"),
        (valid_manifest(), "different-evidence", "evidence_hash"),
        (valid_manifest(), None, "evidence_hash"),
    ),
)
def test_manifest_component_mismatch_disables_llm_and_preserves_high_risk(
    manifest: CalibrationManifest, evidence_hash: str | None, mismatch: str
) -> None:
    provider = FakeProvider(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    engine = ResolverEngine(
        ResolverConfig(True, 20, manifest, canonical_manifest_hash(manifest), evidence_hash), provider=provider
    )

    decision = engine.resolve(request(raw_domain="employer", value="allergic reaction"), ResolverContext(()), RunBudget(1))

    assert decision.fallback_outcome == "not_attempted_disabled"
    assert decision.disabled_reason == f"manifest_component_mismatch:{mismatch}"
    assert decision.guard_fired is True
    assert decision.high_risk is True
    assert decision.blocking_receipt is None and decision.llm_receipt is None
    assert provider.calls == 0


def test_tampered_manifest_mapping_with_stale_hash_soft_disables_llm() -> None:
    manifest = valid_manifest()
    stale_hash = canonical_manifest_hash(manifest)
    tampered_calibration = dataclasses.replace(
        manifest.slices["benign"],
        mapping=((0.0, 0.5, 0.1), (0.5, 1.0, 1.0)),
        tau=0.1,
    )
    tampered_manifest = dataclasses.replace(
        manifest,
        slices={"benign": tampered_calibration, "hr": tampered_calibration},
    )
    provider = FakeProvider(FallbackSuggestion("preference:code_editor", 1.0, (TransportAttempt("ok", 1),)))
    engine = ResolverEngine(ResolverConfig(True, 20, tampered_manifest, stale_hash, "evidence"), provider=provider)

    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))

    assert decision.fallback_outcome == "not_attempted_disabled"
    assert decision.disabled_reason == "manifest_component_mismatch:manifest_hash"
    assert decision.blocking_receipt is None and decision.llm_receipt is None
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("mapping", "tau"),
    (
        (((0.0000001, 0.5, 0.0), (0.5, 1.0, 1.0)), 1.0),
        (((0.0, 0.5000001, 0.0), (0.5, 1.0, 1.0)), 1.0),
        (((0.0, 0.5, 0.0000001), (0.5, 1.0, 1.0)), 1.0),
        (((0.0, 0.5, 0.0), (0.5, 1.0, 1.0)), 1.0000001),
    ),
)
def test_calibration_rejects_unquantized_float_fields(
    mapping: tuple[tuple[float, float, float], ...], tau: float
) -> None:
    with pytest.raises(ValueError, match="quantized"):
        CalibrationSlice(50, (25, 25), mapping, tau)


def test_unquantized_calibration_cannot_form_a_hash_colliding_manifest() -> None:
    quantized = CalibrationSlice(50, (25, 25), ((0.0, 0.5, 0.0), (0.5, 1.0, 1.0)), 1.0)
    manifest = CalibrationManifest(
        "cal-1", "test-model", "test-prompt", PINNED_REGISTRY_HASH, normalizer_rules_hash, "1.0.0",
        ("public-hash",), "evidence", "commit", {"benign": quantized, "hr": quantized},
    )
    assert canonical_manifest_hash(manifest)

    with pytest.raises(ValueError, match="quantized"):
        CalibrationSlice(50, (25, 25), ((0.0, 0.5, 0.0000001), (0.5, 1.0, 1.0)), 1.0)


def test_manifest_snapshots_external_slices_before_engine_construction() -> None:
    source_per_bin = [25, 25]
    source_mapping = [[0.0, 0.5, 0.0], [0.5, 1.0, 1.0]]
    source_slice = CalibrationSlice(50, source_per_bin, source_mapping, 1.0)
    source_slices = {"benign": source_slice, "hr": source_slice}
    manifest = CalibrationManifest(
        "cal-1", "test-model", "test-prompt", PINNED_REGISTRY_HASH, normalizer_rules_hash, "1.0.0",
        ("public-hash",), "evidence", "commit", source_slices,
    )
    manifest_hash = canonical_manifest_hash(manifest)
    provider = FakeProvider(FallbackSuggestion("preference:code_editor", 1.0, (TransportAttempt("ok", 1),)))
    engine = ResolverEngine(ResolverConfig(True, 20, manifest, manifest_hash, "evidence"), provider=provider)

    source_per_bin[0] = 0
    source_mapping[1][2] = 0.0
    object.__setattr__(source_slice, "mapping", ((0.0, 1.0, 0.0),))
    object.__setattr__(source_slice, "tau", 0.0)
    source_slices["benign"] = source_slice
    source_slices["hr"] = source_slice

    with pytest.raises(TypeError):
        manifest.slices["benign"] = source_slice
    assert isinstance(manifest.slices["benign"].per_bin, tuple)
    assert isinstance(manifest.slices["benign"].mapping, tuple)
    assert manifest.slices["benign"].mapping == ((0.0, 0.5, 0.0), (0.5, 1.0, 1.0))
    assert canonical_manifest_hash(manifest) == manifest_hash

    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "selected"
    assert provider.calls == 1


def test_missing_manifest_hash_soft_disables_llm() -> None:
    manifest = valid_manifest()
    provider = FakeProvider(FallbackSuggestion("preference:code_editor", 1.0, (TransportAttempt("ok", 1),)))
    engine = ResolverEngine(ResolverConfig(True, 20, manifest, None, "evidence"), provider=provider)

    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))

    assert decision.fallback_outcome == "not_attempted_disabled"
    assert decision.disabled_reason == "manifest_component_mismatch:manifest_hash"
    assert decision.blocking_receipt is None and decision.llm_receipt is None
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("budget", "expected_outcome", "blocking", "llm"),
    [
        (RunBudget(0), "not_attempted_disabled", False, False),
        (RunBudget(0, calls_made=0), "not_attempted_disabled", False, False),
    ],
)
def test_global_disabled_truth_rows(budget: RunBudget, expected_outcome: str, blocking: bool, llm: bool) -> None:
    decision = ResolverEngine().resolve(request(), ResolverContext(()), budget)
    assert decision.fallback_outcome == expected_outcome
    assert (decision.blocking_receipt is not None) is blocking
    assert (decision.llm_receipt is not None) is llm


def test_budget_row_has_no_receipts() -> None:
    engine, _ = llm_engine(FallbackSuggestion("fact:code_editor", 1.0, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1, calls_made=1))
    assert decision.fallback_outcome == "not_attempted_budget_exhausted"
    assert decision.blocking_receipt is None and decision.llm_receipt is None


def test_upgraded_global_disabled_and_budget_rows_short_circuit_before_blocking() -> None:
    upgraded = request(uid="guard-global", raw_domain="employer", value="allergic")
    disabled = ResolverEngine().resolve(upgraded, ResolverContext(()), RunBudget(1))
    assert disabled.guard_fired is True
    assert disabled.fallback_outcome == "not_attempted_disabled"
    assert disabled.blocking_receipt is None and disabled.llm_receipt is None
    engine, _ = llm_engine(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    budget = engine.resolve(request(uid="guard-budget", raw_domain="employer", value="allergic"), ResolverContext(()), RunBudget(1, 1))
    assert budget.guard_fired is True
    assert budget.fallback_outcome == "not_attempted_budget_exhausted"
    assert budget.blocking_receipt is None and budget.llm_receipt is None


def test_empty_choice_row_has_only_blocking_receipt() -> None:
    engine, provider = llm_engine(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(value="", raw_domain="unmapped"), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "not_attempted_empty_choice_set"
    assert decision.blocking_receipt is not None and decision.blocking_receipt.choice_set == ()
    assert decision.llm_receipt is None and provider.calls == 0


def test_slice_disabled_row_has_only_blocking_receipt() -> None:
    manifest = valid_manifest()
    disabled_hr = dataclasses.replace(manifest, slices={"benign": manifest.slices["benign"], "hr": CalibrationSlice(1, (), (), 0.0)})
    provider = FakeProvider(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    engine = ResolverEngine(
        ResolverConfig(True, 20, disabled_hr, canonical_manifest_hash(disabled_hr), "evidence"), provider=provider
    )
    decision = engine.resolve(request(raw_domain="diet", value="allergic"), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "not_attempted_disabled"
    assert decision.disabled_reason == "hr_slice_required_but_disabled"
    assert decision.blocking_receipt is not None and decision.llm_receipt is None
    assert provider.calls == 0


def test_upgraded_slice_disabled_row_preserves_guard_and_receipt() -> None:
    manifest = valid_manifest()
    disabled_hr = dataclasses.replace(manifest, slices={"benign": manifest.slices["benign"], "hr": CalibrationSlice(1, (), (), 0.0)})
    provider = FakeProvider(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    engine = ResolverEngine(
        ResolverConfig(True, 20, disabled_hr, canonical_manifest_hash(disabled_hr), "evidence"), provider=provider
    )
    decision = engine.resolve(request(uid="guard-slice", raw_domain="employer", value="allergic"), ResolverContext(()), RunBudget(1))
    assert decision.guard_fired is True
    assert decision.fallback_outcome == "not_attempted_disabled"
    assert decision.blocking_receipt is not None and decision.llm_receipt is None


@pytest.mark.parametrize(
    ("suggestion", "outcome", "has_slot"),
    [
        (FallbackSuggestion("preference:code_editor", 1.0, (TransportAttempt("ok", 1),)), "selected", True),
        (FallbackSuggestion("preference:code_editor", 0.0, (TransportAttempt("ok", 1),)), "low_confidence", False),
        (FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)), "abstained", False),
        (FallbackSuggestion("not-a-choice", 1.0, (TransportAttempt("ok", 1),)), "invalid_output", False),
        (FallbackSuggestion(None, None, (TransportAttempt("transport_error", 1),)), "transport_failed", False),
        (FallbackSuggestion(None, None, (TransportAttempt("timeout", 1),)), "timeout", False),
    ],
)
def test_llm_truth_rows_have_both_receipts(suggestion: FallbackSuggestion, outcome: str, has_slot: bool) -> None:
    engine, provider = llm_engine(suggestion)
    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == outcome
    assert (decision.slot_id is not None) is has_slot
    assert decision.blocking_receipt is not None and decision.llm_receipt is not None
    assert provider.calls == 1


def test_upgraded_llm_selection_uses_hr_slice_and_preserves_guard() -> None:
    engine, provider = llm_engine(FallbackSuggestion("fact:employer", 1.0, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(uid="guard-selected", raw_domain="employer", value="allergic"), ResolverContext(()), RunBudget(1))
    assert decision.action == "ADD"
    assert decision.method == "llm_choice"
    assert decision.fallback_outcome == "selected"
    assert decision.guard_fired is True and decision.high_risk is True
    assert decision.blocking_receipt is not None and decision.blocking_receipt.slice == "hr"
    assert decision.llm_receipt is not None and provider.calls == 1


def test_unresolved_encoding_and_run_invariants() -> None:
    engine = ResolverEngine()
    budget = RunBudget(1)
    decision = engine.resolve(request(uid="once"), ResolverContext(()), budget)
    assert decision.unresolved_domain == "xunres_" + hashlib.sha256(b"once").hexdigest()[:32]
    with pytest.raises(ValueError, match="duplicate request_uid"):
        engine.resolve(request(uid="once"), ResolverContext(()), budget)
    collision_uid = "collision"
    encoded = "xunres_" + hashlib.sha256(collision_uid.encode()).hexdigest()[:32]
    collision_budget = RunBudget(1, seen_unresolved={encoded: "other-request"})
    with pytest.raises(RuntimeError, match="encoding collision"):
        engine.resolve(request(uid=collision_uid), ResolverContext(()), collision_budget)
    assert split_slot_id("fact:employer") == ("fact", "employer")
    with pytest.raises(ValueError):
        ExistingSlot("fact:xunres_bad", None)


def test_same_input_is_byte_identical_for_same_budget_prestate() -> None:
    engine = ResolverEngine()
    first = engine.resolve(request(uid="one"), ResolverContext(()), RunBudget(1))
    second = engine.resolve(request(uid="two"), ResolverContext(()), RunBudget(1))
    assert dataclasses.asdict(first) | {"unresolved_domain": "normalized"} == dataclasses.asdict(second) | {
        "unresolved_domain": "normalized"
    }


@pytest.mark.parametrize(
    "vector",
    [
        ("Current_Employer", "employer"), ("my_home_city", "home_city"), ("favorite_coffee_style", "coffee_style"),
        ("allergies", "allergy"), ("Code-Editor", "code_editor"), ("the_primary_browsers", "browser"),
        ("address", "address"), ("status", "status"), ("  ", ""), ("NEW!!phone--model", "phone_model"),
    ],
)
def test_normalize_reference_vectors(vector: tuple[str, str]) -> None:
    assert resolver_normalize(vector[0]) == vector[1]


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_field",
        "duplicate_id",
        "two_form_collision",
        "dangling_neighbor",
        "high_risk_without_lexicon",
        "reserved_alias",
        "below_minimum_slots",
        "below_taxonomy_domain_minimum",
        "id_does_not_match_kind_domain",
        "noncanonical_domain",
        "noncanonical_alias",
        "high_risk_not_bool",
        "too_many_deny_neighbors",
        "too_many_hr_lexicon_terms",
        "non_high_risk_with_hr_lexicon",
        "deny_neighbor_is_high_risk",
        "unexpected_root_keys",
        "reserved_unresolved_domain",
    ),
)
def test_registry_fail_matrix(tmp_path: Path, mutation: str) -> None:
    base = registry_data()

    match mutation:
        case "missing_field":
            del base["slots"][0]["description"]
        case "duplicate_id":
            base["slots"].append(copy.deepcopy(base["slots"][0]))
        case "two_form_collision":
            base["slots"][1]["aliases"].append("name")
        case "dangling_neighbor":
            base["slots"][4]["deny_neighbors"] = ["not_a_slot"]
        case "high_risk_without_lexicon":
            base["slots"][4]["hr_lexicon"] = []
        case "reserved_alias":
            base["slots"][0]["aliases"].append("xunres_bad")
        case "below_minimum_slots":
            base["slots"] = base["slots"][:49]
        case "below_taxonomy_domain_minimum":
            source = next(slot for slot in base["slots"] if slot["taxonomy_domain"] == "identity")
            source["taxonomy_domain"] = "health"
        case "id_does_not_match_kind_domain":
            base["slots"][0]["id"] = "fact:not_legal_name"
        case "noncanonical_domain":
            base["slots"][0]["domain"] = "Legal_Name"
            base["slots"][0]["id"] = "fact:Legal_Name"
        case "noncanonical_alias":
            base["slots"][0]["aliases"].append("Full Name")
        case "high_risk_not_bool":
            base["slots"][0]["high_risk"] = "false"
        case "too_many_deny_neighbors":
            base["slots"][0]["deny_neighbors"] = ["chosen_name", "pronoun", "home_city", "diet", "dietary_restriction"]
        case "too_many_hr_lexicon_terms":
            base["slots"][4]["hr_lexicon"] = [f"term_{index}" for index in range(9)]
        case "non_high_risk_with_hr_lexicon":
            base["slots"][0]["hr_lexicon"] = ["sensitive"]
        case "deny_neighbor_is_high_risk":
            base["slots"][4]["deny_neighbors"] = ["medical_condition"]
        case "unexpected_root_keys":
            base["unexpected"] = True
        case "reserved_unresolved_domain":
            base["slots"][0]["domain"] = "xunres_bad"
            base["slots"][0]["id"] = "fact:xunres_bad"
        case _:
            raise ValueError(f"unknown registry mutation: {mutation}")

    expected = "taxonomy domain identity has fewer" if mutation == "below_taxonomy_domain_minimum" else None
    with pytest.raises(ValueError, match=expected):
        load_registry(write_registry(tmp_path, base), expected_hash=None)


def test_registry_pin_mismatch_still_raises(tmp_path: Path) -> None:
    pin_mutation = registry_data()
    pin_mutation["version"] = "3"
    with pytest.raises(ValueError, match="content hash"):
        load_registry(write_registry(tmp_path, pin_mutation))


def test_taxonomy_pin_mismatch_still_raises(tmp_path: Path) -> None:
    taxonomy = yaml.safe_load(default_taxonomy_path().read_text(encoding="utf-8"))
    taxonomy["version"] = "drifted"
    taxonomy_path = tmp_path / "taxonomy.yaml"
    taxonomy_path.write_text(yaml.safe_dump(taxonomy, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash"):
        load_registry(default_registry_path(), taxonomy_path=taxonomy_path)


def test_injected_registry_integrity_still_raises() -> None:
    registry = load_registry()
    with pytest.raises(ValueError, match="content hash"):
        ResolverEngine(registry=dataclasses.replace(registry, content_hash="not-pinned"))
    with pytest.raises(ValueError, match="at least"):
        ResolverEngine(registry=dataclasses.replace(registry, slots=()))


def test_hr_alias_only_matching_after_folding_is_a_miss() -> None:
    decision = ResolverEngine().resolve(request(raw_domain="current_food_allergy", value="", uid="folded"), ResolverContext(()), RunBudget(1))
    assert decision.slot_id is None
    assert decision.action == "ABSTAIN_ADD"
    assert decision.high_risk is True


def test_hash_pins_and_versions() -> None:
    registry = load_registry()
    content = registry_data()
    taxonomy = yaml.safe_load(default_taxonomy_path().read_text(encoding="utf-8"))
    assert registry.slot_registry_hash == canonical_registry_content_hash(content)
    assert registry.taxonomy_hash == canonical_taxonomy_content_hash(taxonomy)
    assert registry.content_hash == PINNED_REGISTRY_HASH == composite_registry_hash(registry.slot_registry_hash, registry.taxonomy_hash)
    assert normalizer_rules_hash == compute_normalizer_rules_hash()
    assert NORMALIZER_VERSION == "1.0.0"
    assert ResolverEngine().resolve(request(), ResolverContext(()), RunBudget(1)).versions.resolver_code_version == "1.0.0"


def test_blocking_is_deterministic_forced_first_and_overflow() -> None:
    registry = load_registry()
    normal = request(raw_domain="diet", value="allergic", uid="block")
    risk = compute_risk_evidence(normal, registry)
    first = assemble_blocking(normal, registry, risk, "preference:diet")
    second = assemble_blocking(normal, registry, risk, "preference:diet")
    assert first.receipt == second.receipt
    assert first.receipt.choice_set[:2] == tuple(sorted({"fact:medical_allergy", "preference:diet"}))
    many_terms = " ".join(term for slot in registry.slots if slot.high_risk for term in slot.hr_lexicon)
    overflow_risk = compute_risk_evidence(request(uid="overflow", value=many_terms), registry)
    result = assemble_blocking(request(uid="overflow", value=many_terms), registry, overflow_risk)
    assert len(overflow_risk.matched_hr_slot_ids) > CHOICE_SET_K
    assert result.forced_overflow and result.receipt.choice_set == overflow_risk.matched_hr_slot_ids
    decision = ResolverEngine().resolve(request(uid="overflow-2", value=many_terms), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "not_attempted_disabled"  # global gate comes before blocking


@pytest.mark.parametrize(
    ("suggestion", "violation", "outcome"),
    [
        (FallbackSuggestion(None, None, ()), "empty_attempts", "invalid_output"),
        (FallbackSuggestion(None, None, (TransportAttempt("transport_error", 1), TransportAttempt("timeout", 1), TransportAttempt("timeout", 1))), "attempt_sequence", "transport_failed"),
        (FallbackSuggestion(None, None, (TransportAttempt("ok", 21),)), "timeout_exceeded_ok", "timeout"),
        (FallbackSuggestion("preference:code_editor", 1.0, (TransportAttempt("timeout", 1),)), "payload_without_ok", "invalid_output"),
        (FallbackSuggestion("preference:code_editor", 1.0, (TransportAttempt("invalid_output", 1), TransportAttempt("ok", 1))), "attempt_sequence", "transport_failed"),
    ],
)
def test_provider_contract_violations(suggestion: FallbackSuggestion, violation: str, outcome: str) -> None:
    engine, _ = llm_engine(suggestion)
    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == outcome
    assert decision.llm_receipt is not None
    assert decision.llm_receipt.provider_contract_violation == violation


def test_unknown_provider_outcome_cannot_bind() -> None:
    engine, _ = llm_engine(
        FallbackSuggestion(
            "preference:code_editor",
            1.0,
            (TransportAttempt("rate_limited", 1),),  # type: ignore[arg-type]
        )
    )

    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))

    assert decision.action == "ABSTAIN_ADD"
    assert decision.slot_id is None
    assert decision.fallback_outcome == "invalid_output"
    assert decision.llm_receipt is not None
    assert decision.llm_receipt.provider_contract_violation == "unknown_outcome"


@pytest.mark.parametrize(
    ("attempt", "violation"),
    (
        (TransportAttempt("invalid_output", 1, "invalid response"), None),
        (TransportAttempt("ok", 1, "invalid response"), "excerpt_misuse"),
        (TransportAttempt("invalid_output", 1, "é" * 101), "excerpt_misuse"),
    ),
)
def test_provider_raw_excerpt_contract(attempt: TransportAttempt, violation: str | None) -> None:
    engine, _ = llm_engine(FallbackSuggestion(None, None, (attempt,)))
    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))

    assert decision.fallback_outcome == "invalid_output"
    assert decision.llm_receipt is not None
    assert decision.llm_receipt.provider_contract_violation == violation


def test_provider_ok_beyond_timeout_is_not_selected() -> None:
    engine, _ = llm_engine(FallbackSuggestion("fact:code_editor", 1.0, (TransportAttempt("ok", 21),)))
    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "timeout"


def test_overflow_runs_after_global_checks_when_llm_enabled() -> None:
    registry = load_registry()
    terms = " ".join(term for slot in registry.slots if slot.high_risk for term in slot.hr_lexicon)
    engine, provider = llm_engine(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(uid="force-overflow", value=terms), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "forced_set_overflow"
    assert decision.blocking_receipt is not None and decision.llm_receipt is None
    assert provider.calls == 0


def test_upgraded_overflow_keeps_guard_and_blocking_receipt() -> None:
    registry = load_registry()
    terms = " ".join(term for slot in registry.slots if slot.high_risk for term in slot.hr_lexicon)
    engine, provider = llm_engine(FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(uid="guard-overflow", raw_domain="employer", value=terms), ResolverContext(()), RunBudget(1))
    assert decision.guard_fired is True
    assert decision.fallback_outcome == "forced_set_overflow"
    assert decision.blocking_receipt is not None and decision.llm_receipt is None
    assert provider.calls == 0


def test_split_slot_id_rejects_reserved_and_invalid_values() -> None:
    for value in ("fact:xunres_no", "bad:domain", "fact:", "fact:one:two"):
        with pytest.raises(ValueError):
            split_slot_id(value)


def test_no_nan_confidence_reaches_a_decision() -> None:
    engine, _ = llm_engine(FallbackSuggestion("preference:code_editor", math.nan, (TransportAttempt("ok", 1),)))
    decision = engine.resolve(request(), ResolverContext(()), RunBudget(1))
    assert decision.fallback_outcome == "invalid_output"
