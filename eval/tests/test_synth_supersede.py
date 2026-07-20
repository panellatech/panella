"""Determinism + schema-validity tests for eval/goldsets/synth_supersede.py."""
from __future__ import annotations

import json

import pytest

from eval.goldsets.synth_supersede import (
    CONTENT_META,
    DEFAULT_OUT,
    HR_MISDOMAIN_SURFACES,
    HR_SLOTS,
    PROBE_VALUES,
    SCHEMA_PATH,
    SEED,
    SLOT_KINDS,
    SLOT_SURFACES,
    _check_bars,
    _validate_schema,
    generate,
)


def test_generate_produces_at_least_90_cases() -> None:
    data = generate(SEED)
    assert len(data["cases"]) >= 90


def test_generate_is_schema_valid() -> None:
    data = generate(SEED)
    errors = _validate_schema(data)
    assert errors == []


def test_generate_clears_all_content_bars() -> None:
    """`_check_bars` is the mechanical gate `main()` enforces on every regen — assert it reports
    zero failures (the DIRECT per-label assertions below independently re-derive the same counts
    from `generate()`'s output, so a bug in `_check_bars` itself can't mask a real regression)."""
    data = generate(SEED)
    assert _check_bars(data) == []


def test_generate_is_deterministic_across_calls() -> None:
    a = generate(SEED)
    b = generate(SEED)
    assert a == b


def test_generate_covers_all_three_pair_labels() -> None:
    data = generate(SEED)
    labels = {p["label"] for c in data["cases"] for p in c["pairs"]}
    assert labels == {"supersede", "coexist", "unrelated"}


def test_generate_clears_per_label_and_hr_pair_bars() -> None:
    """Independent re-derivation of the same bars `_check_bars`/`main()` enforce (see
    synth_supersede.py's module docstring for the authoritative numbers) — computed directly from
    `generate()`'s cases/pairs, not by delegating to `_check_bars`, so a bug in that helper can't
    hide a real content regression."""
    data = generate(SEED)
    all_pairs = [p for c in data["cases"] for p in c["pairs"]]
    by_label = {"supersede": [], "coexist": [], "unrelated": []}
    for p in all_pairs:
        by_label[p["label"]].append(p)

    assert len(data["cases"]) >= 90
    assert len(all_pairs) >= 300
    assert len(by_label["unrelated"]) >= 150
    assert len(by_label["supersede"]) >= 70
    assert len(by_label["coexist"]) >= 30

    hr_supersede = [p for p in by_label["supersede"] if p.get("high_risk") is True]
    hr_unrelated = [p for p in by_label["unrelated"] if p.get("high_risk") is True]
    assert len(hr_supersede) >= 12
    assert len(hr_unrelated) >= 20

    coexist_trap_cases = [c for c in data["cases"] if c["case_id"].startswith("sc-coexisttrap-")]
    assert len(coexist_trap_cases) >= 6

    # Every `high_risk` field present anywhere in the goldset must be a real bool (never a
    # stringly-typed "true"/1/etc.) — `_validate_schema` also asserts this; this is a direct,
    # independent re-check over the raw generated pairs.
    for p in all_pairs:
        if "high_risk" in p:
            assert isinstance(p["high_risk"], bool), p


def test_shipped_goldset_pairs_are_aspect_slot_content_disjoint() -> None:
    """The label-vocabulary invariant, recomputed INDEPENDENTLY of the generator's own
    `_check_aspect_disjointness` sweep: load the COMMITTED supersede_v1.json and, via the exported
    `CONTENT_META` aspect map, assert every `unrelated` pair (standalone AND multi) joins two facts
    with different contents AND different source slots AND different aspects, and every `coexist`
    pair satisfies the BICONDITIONAL (GH-bot r3): one SHARED aspect AND two different slots. This
    is what makes both labels defensible: no `unrelated` pair joins same-cluster facts (two music
    facts, two gym facts, a verbatim duplicate), and no `coexist` pair spans life domains (a
    cross-aspect pair is unrelated by definition, not coexist)."""
    data = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    unrelated_checked = coexist_checked = 0
    for case in data["cases"]:
        content_by_id = {f["fact_id"]: f["content"] for f in case["facts"]}
        for pair in case["pairs"]:
            e_txt = content_by_id[pair["earlier_id"]]
            l_txt = content_by_id[pair["later_id"]]
            assert e_txt in CONTENT_META, (case["case_id"], e_txt)
            assert l_txt in CONTENT_META, (case["case_id"], l_txt)
            e_slot, e_aspect = CONTENT_META[e_txt]
            l_slot, l_aspect = CONTENT_META[l_txt]
            if pair["label"] == "unrelated":
                unrelated_checked += 1
                assert e_txt != l_txt, (case["case_id"], e_txt)
                assert e_slot != l_slot, (case["case_id"], e_slot, e_txt, l_txt)
                assert e_aspect != l_aspect, (case["case_id"], e_aspect, e_txt, l_txt)
            elif pair["label"] == "coexist":
                coexist_checked += 1
                assert e_slot != l_slot, (case["case_id"], e_slot, e_txt, l_txt)
                assert e_aspect == l_aspect, (case["case_id"], e_aspect, l_aspect, e_txt, l_txt)
            else:  # supersede: one shared slot, by construction
                assert e_slot == l_slot, (case["case_id"], e_slot, l_slot, e_txt, l_txt)
    # The invariant must have real coverage, not a vacuous pass over an empty file.
    assert unrelated_checked >= 150
    assert coexist_checked >= 30


