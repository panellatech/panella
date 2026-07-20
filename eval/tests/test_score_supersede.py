"""Unit tests for eval/goldsets/score_supersede.py — pure-function confusion-matrix scorer,
tested on a hand-built miniature goldset (not the full synthetic v0 set)."""
from __future__ import annotations

from eval.goldsets.score_supersede import score

_MINI_GOLDSET = {
    "goldset": "panella-supersede-confusion-matrix",
    "version": "v0",
    "cases": [
        {
            "case_id": "mini-1",
            "facts": [
                {"fact_id": "a", "content": "works at Placeholder Co", "date": "2024-01-01T00:00:00Z"},
                {"fact_id": "b", "content": "now works at Other Co", "date": "2024-06-01T00:00:00Z"},
            ],
            "pairs": [{"earlier_id": "a", "later_id": "b", "label": "supersede"}],
            "current_truth": [{"fact_id": "b", "rationale": "latest employer fact"}],
        },
        {
            "case_id": "mini-2",
            "facts": [
                {"fact_id": "a", "content": "avoids gluten", "date": "2024-01-01T00:00:00Z"},
                {"fact_id": "b", "content": "enjoys jazz music", "date": "2024-02-01T00:00:00Z"},
            ],
            "pairs": [{"earlier_id": "a", "later_id": "b", "label": "coexist"}],
            "current_truth": [
                {"fact_id": "a", "rationale": "independent"},
                {"fact_id": "b", "rationale": "independent"},
            ],
        },
        {
            "case_id": "mini-3",
            "facts": [
                {"fact_id": "a", "content": "works at Placeholder Co", "date": "2024-01-01T00:00:00Z"},
                {"fact_id": "b", "content": "drinks black coffee", "date": "2024-03-01T00:00:00Z"},
            ],
            "pairs": [{"earlier_id": "a", "later_id": "b", "label": "unrelated"}],
            "current_truth": [
                {"fact_id": "a", "rationale": "independent"},
                {"fact_id": "b", "rationale": "independent"},
            ],
        },
    ],
}


