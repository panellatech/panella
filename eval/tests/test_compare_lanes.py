"""Unit tests for eval/longmemeval/compare_lanes.py."""
from __future__ import annotations

import json

from eval.longmemeval.compare_lanes import INTENTIONAL_LANE_DELTAS, compare


def _write_dump(path, rows) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


def test_compare_computes_per_type_delta(tmp_path) -> None:
    store_rows = [
        {"type": "single-session", "recall@1": 1.0, "recall@5": 1.0, "recall@10": 1.0},
        {"type": "single-session", "recall@1": 0.5, "recall@5": 1.0, "recall@10": 1.0},
    ]
    facade_rows = [
        {"type": "single-session", "recall@1": 0.5, "recall@5": 1.0, "recall@10": 1.0},
        {"type": "single-session", "recall@1": 0.5, "recall@5": 0.5, "recall@10": 1.0},
    ]
    store_path = tmp_path / "store.json"
    facade_path = tmp_path / "facade.json"
    _write_dump(store_path, store_rows)
    _write_dump(facade_path, facade_rows)

    result = compare(store_path, facade_path)
    row = next(r for r in result["per_type"] if r["type"] == "single-session")
    assert row["store_n"] == 2
    assert row["facade_n"] == 2
    assert row["store_recall@1"] == 0.75
    assert row["facade_recall@1"] == 0.5
    assert row["delta_recall@1"] == 0.5 - 0.75


def test_compare_includes_overall_row(tmp_path) -> None:
    rows = [{"type": "t1", "recall@1": 1.0, "recall@5": 1.0, "recall@10": 1.0}]
    store_path = tmp_path / "store.json"
    facade_path = tmp_path / "facade.json"
    _write_dump(store_path, rows)
    _write_dump(facade_path, rows)
    result = compare(store_path, facade_path)
    types = {r["type"] for r in result["per_type"]}
    assert "OVERALL" in types


def test_intentional_lane_deltas_always_present(tmp_path) -> None:
    """The honest-framing table must be emitted on EVERY comparison — never omitted, never a
    cherry-picked subset."""
    rows = [{"type": "t1", "recall@1": 1.0, "recall@5": 1.0, "recall@10": 1.0}]
    store_path = tmp_path / "store.json"
    facade_path = tmp_path / "facade.json"
    _write_dump(store_path, rows)
    _write_dump(facade_path, rows)
    result = compare(store_path, facade_path)
    assert result["intentional_lane_deltas"] == INTENTIONAL_LANE_DELTAS
    assert len(result["intentional_lane_deltas"]) >= 5
    assert "leaderboard" in result["framing"].lower()