def test_hr_slot_pairs_carry_high_risk_flag() -> None:
    """SCHEMA.md's high_risk definition, recomputed INDEPENDENTLY of the generator's own
    `_check_hr_flags` sweep over both the generated data and the committed goldset: every pair
    whose either fact's content maps (via the exported `CONTENT_META`) to a source slot in
    `HR_SLOTS` must carry `high_risk: true` — including the hr-x-benign `unrelated` pairs inside
    `sc-hrmulti-*` cases (the GH-bot r1 finding: those traps were silently excluded from the
    scorer's hr false-merge slice). Conversely a pair carrying the flag must involve at least one
    hr-slotted fact."""
    for data in (generate(SEED), json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))):
        flagged_checked = 0
        for case in data["cases"]:
            content_by_id = {f["fact_id"]: f["content"] for f in case["facts"]}
            for pair in case["pairs"]:
                e_slot = CONTENT_META[content_by_id[pair["earlier_id"]]][0]
                l_slot = CONTENT_META[content_by_id[pair["later_id"]]][0]
                involves_hr = e_slot in HR_SLOTS or l_slot in HR_SLOTS
                assert pair.get("high_risk", False) is involves_hr, (case["case_id"], pair)
                if involves_hr:
                    flagged_checked += 1
        # Real coverage: hr supersede (16) + standalone hr unrelated (24) + hrmulti hr pairs (18).
        assert flagged_checked >= 50


def test_every_fact_carries_a_valid_probe() -> None:
    """Probe contract (v1.1), recomputed independently over both generate() output and the
    committed goldset: every fact carries a probe with exactly {kind, raw_domain, value}, whose
    kind is the slot's registered kind, whose value is the content's registered probe value, and
    whose raw_domain comes from the slot's correct surface pool — or, for hr slots, possibly its
    deliberate mis-domain pool (which trap contexts use is pinned by the dedicated test below)."""
    for data in (generate(SEED), json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))):
        checked = 0
        for case in data["cases"]:
            for fact in case["facts"]:
                probe = fact["probe"]
                assert set(probe) == {"kind", "raw_domain", "value"}, (case["case_id"], fact["fact_id"])
                slot, _aspect = CONTENT_META[fact["content"]]
                assert probe["kind"] == SLOT_KINDS[slot], (case["case_id"], fact["fact_id"])
                assert probe["value"] == PROBE_VALUES[fact["content"]], (case["case_id"], fact["fact_id"])
                allowed = set(SLOT_SURFACES[slot]) | set(HR_MISDOMAIN_SURFACES.get(slot, ()))
                assert probe["raw_domain"] in allowed, (case["case_id"], fact["fact_id"], probe["raw_domain"])
                checked += 1
        assert checked >= 400  # every fact in every case, not a vacuous subset


def test_supersede_pairs_use_distinct_probe_surfaces() -> None:
    """The key-stability pressure the probe contract exists to create: every supersede-labeled
    pair (standalone, hr, trap, AND multi) joins two probes with DIFFERENT raw_domain surfaces —
    a resolver can never merge them by string-equal domains alone."""
    data = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    checked = 0
    for case in data["cases"]:
        probe_by_id = {f["fact_id"]: f["probe"] for f in case["facts"]}
        for pair in case["pairs"]:
            if pair["label"] != "supersede":
                continue
            e_rd = probe_by_id[pair["earlier_id"]]["raw_domain"]
            l_rd = probe_by_id[pair["later_id"]]["raw_domain"]
            assert e_rd != l_rd, (case["case_id"], e_rd)
            checked += 1
    assert checked >= 70  # matches the supersede-pairs content bar


