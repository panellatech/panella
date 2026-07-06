"""Unit tests for eval/longmemeval/compare_lanes.py."""
from __future__ import annotations

import json
import os

from eval.longmemeval.compare_lanes import STORE_LANE_DESCRIPTION, _derive_intentional_lane_deltas, compare
from panella.governance import load_governance


def _write_dump(path, rows) -> None:
    path.write_text(json.dumps(rows), encoding="utf-8")


def _write_lane_dump(path, lane, rows) -> None:
    """Real dumps always carry qid + lane (ingest_retrieve writes both); the comparability guard
    requires them, so fixtures model the real shape."""
    stamped = [{**r, "lane": lane, "qid": r.get("qid", f"q{i}")} for i, r in enumerate(rows)]
    path.write_text(json.dumps(stamped), encoding="utf-8")


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
    _write_lane_dump(store_path, "store", store_rows)
    _write_lane_dump(facade_path, "facade", facade_rows)

    result = compare(store_path, facade_path, governance=load_governance())
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
    _write_lane_dump(store_path, "store", rows)
    _write_lane_dump(facade_path, "facade", rows)
    result = compare(store_path, facade_path, governance=load_governance())
    types = {r["type"] for r in result["per_type"]}
    assert "OVERALL" in types


def test_intentional_lane_deltas_always_present(tmp_path) -> None:
    """The honest-framing table must be emitted on EVERY comparison — never omitted, never a
    cherry-picked subset."""
    rows = [{"type": "t1", "recall@1": 1.0, "recall@5": 1.0, "recall@10": 1.0}]
    store_path = tmp_path / "store.json"
    facade_path = tmp_path / "facade.json"
    _write_lane_dump(store_path, "store", rows)
    _write_lane_dump(facade_path, "facade", rows)
    gov = load_governance()
    result = compare(store_path, facade_path, governance=gov)
    assert result["intentional_lane_deltas"] == _derive_intentional_lane_deltas(gov)
    assert len(result["intentional_lane_deltas"]) >= 5
    assert "leaderboard" in result["framing"].lower()


def test_store_lane_description_is_honest_and_present(tmp_path) -> None:
    """The finding's exact fix: the store lane must be described as "raw store search (no
    governance semantics)" — not silently implied to share the facade's profile/ABAC/lifecycle
    machinery."""
    rows = [{"type": "t1", "recall@1": 1.0, "recall@5": 1.0, "recall@10": 1.0}]
    store_path = tmp_path / "store.json"
    facade_path = tmp_path / "facade.json"
    _write_lane_dump(store_path, "store", rows)
    _write_lane_dump(facade_path, "facade", rows)
    result = compare(store_path, facade_path, governance=load_governance())
    assert result["store_lane"] == STORE_LANE_DESCRIPTION
    assert result["store_lane"] == "raw store search (no governance semantics)"


def test_intentional_lane_deltas_reflect_the_real_rendered_serving_profile() -> None:
    """No hardcoded config values in the emitted table — every delta's `shipped_default` must
    match what `render_serving_profile` ACTUALLY renders for the given governance, proving the
    table is derived, not copy-pasted."""
    import yaml

    from panella.config_render import render_serving_profile

    gov = load_governance()
    profile = yaml.safe_load(render_serving_profile(gov))
    deltas = _derive_intentional_lane_deltas(gov)

    top_k_row = next(d for d in deltas if d["delta"] == "profile top-k cap")
    assert str(profile["max_query_k"]) in top_k_row["shipped_default"]

    allowlist_row = next(d for d in deltas if d["delta"] == "tenant/read allowlist (ABAC)")
    assert repr(profile["read_allowlist"]) in allowlist_row["shipped_default"]

    boost_row = next(d for d in deltas if d["delta"] == "wing soft-boost")
    assert str(profile["wing_boost"]["default"]) in boost_row["shipped_default"]


def test_intentional_lane_deltas_reflect_the_real_adapter_constants() -> None:
    """The overfetch/lifecycle-status deltas must match the REAL panella_adapter.py module
    constants at the time of the call — proving they are imported live, not hand-copied strings
    that could silently drift from a future change to those constants."""
    from panella import panella_adapter

    deltas = _derive_intentional_lane_deltas(load_governance())

    overfetch_row = next(d for d in deltas if d["delta"] == "overfetch + backfill")
    assert str(panella_adapter.PANELLA_OVERFETCH_N) in overfetch_row["shipped_default"]

    lifecycle_row = next(d for d in deltas if d["delta"] == "lifecycle validity filtering")
    assert repr(sorted(panella_adapter.EXCLUDED_RECALL_STATUSES)) in lifecycle_row["shipped_default"]


