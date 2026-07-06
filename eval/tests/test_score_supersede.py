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
    assert report.recall["unrelated"] == 0.0  # the one gold unrelated pair was missed


def test_missing_predictions_are_reported_not_scored_as_wrong() -> None:
    predictions = [
        {"case_id": "mini-1", "earlier_id": "a", "later_id": "b", "predicted_label": "supersede"},
    ]
    report = score(_MINI_GOLDSET, predictions)
    assert report.n_covered == 1
    assert report.n_missing == 2
    assert {"case_id": "mini-2", "earlier_id": "a", "later_id": "b"} in report.missing_pairs
    assert {"case_id": "mini-3", "earlier_id": "a", "later_id": "b"} in report.missing_pairs
    # A covered-only recall/precision (not deflated by missing coverage).
    assert report.recall["supersede"] == 1.0


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


def test_predictions_accepted_as_dict_keyed_by_case_id() -> None:
    predictions = {
        "mini-1": [{"earlier_id": "a", "later_id": "b", "predicted_label": "supersede"}],
        "mini-2": [{"earlier_id": "a", "later_id": "b", "predicted_label": "coexist"}],
        "mini-3": [{"earlier_id": "a", "later_id": "b", "predicted_label": "unrelated"}],
    }
    report = score(_MINI_GOLDSET, predictions)
    assert report.n_covered == 3
    assert report.false_merge_count == 0