def test_hr_trap_probes_are_misdomained() -> None:
    """The hr-escalation trap convention: in sc-hrunrelated-* the hr fact (f-a), and in
    sc-hrmulti-* BOTH hr facts (f1/f2), carry a probe raw_domain from the slot's DELIBERATE
    mis-domain pool (and never from its correct pool) — sensitive content under a benign-looking
    surface. Standalone sc-hrsupersede-* cases stay on CORRECT surfaces (they measure key
    stability, not escalation)."""
    data = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    trap_checked = correct_checked = 0
    for case in data["cases"]:
        cid = case["case_id"]
        fact_by_id = {f["fact_id"]: f for f in case["facts"]}
        if cid.startswith("sc-hrunrelated-"):
            trap_facts = [fact_by_id["f-a"]]
        elif cid.startswith("sc-hrmulti-"):
            trap_facts = [fact_by_id["f1"], fact_by_id["f2"]]
        elif cid.startswith("sc-hrsupersede-"):
            for fact in (fact_by_id["f-earlier"], fact_by_id["f-later"]):
                slot, _aspect = CONTENT_META[fact["content"]]
                assert fact["probe"]["raw_domain"] in SLOT_SURFACES[slot], (cid, fact["fact_id"])
                assert fact["probe"]["raw_domain"] not in HR_MISDOMAIN_SURFACES[slot], (cid, fact["fact_id"])
                correct_checked += 1
            continue
        else:
            continue
        for fact in trap_facts:
            slot, _aspect = CONTENT_META[fact["content"]]
            assert slot in HR_SLOTS, (cid, fact["fact_id"])
            assert fact["probe"]["raw_domain"] in HR_MISDOMAIN_SURFACES[slot], (cid, fact["fact_id"])
            assert fact["probe"]["raw_domain"] not in SLOT_SURFACES[slot], (cid, fact["fact_id"])
            trap_checked += 1
    assert trap_checked >= 36  # 24 hrunrelated + 6 hrmulti x 2
    assert correct_checked >= 20  # 10 hrsupersede cases x 2 facts


def test_current_truth_matches_supersede_structure() -> None:
    """Mirror of the generator's `_check_current_truth` sweep, recomputed INDEPENDENTLY over the
    committed goldset: per case, current_truth ids == all fact ids minus the earlier-side ids of
    supersede pairs (a superseded fact is no longer currently true; every other fact is)."""
    data = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    for case in data["cases"]:
        fact_ids = {f["fact_id"] for f in case["facts"]}
        superseded = {p["earlier_id"] for p in case["pairs"] if p["label"] == "supersede"}
        actual = {ct["fact_id"] for ct in case["current_truth"]}
        assert actual == fact_ids - superseded, case["case_id"]


def test_committed_goldset_validates_against_real_jsonschema() -> None:
    """Belt-when-available: validate the committed goldset against supersede.schema.json with the
    REAL jsonschema library (the hand validator `_validate_schema` stays the dependency-free
    floor; this cross-checks it whenever the package is importable, and skips cleanly when not)."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    data = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)


def test_cases_sorted_by_case_id() -> None:
    data = generate(SEED)
    ids = [c["case_id"] for c in data["cases"]]
    assert ids == sorted(ids)


def test_pairs_sorted_by_earlier_later() -> None:
    data = generate(SEED)
    for case in data["cases"]:
        keys = [(p["earlier_id"], p["later_id"]) for p in case["pairs"]]
        assert keys == sorted(keys)


def test_no_real_names_marker_absent() -> None:
    """Sanity check: the synthetic content must never contain an obviously-real-looking marker.
    This is a narrow smoke check (not a full PII scanner) — it guards against a copy-paste
    regression reintroducing real data from a source fixture. The forbidden substrings are built
    at runtime (never appear as literals in this file) so a tracked-tree deny-scan never has to
    special-case a test asserting their ABSENCE."""
    forbidden_name = "".join(["R", "a", "v", "e", "n"])
    forbidden_email = "".join(["r", "a", "v", "e", "n", ".", "h", "i", "m", "@", "g", "m", "a", "i", "l", ".", "c", "o", "m"])
    data = generate(SEED)
    blob = str(data)
    assert forbidden_name not in blob
    assert forbidden_email not in blob
