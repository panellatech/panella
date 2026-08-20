"""Hermetic coverage for the v2b blocking diagnostic seam and selection contract."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace

import pytest

from eval.goldsets import resolver_blocking_diag as diag
from eval.goldsets.key_correctness_eval import load_items
from panella.resolver import engine as resolver_engine
from panella.resolver.registry import RegistrySlot, SlotRegistry


@pytest.fixture(autouse=True)
def _allow_hermetic_registry_in_resolver_engine(monkeypatch):
    monkeypatch.setattr(resolver_engine, "MIN_REGISTRY_SLOTS", 1)
    monkeypatch.setattr(resolver_engine, "PINNED_REGISTRY_HASH", "r" * 64)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_artifact(tmp_path, **overrides):
    source_uids = {item.item_id for item in load_items(diag.EXTRACTION_SOURCES["source_items"])}
    artifact = {
        "candidates": {uid: [] for uid in source_uids},
        "n_items": len(source_uids),
        "source_items": {"sha256": diag._sha256(diag.EXTRACTION_SOURCES["source_items"])},
        "source_fixture": {"sha256": diag._sha256(diag.EXTRACTION_SOURCES["source_fixture"])},
    }
    artifact.update(overrides)
    path = tmp_path / "fake_candidates.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    return path


def _allow(monkeypatch, path):
    monkeypatch.setattr(diag, "CANDIDATE_HASH_ALLOWLIST", frozenset({diag._sha256(path)}))


def _pin_injected_production_sources(monkeypatch, inputs):
    monkeypatch.setattr(diag, "EXTRACTION_SOURCE_ITEMS_SHA256", inputs.extraction_source_items_sha256)
    monkeypatch.setattr(diag, "EXTRACTION_SOURCE_FIXTURE_SHA256", inputs.extraction_source_fixture_sha256)
    monkeypatch.setattr(diag, "RETENTION_LEDGER_SHA256", inputs.retention_ledger_sha256)


def test_admission_accepts_allowlisted_artifact(tmp_path, monkeypatch):
    path = _fake_artifact(tmp_path)
    _allow(monkeypatch, path)
    assert set(diag._load_candidates(path)) == {
        item.item_id for item in load_items(diag.EXTRACTION_SOURCES["source_items"])
    }


def test_admission_rejects_unlisted_hash(tmp_path):
    path = _fake_artifact(tmp_path)
    with pytest.raises(ValueError, match="not allowlisted"):
        diag._load_candidates(path)


def test_admission_rejects_declared_count_mismatch(tmp_path, monkeypatch):
    path = _fake_artifact(tmp_path, n_items=3)
    _allow(monkeypatch, path)
    with pytest.raises(ValueError, match="n_items"):
        diag._load_candidates(path)


def test_admission_rejects_source_hash_mismatch(tmp_path, monkeypatch):
    path = _fake_artifact(tmp_path, source_items={"sha256": "0" * 64})
    _allow(monkeypatch, path)
    with pytest.raises(ValueError, match="source"):
        diag._load_candidates(path)


def test_admission_rejects_empty_uid(tmp_path, monkeypatch):
    path = _fake_artifact(tmp_path, candidates={"": []}, n_items=1)
    _allow(monkeypatch, path)
    with pytest.raises(ValueError, match="non-empty"):
        diag._load_candidates(path)


@pytest.mark.parametrize("mutation", ("extra", "missing", "renamed"))
def test_admission_rejects_non_bijective_candidate_item_set(tmp_path, monkeypatch, mutation):
    source_uids = {item.item_id for item in load_items(diag.EXTRACTION_SOURCES["source_items"])}
    candidates = {uid: [] for uid in source_uids}
    removed = next(iter(candidates))
    if mutation == "extra":
        candidates["extra-item"] = []
    elif mutation == "missing":
        del candidates[removed]
    else:
        del candidates[removed]
        candidates["renamed-item"] = []
    path = _fake_artifact(tmp_path, candidates=candidates, n_items=len(candidates))
    _allow(monkeypatch, path)
    with pytest.raises(ValueError, match="exact bijection"):
        diag._load_candidates(path)


def test_admission_rejects_duplicate_json_keys(tmp_path, monkeypatch):
    path = _fake_artifact(tmp_path)
    n_items = len(load_items(diag.EXTRACTION_SOURCES["source_items"]))
    path.write_text(path.read_text(encoding="utf-8")[:-1] + f',"n_items":{n_items}}}', encoding="utf-8")
    _allow(monkeypatch, path)
    with pytest.raises(ValueError, match="duplicate keys"):
        diag._load_candidates(path)


def test_production_source_hashes_are_literal_pins() -> None:
    assert diag._sha256(diag.EXTRACTION_SOURCES["source_items"]) == diag.EXTRACTION_SOURCE_ITEMS_SHA256
    assert diag._sha256(diag.EXTRACTION_SOURCES["source_fixture"]) == diag.EXTRACTION_SOURCE_FIXTURE_SHA256
    assert diag._sha256(diag.LEDGER_PATH) == diag.RETENTION_LEDGER_SHA256


@pytest.mark.parametrize(
    ("unresolved", "retention_pass", "approved_remap_eliminated", "expected_exit"),
    (
        (False, True, True, 0),
        (True, True, True, 1),
        (False, False, True, 1),
        (False, True, False, 1),
    ),
)
def test_main_exit_and_report_pass_cover_every_pair_face(
    tmp_path, monkeypatch, capsys, unresolved, retention_pass, approved_remap_eliminated, expected_exit
):
    monkeypatch.setattr(diag, "OUT_DIR", tmp_path)
    monkeypatch.setattr(
        diag,
        "_run_pair",
        lambda: (
            {
                "pair_classification": {"unresolved_semantic": int(unresolved)},
                "det": {
                    "retention": {
                        "pass": retention_pass,
                        "approved_remap_eliminated": approved_remap_eliminated,
                    }
                },
            },
            [],
        ),
    )

    assert diag.main([]) == expected_exit
    assert json.loads(capsys.readouterr().out)["pass"] is (expected_exit == 0)


def _registry() -> SlotRegistry:
    slot = RegistrySlot(
        "preference:sport", "preference", "sport", "sport updates", False, (), (), (), None, "test", ()
    )
    return SlotRegistry("test", (slot,), "r" * 64, "s" * 64, "t" * 64, MappingProxyType({slot.slot_id: slot}), MappingProxyType({}), MappingProxyType({}))


def _inputs(tmp_path):
    pair = {
        "cases": [{
            "case_id": "case",
            "facts": [
                {"fact_id": "f1", "probe": {"kind": "preference", "raw_domain": "sports", "value": "daily"}, "content": "daily sports", "date": "2026-01-01"},
                {"fact_id": "f2", "probe": {"kind": "preference", "raw_domain": "unmapped", "value": "sports"}, "content": "sports", "date": "2026-01-02"},
                {"fact_id": "f3", "probe": {"kind": "preference", "raw_domain": "unmapped_two", "value": "sports"}, "content": "sports", "date": "2026-01-03"},
            ],
            "pairs": [
                {"earlier_id": "f1", "later_id": "f2", "label": "supersede"},
                {"earlier_id": "f2", "later_id": "f3", "label": "supersede"},
                {"earlier_id": "f1", "later_id": "f3", "label": "unrelated"},
            ],
        }]
    }
    candidates = {"n_items": 1, "candidates": {"item": [{"source_sid": "item", "kind": "preference", "raw_domain": "unmapped", "value": "", "evidence_text": ""}]}}
    ledger = {"cases": [{"request_uid": "case/f1", "hit_slot": "preference:sport", "initial_state": "must_retain_correct"}]}
    paths = {}
    for name, document in {"pair": pair, "candidates": candidates, "items": {"extra_items": [{"id": "item", "text": "synthetic source"}]}, "fixture": {"lifecycles": []}, "ledger": ledger}.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        paths[name] = path
    return diag.DiagnosticInputs(
        paths["pair"], _hash(paths["pair"]), paths["candidates"], frozenset({_hash(paths["candidates"])}),
        paths["items"], _hash(paths["items"]), paths["fixture"], _hash(paths["fixture"]),
        paths["ledger"], _hash(paths["ledger"]), tmp_path / "out",
        {"facts": 3, "pairs": 3, "sup_pairs": 2, "negative_pairs": 1},
    )


def _bundle(inputs, baseline):
    return {"c1_merged": baseline, "cbfd01c": {"commit": "cbfd01c", "absent": "historical baseline unavailable"}}


def _cohort_row(slot_id, choice_set, *, guard_fired=False):
    return {
        "det": {"slot_id": slot_id, "guard_fired": guard_fired},
        "receipt": SimpleNamespace(choice_set=choice_set),
    }


def test_zero_deterministic_pair_with_intersection_is_reachable():
    pairs = [{"case_id": "case", "earlier_id": "earlier", "later_id": "later", "label": "supersede"}]
    rows = {
        "case/earlier": _cohort_row(None, ("shared", "earlier-only")),
        "case/later": _cohort_row(None, ("shared", "later-only")),
    }

    cohort, mrr = diag._cohort_metrics(pairs, rows)

    assert cohort == {"both_det_same": 0, "det_diff": 0, "reachable": 1, "structural": 0}
    assert mrr == {"num": 0, "den": 0, "gold_source": "det_anchor_proxy"}


def test_zero_deterministic_pair_without_intersection_is_structural():
    pairs = [{"case_id": "case", "earlier_id": "earlier", "later_id": "later", "label": "supersede"}]
    rows = {
        "case/earlier": _cohort_row(None, ("earlier-only",)),
        "case/later": _cohort_row(None, ("later-only",)),
    }

    cohort, _ = diag._cohort_metrics(pairs, rows)

    assert cohort == {"both_det_same": 0, "det_diff": 0, "reachable": 0, "structural": 1}


def test_deterministic_anchor_mrr_is_mean_reciprocal_rank():
    pairs = [
        {"case_id": "case", "earlier_id": "one-anchor", "later_id": "one-missed", "label": "supersede"},
        {"case_id": "case", "earlier_id": "two-anchor", "later_id": "two-missed", "label": "supersede"},
    ]
    rows = {
        "case/one-anchor": _cohort_row("one", ()),
        "case/one-missed": _cohort_row(None, ("one",)),
        "case/two-anchor": _cohort_row("two", ()),
        "case/two-missed": _cohort_row(None, ("other", "two")),
    }

    cohort, mrr = diag._cohort_metrics(pairs, rows)

    assert cohort == {"both_det_same": 0, "det_diff": 0, "reachable": 2, "structural": 0}
    assert mrr == {"num": 3, "den": 4, "gold_source": "det_anchor_proxy"}


def test_core_metrics_rejects_incomplete_cohort_partition():
    metrics = {
        "negative_overlap": {"n": 0, "d": 1},
        "migration": {"n": 0, "d": 2},
        "overflow": {"n": 0, "d": 2},
        "empty_pair_face": {"n": 0, "d": 2},
        "empty_extraction_face": {"n": 0, "d": 1},
        "cohort": {"both_det_same": 1, "det_diff": 0, "reachable": 0, "structural": 0},
        "pool_size_distribution": {"0": 3},
    }

    with pytest.raises(ValueError, match="cohort"):
        diag._validate_core_metrics(metrics, expected_cardinalities={"facts": 2, "pairs": 2, "sup_pairs": 2, "negative_pairs": 1}, expected_extraction_rows=1)


@pytest.mark.parametrize("metric", ("migration", "overflow"))
def test_core_metrics_requires_pair_face_denominator(metric):
    metrics = {
        "negative_overlap": {"n": 0, "d": 1},
        "migration": {"n": 0, "d": 2},
        "overflow": {"n": 0, "d": 2},
        "empty_pair_face": {"n": 0, "d": 2},
        "empty_extraction_face": {"n": 0, "d": 1},
        "cohort": {"both_det_same": 2, "det_diff": 0, "reachable": 0, "structural": 0},
        "pool_size_distribution": {"0": 3},
    }
    metrics[metric]["d"] = 3

    with pytest.raises(ValueError, match="denominator"):
        diag._validate_core_metrics(metrics, expected_cardinalities={"facts": 2, "pairs": 2, "sup_pairs": 2, "negative_pairs": 1}, expected_extraction_rows=1)


def test_migration_and_overflow_metrics_exclude_extraction_face():
    pair_rows = {
        "pair": {
            "receipt": SimpleNamespace(slice="hr", choice_set=()),
            "risk_any": False,
            "forced_overflow": False,
        }
    }
    extraction_rows = {
        "extraction": {
            "receipt": SimpleNamespace(slice="hr", choice_set=()),
            "risk_any": False,
            "forced_overflow": True,
        }
    }

    metrics = diag._metrics(pair_rows, extraction_rows, [])

    assert metrics["migration"] == {"n": 1, "d": 1}
    assert metrics["overflow"] == {"n": 0, "d": 1}
    assert metrics["pool_size_distribution"] == {"0": 2}


def _cell(k=8, theta=(3, 20), *, recall=(1, 2), pollution=(0, 1), predicates=None):
    base = {
        "negative_overlap": {"n": 0, "d": 1}, "migration": {"n": 0, "d": 4}, "overflow": {"n": 0, "d": 4},
        "pollution": {"n": pollution[0], "d": pollution[1]}, "empty_pair_face": {"n": 0, "d": 3}, "empty_extraction_face": {"n": 1, "d": 1},
        "cohort": {"both_det_same": 0, "det_diff": 0, "reachable": recall[0], "structural": recall[1] - recall[0]},
        "det_miss_recall": {"n": recall[0], "d": recall[1]}, "det_anchor_mrr": {"num": 1, "den": 1, "gold_source": "det_anchor_proxy"}, "pool_size_distribution": {"0": 1, "1": 3},
    }
    return {"k": k, "theta": list(theta), "v2_instrument_rules_hash": diag.blocking_v2_instrument_rules_hash_for(k, theta), "metrics": base, "predicates": predicates or {"negative_overlap_ok": True, "migration_ok": True, "overflow_ok": True, "pollution_ok": True, "det_zero_delta_ok": True, "feasible": True}}


def test_baseline_is_hermetic_and_replays_guarded_target(tmp_path):
    inputs = _inputs(tmp_path)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    assert baseline["metrics"]["cohort"] == {"both_det_same": 0, "det_diff": 0, "reachable": 2, "structural": 0}
    assert baseline["metrics"]["migration"]["d"] == inputs.expected_cardinalities["facts"]
    assert baseline["metrics"]["overflow"]["d"] == inputs.expected_cardinalities["facts"]
    assert "det_miss_recall" not in baseline["metrics"]
    assert baseline["metrics"]["empty_extraction_face"] == {"n": 1, "d": 1}
    assert baseline["deterministic_by_uid"]["case/f1"]["retention_ledger"] is True


def test_deterministic_map_uses_engine_not_guard_target(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    pair = json.loads(inputs.pair_goldset_path.read_text(encoding="utf-8"))
    pair["cases"][0]["facts"][0]["probe"]["raw_domain"] = "guarded_only"
    inputs.pair_goldset_path.write_text(json.dumps(pair), encoding="utf-8")
    inputs = replace(inputs, pair_goldset_sha256=_hash(inputs.pair_goldset_path))
    slot = _registry().slots[0]
    monkeypatch.setattr(diag, "prepare_guard", lambda *_: SimpleNamespace(target=slot, method="exact", guard_fired=True))

    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")

    assert baseline["deterministic_by_uid"]["case/f1"] == {
        "slot_id": None,
        "method": "none",
        "guard_fired": False,
        "retention_ledger": False,
    }


def test_baseline_out_cli_runs_against_v1_with_temp_output(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    monkeypatch.setattr(diag, "PAIR_GOLDSET", inputs.pair_goldset_path)
    monkeypatch.setattr(diag, "PAIR_GOLDSET_SHA256", inputs.pair_goldset_sha256)
    monkeypatch.setattr(diag, "EXTRACTION_SOURCES", {"source_items": inputs.extraction_source_items_path, "source_fixture": inputs.extraction_source_fixture_path})
    monkeypatch.setattr(diag, "LEDGER_PATH", inputs.retention_ledger_path)
    _pin_injected_production_sources(monkeypatch, inputs)
    monkeypatch.setattr(diag, "CANDIDATE_HASH_ALLOWLIST", inputs.candidate_allowlist)
    monkeypatch.setattr(diag, "_PRODUCTION_CARDINALITIES", dict(inputs.expected_cardinalities))
    monkeypatch.setattr(diag, "load_registry", _registry)
    output = tmp_path / "baseline.json"
    assert diag.main(["--candidates", str(inputs.candidate_path), "--baseline-out", str(output), "--produced-by", "chief", "--commit", "c1", "--description-remediation-commit", "c0"]) == 0
    assert set(json.loads(output.read_text(encoding="utf-8"))) == {"c1_merged"}


def _configure_sweep_cli(monkeypatch, inputs):
    monkeypatch.setattr(diag, "PAIR_GOLDSET", inputs.pair_goldset_path)
    monkeypatch.setattr(diag, "PAIR_GOLDSET_SHA256", inputs.pair_goldset_sha256)
    monkeypatch.setattr(diag, "EXTRACTION_SOURCES", {"source_items": inputs.extraction_source_items_path, "source_fixture": inputs.extraction_source_fixture_path})
    monkeypatch.setattr(diag, "LEDGER_PATH", inputs.retention_ledger_path)
    _pin_injected_production_sources(monkeypatch, inputs)
    monkeypatch.setattr(diag, "CANDIDATE_HASH_ALLOWLIST", inputs.candidate_allowlist)
    monkeypatch.setattr(diag, "_PRODUCTION_CARDINALITIES", dict(inputs.expected_cardinalities))
    monkeypatch.setattr(diag, "load_registry", _registry)


def _sweep_args(inputs, bundle_path, output_path, bundle_hash):
    return [
        "--candidates", str(inputs.candidate_path),
        "--baseline-bundle", str(bundle_path),
        "--baseline-bundle-sha256", bundle_hash,
        "--sweep-out", str(output_path),
        "--commit", "c2",
    ]


def test_sweep_cli_rejects_baseline_bundle_sha_mismatch(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    _configure_sweep_cli(monkeypatch, inputs)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_bundle(inputs, baseline)), encoding="utf-8")

    with pytest.raises(ValueError, match=r"expected=000000000000 actual=[0-9a-f]{12}"):
        diag.main(_sweep_args(inputs, bundle_path, tmp_path / "sweep.json", "0" * 64))


def test_sweep_cli_rejects_metrics_that_do_not_recompute(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    _configure_sweep_cli(monkeypatch, inputs)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    bundle = _bundle(inputs, baseline)
    bundle["c1_merged"] = dict(baseline, metrics=dict(baseline["metrics"], negative_overlap={"n": 0, "d": 1}))
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    bundle_hash = _hash(bundle_path)

    with pytest.raises(ValueError, match="metrics differ at negative_overlap"):
        diag.main(_sweep_args(inputs, bundle_path, tmp_path / "sweep.json", bundle_hash))


def test_sweep_cli_rejects_deterministic_map_that_does_not_recompute(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    _configure_sweep_cli(monkeypatch, inputs)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    deterministic = dict(baseline["deterministic_by_uid"])
    deterministic["case/f1"] = dict(deterministic["case/f1"], retention_ledger=False)
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_bundle(inputs, dict(baseline, deterministic_by_uid=deterministic))), encoding="utf-8")
    bundle_hash = _hash(bundle_path)

    with pytest.raises(ValueError, match=r"deterministic map differs at case/f1.retention_ledger"):
        diag.main(_sweep_args(inputs, bundle_path, tmp_path / "sweep.json", bundle_hash))


def test_sweep_cli_recomputes_baseline_and_writes_v2b_sweep_3(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    _configure_sweep_cli(monkeypatch, inputs)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_bundle(inputs, baseline)), encoding="utf-8")
    bundle_hash = _hash(bundle_path)
    output = tmp_path / "sweep.json"

    assert diag.main(_sweep_args(inputs, bundle_path, output, bundle_hash)) == 0

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema_version"] == "v2b-sweep-3"
    assert document["baseline_recomputed"] is True
    assert document["baseline_bundle_sha256"] == bundle_hash


def test_cardinality_and_candidate_source_sid_fail_closed(tmp_path):
    inputs = _inputs(tmp_path)
    with pytest.raises(ValueError, match="cardinality"):
        diag.build_baseline(
            replace(inputs, expected_cardinalities={"facts": 4, "pairs": 3, "sup_pairs": 2, "negative_pairs": 1}),
            registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0",
        )
    document = json.loads(inputs.candidate_path.read_text(encoding="utf-8"))
    document["candidates"]["item"][0]["source_sid"] = "wrong"
    inputs.candidate_path.write_text(json.dumps(document), encoding="utf-8")
    bad = replace(inputs, candidate_allowlist=frozenset({_hash(inputs.candidate_path)}))
    with pytest.raises(ValueError, match="source_sid"):
        diag.build_baseline(bad, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")


def test_baseline_bundle_two_cbfd_forms_and_fail_closed_validation(tmp_path):
    inputs = _inputs(tmp_path)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    kwargs = {
        "input_hashes": inputs.input_hashes(),
        "registry_hash": "r" * 64,
        "expected_cardinalities": inputs.expected_cardinalities,
        "expected_extraction_rows": 1,
        "expected_deterministic_uids": set(baseline["deterministic_by_uid"]),
    }
    absent = _bundle(inputs, baseline)
    assert diag._validate_baseline_bundle(absent, **kwargs)["c1_merged"] == baseline
    present = _bundle(inputs, baseline)
    present["cbfd01c"] = {"commit": "cbfd01c", "metrics": baseline["metrics"]}
    diag._validate_baseline_bundle(present, **kwargs)
    broken = _bundle(inputs, baseline)
    broken["unknown"] = True
    with pytest.raises(ValueError, match="exactly"):
        diag._validate_baseline_bundle(broken, **kwargs)
    broken = _bundle(inputs, baseline)
    broken["c1_merged"] = dict(baseline, registry_hash="wrong")
    with pytest.raises(ValueError, match="identity"):
        diag._validate_baseline_bundle(broken, **kwargs)
    broken = _bundle(inputs, baseline)
    broken["c1_merged"] = dict(baseline, metrics=dict(baseline["metrics"], negative_overlap={"n": 0, "d": 99}))
    with pytest.raises(ValueError, match="denominator"):
        diag._validate_baseline_bundle(broken, **kwargs)


@pytest.mark.parametrize("tamper", ("extraction_denominator", "pool_cardinality", "deterministic_shape"))
def test_baseline_bundle_rejects_new_cross_face_tampers(tmp_path, tamper):
    inputs = _inputs(tmp_path)
    baseline = diag.build_baseline(inputs, registry=_registry(), commit="c1", produced_by="chief", description_remediation_commit="c0")
    broken = _bundle(inputs, baseline)
    c1 = dict(broken["c1_merged"])
    metrics = dict(baseline["metrics"])
    if tamper == "extraction_denominator":
        metrics["empty_extraction_face"] = {"n": 0, "d": 2}
    elif tamper == "pool_cardinality":
        metrics["pool_size_distribution"] = {"0": 1}
    else:
        deterministic = dict(baseline["deterministic_by_uid"])
        deterministic.pop(next(iter(deterministic)))
        c1["deterministic_by_uid"] = deterministic
    c1["metrics"] = metrics
    broken["c1_merged"] = c1

    with pytest.raises(ValueError):
        diag._validate_baseline_bundle(
            broken,
            input_hashes=inputs.input_hashes(),
            registry_hash="r" * 64,
            expected_cardinalities=inputs.expected_cardinalities,
            expected_extraction_rows=1,
            expected_deterministic_uids=set(baseline["deterministic_by_uid"]),
        )


def test_cross_multiplied_predicates_and_theta_order():
    baseline = {"negative_overlap": {"n": 1, "d": 50}, "migration": {"n": 0, "d": 50}, "overflow": {"n": 1, "d": 50}}
    metrics = {"negative_overlap": {"n": 2, "d": 50}, "migration": {"n": 1, "d": 50}, "overflow": {"n": 1, "d": 50}, "pollution": {"n": 1, "d": 4}}
    assert diag.evaluate_predicates(metrics, baseline, det_zero_delta=True)["feasible"] is True
    assert diag._theta_sort_key((3, 20)) < diag._theta_sort_key((1, 5))
    assert diag._theta_sort_key((3, 20)) < diag._theta_sort_key((3, 10))


def test_selection_schema_failure_order_tie_break_and_infeasible_cell(tmp_path):
    failed = _cell(predicates={"negative_overlap_ok": True, "migration_ok": False, "overflow_ok": False, "pollution_ok": False, "det_zero_delta_ok": False, "feasible": False})
    lower_theta = _cell(theta=(3, 20), recall=(1, 2))
    higher_theta = _cell(theta=(1, 5), recall=(1, 2))
    selected, trace = diag.select_grid([failed, lower_theta, higher_theta])
    assert failed["predicates"]["first_failure"] == "migration_ok"
    assert selected == {"k": 8, "theta": (1, 5)}
    assert trace[0].endswith("migration_ok")
    impossible = _cell(pollution=(0, 0), predicates={"negative_overlap_ok": True, "migration_ok": True, "overflow_ok": True, "pollution_ok": False, "det_zero_delta_ok": True, "feasible": False})
    assert diag.select_grid([impossible])[0] is None
    document = diag.sweep_document(baseline_bundle={"c1_merged": {}}, baseline_bundle_sha256="a" * 64, input_hashes={name: "b" * 64 for name in diag._INPUT_HASH_KEYS}, cells=[failed, lower_theta, higher_theta], produced_at_commit="c2")
    assert set(document) == {"schema_version", "v1_operative_rules_hash", "produced_at_commit", "baseline_recomputed", "baseline_bundle_sha256", "input_hashes", "cells", "selected_grid", "selection_trace"}
    assert document["schema_version"] == "v2b-sweep-3"
    assert document["baseline_recomputed"] is True
    assert document["v1_operative_rules_hash"] == diag.blocking_v1_operative_rules_hash()
    assert set(document["cells"][0]) == {"k", "theta", "v2_instrument_rules_hash", "metrics", "predicates"}
    assert document["cells"][0]["v2_instrument_rules_hash"] == diag.blocking_v2_instrument_rules_hash_for(8, (3, 20))
    assert set(document["cells"][0]["metrics"]) == {"negative_overlap", "migration", "overflow", "pollution", "empty_pair_face", "empty_extraction_face", "cohort", "det_miss_recall", "det_anchor_mrr", "pool_size_distribution"}
    assert set(document["cells"][0]["predicates"]) == {"negative_overlap_ok", "migration_ok", "overflow_ok", "pollution_ok", "det_zero_delta_ok", "feasible", "first_failure"}
    assert document["cells"][-1]["predicates"]["first_failure"] is None


def test_sweep_document_rejects_a_cell_with_a_mismatched_instrument_hash():
    cell = _cell()
    cell["v2_instrument_rules_hash"] = "0" * 64
    with pytest.raises(ValueError, match="instrument rules hash"):
        diag.sweep_document(
            baseline_bundle={"c1_merged": {}},
            baseline_bundle_sha256="a" * 64,
            input_hashes={name: "b" * 64 for name in diag._INPUT_HASH_KEYS},
            cells=[cell],
            produced_at_commit="c2",
        )
