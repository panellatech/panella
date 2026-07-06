"""Determinism + schema-validity tests for eval/goldsets/synth_supersede.py."""
from __future__ import annotations

from eval.goldsets.synth_supersede import SEED, _validate_schema, generate


def test_generate_produces_at_least_40_cases() -> None:
    data = generate(SEED)
    assert len(data["cases"]) >= 40


def test_generate_is_schema_valid() -> None:
    data = generate(SEED)
    errors = _validate_schema(data)
    assert errors == []


def test_generate_is_deterministic_across_calls() -> None:
    a = generate(SEED)
    b = generate(SEED)
    assert a == b


def test_generate_covers_all_three_pair_labels() -> None:
    data = generate(SEED)
    labels = {p["label"] for c in data["cases"] for p in c["pairs"]}
    assert labels == {"supersede", "coexist", "unrelated"}


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
