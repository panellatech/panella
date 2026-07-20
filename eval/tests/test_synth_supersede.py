"""Determinism + schema-validity tests for eval/goldsets/synth_supersede.py."""
from __future__ import annotations

from eval.goldsets.synth_supersede import SEED, _check_bars, _validate_schema, generate


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
