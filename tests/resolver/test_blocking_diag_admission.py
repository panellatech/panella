"""Hermetic admission tests for the diag candidate-artifact gate (c1-e2.1)."""

from __future__ import annotations

import json

import pytest

from eval.goldsets import resolver_blocking_diag as diag


def _fake_artifact(tmp_path, **overrides):
    artifact = {
        "candidates": {"item-1": [], "item-2": [{"kind": "fact"}]},
        "n_items": 2,
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
    assert set(diag._load_candidates(path)) == {"item-1", "item-2"}


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