def test_perfect_predictions_yield_perfect_precision_recall() -> None:
    predictions = [
        {"case_id": "mini-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
        {"case_id": "mini-2", "earlier_id": "a", "later_id": "b", "predicted_label": "coexist"},
        {"case_id": "mini-3", "earlier_id": "a", "later_id": "b", "predicted_label": "unrelated"},
    ]
    report = score(_MINI_GOLDSET, predictions)
    assert report.precision == {"supersede": 1.0, "coexist": 1.0, "unrelated": 1.0}
    assert report.recall == {"supersede": 1.0, "coexist": 1.0, "unrelated": 1.0}
    assert report.coverage == 1.0
    assert report.false_merge_count == 0
    assert report.n_covered == 3
    assert report.n_missing == 0


def test_false_merge_is_counted_when_unrelated_predicted_as_supersede() -> None:
    """The dangerous confusion this goldset exists to catch: a classifier merges two UNRELATED
    facts into a supersede/coexist relationship."""
    predictions = [
        {"case_id": "mini-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
        {"case_id": "mini-2", "earlier_id": "a", "later_id": "b", "predicted_label": "coexist"},
        {"case_id": "mini-3", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},  # WRONG
    ]
    report = score(_MINI_GOLDSET, predictions)
    assert report.false_merge_count == 1
    assert report.confusion["unrelated"]["supersede"] == 1
    assert report.precision["supersede"] == 0.5  # 1 correct (mini-1) out of 2 predicted supersede
    assert report.recall["unrelated"] == 0.0  # the one gold unrelated pair was WRONGLY predicted (not missing)
    assert report.confusion["unrelated"]["missing"] == 0  # this pair WAS predicted -- just wrong, a different failure mode than missing
    # Coverage vs. recall distinction: every gold pair here HAS a prediction (full coverage), the
    # predictions are just partly WRONG. This is the mirror case of the missing-predictions test
    # below (which has full per-covered-pair correctness but partial coverage) -- the two metrics
    # must move independently.
    assert report.coverage == 1.0


def test_missing_predictions_are_reported_and_count_against_recall_and_coverage() -> None:
    """The bug this locks in: a gold pair with NO matching prediction is a real scoring miss, not
    a neutral non-event that gets quietly excluded. Before the fix, a scorer that predicted
    NOTHING for coexist/unrelated still reported recall=1.0 on both (an empty gold_total's
    `if gold_total else 1.0` fallback in the OLD code) — exactly backwards, since zero coverage is
    the WORST case a scorer can produce, not a vacuous pass. Now: each deliberately-missing pair
    lands in confusion[gold_label]["missing"], deflating that label's recall below 1.0, and the
    separate `coverage` metric (predicted pairs / gold pairs) drops below 1.0 too."""
    predictions = [
        {"case_id": "mini-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
    ]
    report = score(_MINI_GOLDSET, predictions)
    assert report.n_covered == 1
    assert report.n_missing == 2
    assert {"case_id": "mini-2", "earlier_id": "a", "later_id": "b"} in report.missing_pairs
    assert {"case_id": "mini-3", "earlier_id": "a", "later_id": "b"} in report.missing_pairs

    # supersede's one gold pair (mini-1) WAS covered and predicted correctly — recall stays 1.0
    # for THIS label specifically (it has no missing pair of its own).
    assert report.recall["supersede"] == 1.0
    # coexist and unrelated each have exactly ONE gold pair, and BOTH are the deliberately-missing
    # ones — recall on each must be DEFLATED below 1.0 (0.0, since their only gold pair is
    # missing), not the old vacuous 1.0.
    assert report.recall["coexist"] < 1.0
    assert report.recall["coexist"] == 0.0
    assert report.recall["unrelated"] < 1.0
    assert report.recall["unrelated"] == 0.0
    # The "missing" column carries the miss for each label's gold pair.
    assert report.confusion["coexist"]["missing"] == 1
    assert report.confusion["unrelated"]["missing"] == 1
    assert report.confusion["supersede"]["missing"] == 0

    # coverage = 1 covered / 3 gold pairs total — deflated below 1.0, independent of per-label
    # recall (a caller needs BOTH: recall says WHICH labels are wrong-or-uncovered, coverage says
    # how much of the goldset was even attempted).
    assert report.coverage < 1.0
    assert report.coverage == 1 / 3


def test_extra_predictions_beyond_the_goldset_are_reported() -> None:
    predictions = [
        {"case_id": "mini-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
        {"case_id": "mini-2", "earlier_id": "a", "later_id": "b", "predicted_label": "coexist"},
        {"case_id": "mini-3", "earlier_id": "a", "later_id": "b", "predicted_label": "unrelated"},
        {"case_id": "mini-999", "earlier_id": "x", "later_id": "y", "predicted_label": "supersede"},
    ]
    report = score(_MINI_GOLDSET, predictions)
    assert report.n_extra_predictions == 1
    assert report.extra_predictions == [{"case_id": "mini-999", "earlier_id": "x", "later_id": "y"}]
    # coverage is gold-pair-denominated (n_covered / n_gold_pairs) — an extra prediction beyond
    # the goldset must NOT inflate coverage past 1.0; all 3 real gold pairs are covered, no more.
    assert report.coverage == 1.0


def test_predictions_accepted_as_dict_keyed_by_case_id() -> None:
    predictions = {
        "mini-1": [{"earlier_id": "a", "later_id": "b", "predicted_label": "supersede"}],
        "mini-2": [{"earlier_id": "a", "later_id": "b", "predicted_label": "coexist"}],
        "mini-3": [{"earlier_id": "a", "later_id": "b", "predicted_label": "unrelated"}],
    }
    report = score(_MINI_GOLDSET, predictions)
    assert report.n_covered == 3
    assert report.false_merge_count == 0


def test_hr_fields_stay_none_when_goldset_has_no_high_risk_pairs() -> None:
    """`_MINI_GOLDSET` carries no `high_risk` field on any pair (the pre-v1 shape) — the three hr_*
    report fields must stay `None` (serializes to JSON `null`) so a caller ignorant of the v1
    extension sees the exact same report shape it always has."""
    predictions = [
        {"case_id": "mini-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
    ]
    report = score(_MINI_GOLDSET, predictions)
    assert report.hr_supersede_recall is None
    assert report.hr_false_merge_count is None
    assert report.hr_coverage is None
    d = report.to_dict()
    assert d["hr_supersede_recall"] is None
    assert d["hr_false_merge_count"] is None
    assert d["hr_coverage"] is None


_HR_GOLDSET = {
    "goldset": "panella-supersede-confusion-matrix",
    "version": "v1",
    "cases": [
        {
            "case_id": "hr-1",
            "facts": [
                {"fact_id": "a", "content": "takes Veltrazine daily", "date": "2024-01-01T00:00:00Z"},
                {"fact_id": "b", "content": "switched to Norvexol", "date": "2024-06-01T00:00:00Z"},
            ],
            "pairs": [{"earlier_id": "a", "later_id": "b", "label": "supersede", "high_risk": True}],
            "current_truth": [{"fact_id": "b", "rationale": "latest medication fact"}],
        },
        {
            "case_id": "hr-2",
            "facts": [
                {"fact_id": "a", "content": "is allergic to shellfish", "date": "2024-01-01T00:00:00Z"},
                {"fact_id": "b", "content": "switched note-taking apps", "date": "2024-02-01T00:00:00Z"},
            ],
            "pairs": [{"earlier_id": "a", "later_id": "b", "label": "unrelated", "high_risk": True}],
            "current_truth": [
                {"fact_id": "a", "rationale": "independent"},
                {"fact_id": "b", "rationale": "independent"},
            ],
        },
        {
            "case_id": "hr-3",
            "facts": [
                {"fact_id": "a", "content": "benign fact a", "date": "2024-01-01T00:00:00Z"},
                {"fact_id": "b", "content": "benign fact b", "date": "2024-02-01T00:00:00Z"},
            ],
            "pairs": [{"earlier_id": "a", "later_id": "b", "label": "coexist"}],
            "current_truth": [
                {"fact_id": "a", "rationale": "independent"},
                {"fact_id": "b", "rationale": "independent"},
            ],
        },
    ],
}


def test_hr_supersede_recall_and_false_merge_on_perfect_predictions() -> None:
    """A toy hr goldset (one hr supersede pair, one hr unrelated pair, one ordinary benign coexist
    pair) scored against PERFECT predictions: hr_supersede_recall=1.0 (the hr supersede pair was
    predicted supersede), hr_false_merge_count=0 (the hr unrelated pair was correctly predicted
    unrelated, not merged), hr_coverage=1.0 (both hr pairs got a prediction)."""
    predictions = [
        {"case_id": "hr-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
        {"case_id": "hr-2", "earlier_id": "a", "later_id": "b", "predicted_label": "unrelated"},
        {"case_id": "hr-3", "earlier_id": "a", "later_id": "b", "predicted_label": "coexist"},
    ]
    report = score(_HR_GOLDSET, predictions)
    assert report.hr_supersede_recall == 1.0
    assert report.hr_false_merge_count == 0
    assert report.hr_coverage == 1.0


def test_hr_false_merge_counted_when_hr_unrelated_predicted_supersede() -> None:
    """The dangerous hr-specific failure: the hr unrelated pair (shellfish allergy x note-taking
    app switch) gets WRONGLY predicted `supersede` — hr_false_merge_count must catch it even though
    the ordinary (non-hr) false_merge_count/confusion matrix already would; the hr-scoped field
    isolates this from the ordinary supersede pair (hr-1, correctly predicted) diluting the count."""
    predictions = [
        {"case_id": "hr-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
        {"case_id": "hr-2", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},  # WRONG, high_risk
        {"case_id": "hr-3", "earlier_id": "a", "later_id": "b", "predicted_label": "coexist"},
    ]
    report = score(_HR_GOLDSET, predictions)
    assert report.hr_supersede_recall == 1.0  # hr-1 (the only gold hr supersede pair) still correct
    assert report.hr_false_merge_count == 1
    assert report.false_merge_count == 1  # the ordinary confusion-matrix count agrees


def test_hr_supersede_recall_deflated_by_missing_prediction() -> None:
    """Missing counts as a miss for hr_supersede_recall too (same convention as the main
    `recall`/`coverage` metrics) — the hr supersede pair (hr-1) gets NO prediction at all."""
    predictions = [
        {"case_id": "hr-2", "earlier_id": "a", "later_id": "b", "predicted_label": "unrelated"},
        {"case_id": "hr-3", "earlier_id": "a", "later_id": "b", "predicted_label": "coexist"},
    ]
    report = score(_HR_GOLDSET, predictions)
    assert report.hr_supersede_recall == 0.0
    assert report.hr_coverage == 0.5  # 1 of 2 hr pairs (hr-2) got a prediction; hr-1 did not


def test_stringly_typed_high_risk_is_not_treated_as_hr() -> None:
    """The hr-flag read is `is True`, not truthiness: a malformed stringly-typed flag ("true")
    must NOT widen the hr slice — the goldset schema types the field as a real bool, and on
    malformed input the scorer treats the pair as not-hr (hr fields stay None) instead of
    silently accepting the string."""
    goldset = {
        "goldset": "panella-supersede-confusion-matrix",
        "version": "v1",
        "cases": [
            {
                "case_id": "s-1",
                "facts": [
                    {"fact_id": "a", "content": "takes Drug A", "date": "2024-01-01T00:00:00Z"},
                    {"fact_id": "b", "content": "takes Drug B", "date": "2024-02-01T00:00:00Z"},
                ],
                "pairs": [{"earlier_id": "a", "later_id": "b", "label": "supersede", "high_risk": "true"}],
                "current_truth": [{"fact_id": "b", "rationale": "latest"}],
            }
        ],
    }
    predictions = [{"case_id": "s-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"}]
    report = score(goldset, predictions)
    assert report.hr_supersede_recall is None
    assert report.hr_false_merge_count is None
    assert report.hr_coverage is None
