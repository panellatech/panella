#!/usr/bin/env python3
"""The supersede confusion-matrix scorer CONTRACT the construction-rung work will consume.

Pure function: (goldset, predictions) -> per-label precision/recall + confusion matrix +
false-merge count + coverage. No I/O beyond the two inputs it is handed, no network, no store
access — callers own loading the goldset JSON and producing predictions in the documented shape.

Prediction shape (a list of dicts, or a dict keyed by case_id — both accepted, see
`_normalize_predictions`):
    {
      "case_id": "sc-supersede-0000-employer",
      "earlier_id": "f-earlier",
      "later_id": "f-later",
      "predicted_label": "supersede"   # one of supersede|coexist|unrelated
    }

A prediction is matched to its gold pair by (case_id, earlier_id, later_id). A gold pair the
predictions do NOT cover is a real scoring miss, not a neutral non-event: it is counted as a false
negative for its gold label (confusion[gold_label]["missing"], a dedicated fourth column alongside
the three real predicted labels) — a classifier that predicts NOTHING for a slot must NOT score
recall=1.0 on that label merely because it never guessed wrong. `coverage` (n_covered /
n_gold_pairs) is reported separately from recall, since coverage and per-label correctness are
different failure modes a caller needs to distinguish (a scorer with high coverage but wrong
guesses, vs. one with low coverage but correct guesses where it did guess, look identical under
recall alone). The per-pair `missing_pairs` list (unchanged from before) still reports the raw
(case_id, earlier_id, later_id) tuples for anyone auditing individual gaps.

The "false-merge count" is the count of predicted `supersede` or `coexist` labels where gold says
`unrelated` — the dangerous confusion this whole goldset exists to catch (see SCHEMA.md's
`unrelated`-label rationale): a classifier that merges two unrelated facts into the same tracked
slot corrupts the current-truth set with a fabricated relationship.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eval._paths import assert_eval_out

LABELS = ("supersede", "coexist", "unrelated")
# The confusion matrix's predicted-side columns: the three real labels PLUS "missing" (a gold pair
# with no matching prediction at all — see module docstring). "missing" is never a value
# `predicted_label` can legally hold (see `score`'s validation), so it cannot collide with a real
# prediction; it exists ONLY to give a coverage gap a slot in the SAME table real errors live in.
_PREDICTED_COLUMNS = (*LABELS, "missing")


@dataclass
class ConfusionMatrixReport:
    # confusion[gold_label][predicted_label_or_"missing"] = count
    confusion: dict[str, dict[str, int]] = field(
        default_factory=lambda: {g: {p: 0 for p in _PREDICTED_COLUMNS} for g in LABELS}
    )
    precision: dict[str, float] = field(default_factory=dict)
    recall: dict[str, float] = field(default_factory=dict)
    coverage: float = 0.0
    false_merge_count: int = 0
    n_covered: int = 0
    n_gold_pairs: int = 0
    n_missing: int = 0
    missing_pairs: list[dict[str, str]] = field(default_factory=list)
    n_extra_predictions: int = 0
    extra_predictions: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "confusion": self.confusion,
            "precision": {k: round(v, 4) for k, v in self.precision.items()},
            "recall": {k: round(v, 4) for k, v in self.recall.items()},
            "coverage": round(self.coverage, 4),
            "false_merge_count": self.false_merge_count,
            "n_covered": self.n_covered,
            "n_gold_pairs": self.n_gold_pairs,
            "n_missing": self.n_missing,
            "missing_pairs": self.missing_pairs,
            "n_extra_predictions": self.n_extra_predictions,
            "extra_predictions": self.extra_predictions,
        }


def _gold_pairs(goldset: dict[str, Any]) -> dict[tuple[str, str, str], str]:
    """Flatten the goldset into {(case_id, earlier_id, later_id): gold_label}."""
    out: dict[tuple[str, str, str], str] = {}
    for case in goldset.get("cases", []):
        case_id = case["case_id"]
        for pair in case.get("pairs", []):
            key = (case_id, pair["earlier_id"], pair["later_id"])
            out[key] = pair["label"]
    return out


def _normalize_predictions(predictions: Any) -> dict[tuple[str, str, str], str]:
    """Accept either a flat list of prediction dicts or a {case_id: [predictions]} mapping.
    Returns {(case_id, earlier_id, later_id): predicted_label}."""
    out: dict[tuple[str, str, str], str] = {}
    if isinstance(predictions, dict):
        items = []
        for case_id, preds in predictions.items():
            for p in preds:
                items.append({**p, "case_id": p.get("case_id", case_id)})
    elif isinstance(predictions, list):
        items = predictions
    else:
        raise TypeError(f"predictions must be a list or dict, got {type(predictions).__name__}")
    for p in items:
        key = (p["case_id"], p["earlier_id"], p["later_id"])
        out[key] = p["predicted_label"]
    return out


def score(goldset: dict[str, Any], predictions: Any) -> ConfusionMatrixReport:
    """Score `predictions` against `goldset`. Pure function — see module docstring for shapes."""
    gold = _gold_pairs(goldset)
    pred = _normalize_predictions(predictions)

    report = ConfusionMatrixReport()
    report.n_gold_pairs = len(gold)

    covered_keys = set(gold) & set(pred)
    missing_keys = set(gold) - set(pred)
    extra_keys = set(pred) - set(gold)

    for key in sorted(covered_keys):
        case_id, earlier_id, later_id = key
        g_label = gold[key]
        p_label = pred[key]
        if p_label not in LABELS:
            raise ValueError(f"prediction for {key} has invalid predicted_label={p_label!r}")
        report.confusion[g_label][p_label] += 1
        if g_label == "unrelated" and p_label in ("supersede", "coexist"):
            report.false_merge_count += 1

    # A gold pair with NO matching prediction is a real scoring miss, not a neutral non-event —
    # count it as a false negative for its gold label in the SAME confusion table real errors
    # live in (the "missing" column). Without this, a scorer that predicts nothing for an entire
    # label would score recall=1.0 on it (an empty gold_total in the OLD code's `if gold_total else
    # 1.0` fallback), which is exactly backwards: zero coverage is the WORST case, not a vacuous
    # pass.
    for key in sorted(missing_keys):
        g_label = gold[key]
        report.confusion[g_label]["missing"] += 1

    report.n_covered = len(covered_keys)
    report.n_missing = len(missing_keys)
    report.missing_pairs = [
        {"case_id": k[0], "earlier_id": k[1], "later_id": k[2]} for k in sorted(missing_keys)
    ]
    report.n_extra_predictions = len(extra_keys)
    report.extra_predictions = [
        {"case_id": k[0], "earlier_id": k[1], "later_id": k[2]} for k in sorted(extra_keys)
    ]
    # Coverage: of every gold pair, how many had ANY prediction at all (right or wrong) — a
    # DIFFERENT failure mode than recall (a scorer can have perfect coverage with wrong guesses,
    # or partial coverage with correct guesses where it did guess; recall alone conflates the two).
    report.coverage = (report.n_covered / report.n_gold_pairs) if report.n_gold_pairs else 1.0

    for label in LABELS:
        # Precision: of every pair PREDICTED this label, how many were actually this label. Sums
        # ONLY over the real LABELS columns (excludes "missing" — a pair with no prediction was
        # never predicted as `label`, so it must never inflate or deflate any label's precision
        # denominator).
        predicted_total = sum(report.confusion[g][label] for g in LABELS)
        true_positive = report.confusion[label][label]
        report.precision[label] = (true_positive / predicted_total) if predicted_total else 1.0
        # Recall: of every pair GOLD-labeled this label, how many were predicted correctly. The
        # row sum now naturally includes the "missing" column (populated above), so a label with
        # gold pairs the predictions never covered correctly reports a DEFLATED recall instead of
        # the old vacuous 1.0 — a missing gold pair is scored as a miss, not excluded.
        gold_total = sum(report.confusion[label].values())
        report.recall[label] = (true_positive / gold_total) if gold_total else 1.0

    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--goldset", type=Path, required=True)
    ap.add_argument("--predictions", type=Path, required=True, help="JSON file: list of prediction dicts or {case_id: [predictions]}")
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here (REQUIRED — must be under eval/out/; same pattern as key_correctness_eval.py)")
    args = ap.parse_args(argv)

    if not args.out:
        # HARD CONSTRAINT compliance: printing the report to stdout would put precision/recall
        # metric values on stdout. Require --out for a real run (same pattern as
        # key_correctness_eval.py); only tests call score() directly and handle the report object
        # themselves.
        print(
            "no --out given: report NOT printed (numeric output must land under eval/out/ only); "
            "pass --out eval/out/<name>.json",
            file=sys.stderr,
        )
        return 2
    out_path = assert_eval_out(args.out)

    goldset = json.loads(args.goldset.read_text(encoding="utf-8"))
    predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
    report = score(goldset, predictions)
    text = json.dumps(report.to_dict(), indent=2, sort_keys=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {out_path} (report inside; not printed to stdout)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
