"""Hermetic admission tests for the diag candidate-artifact gate (c1-e2.1)."""

from __future__ import annotations

import json

import pytest

from eval.goldsets import resolver_blocking_diag as diag
from eval.goldsets.key_correctness_eval import load_items


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