def test_intentional_lane_deltas_reflect_the_real_reader_env(monkeypatch) -> None:
    """The reader++/reranker delta must reflect whatever THIS run's actual environment has set —
    proving it is read live from os.environ, not a hardcoded "off" string."""
    from panella.reader import ENV_READER, ENV_RERANKER

    monkeypatch.setenv(ENV_READER, "readerpp")
    monkeypatch.setenv(ENV_RERANKER, "cross-encoder")
    deltas = _derive_intentional_lane_deltas(load_governance())
    reader_row = next(d for d in deltas if d["delta"] == "reader++ / cross-encoder reranking")
    assert "readerpp" in reader_row["shipped_default"]
    assert "cross-encoder" in reader_row["shipped_default"]


def test_lane_deltas_ignore_host_shell_governance_overlay(monkeypatch):
    """A lingering host-shell PANELLA_GOVERNANCE_OVERLAY export must not leak into the "this run"
    delta table: the eval facade runs with that env CLEARED (eval/compose.eval.yml), so the
    derivation clears it too (review r2 P1). A bogus overlay path makes the failure mode loud —
    without the clearing, current_governance() would try (and fail) to load the operator's box
    config instead of the generic governance the eval box actually runs."""
    monkeypatch.setenv("PANELLA_GOVERNANCE_OVERLAY", "/nonexistent/host-box-governance.yaml")
    monkeypatch.setenv("PANELLA_CONFIG_DIR", "/nonexistent/host-config-dir")
    monkeypatch.setenv("PANELLA_READER", "readerpp")  # host export ≠ this run (round-4 P2)
    monkeypatch.setenv("PANELLA_RERANKER", "cross-encoder")

    rows = _derive_intentional_lane_deltas()

    by_name = {row["delta"]: row for row in rows}
    assert "read_allowlist: ['owner/*']" in by_name["tenant/read allowlist (ABAC)"]["shipped_default"]
    reader_row = by_name["reader++ / cross-encoder reranking"]["shipped_default"]
    assert "readerpp" not in reader_row and "cross-encoder" not in reader_row.split("(")[0]
    assert "off" in reader_row
    # the derivation restored the operator's env afterwards
    assert os.environ["PANELLA_GOVERNANCE_OVERLAY"] == "/nonexistent/host-box-governance.yaml"
    assert os.environ["PANELLA_CONFIG_DIR"] == "/nonexistent/host-config-dir"


def _rows(lane, qids):
    return [
        {"qid": q, "type": "single-session-user", "lane": lane,
         "recall@1": 1.0, "recall@5": 1.0, "recall@10": 1.0}
        for q in qids
    ]


def test_compare_refuses_mismatched_question_sets(tmp_path, capsys):
    """Dumps covering different question sets (stale file / different --n-per-type rerun) must be
    refused, not silently averaged into a meaningless delta (GH-bot P2)."""
    import json

    import pytest

    from eval.longmemeval.compare_lanes import compare

    store = tmp_path / "store.json"
    facade = tmp_path / "facade.json"
    store.write_text(json.dumps(_rows("store", ["q1", "q2"])))
    facade.write_text(json.dumps(_rows("facade", ["q1", "q3"])))
    with pytest.raises(SystemExit) as exc_info:
        compare(store, facade)
    assert exc_info.value.code == 2
    assert "different question sets" in capsys.readouterr().err


def test_compare_refuses_swapped_lane_files(tmp_path, capsys):
    import json

    import pytest

    from eval.longmemeval.compare_lanes import compare

    store = tmp_path / "store.json"
    facade = tmp_path / "facade.json"
    store.write_text(json.dumps(_rows("facade", ["q1"])))  # swapped
    facade.write_text(json.dumps(_rows("facade", ["q1"])))
    with pytest.raises(SystemExit) as exc_info:
        compare(store, facade)
    assert exc_info.value.code == 2
    assert "swapped or stale" in capsys.readouterr().err
