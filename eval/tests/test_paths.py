"""Unit tests for eval/_paths.py::assert_eval_out — the shared guard every metric-writing tool
(ingest_retrieve, qa, compare_lanes, render_report, score_supersede, key_correctness_eval) routes
its --out path through. A path that resolves outside eval/out/ must hard-fail (exit 2), never
silently write, and never warn-and-continue."""
from __future__ import annotations

import pytest

from eval._paths import EVAL_OUT_DIR, assert_eval_out


def test_path_under_eval_out_is_accepted() -> None:
    resolved = assert_eval_out("eval/out/stage_a_retrieval.json")
    assert resolved == (EVAL_OUT_DIR / "stage_a_retrieval.json")


def test_nested_path_under_eval_out_is_accepted() -> None:
    resolved = assert_eval_out("eval/out/nested/dir/report.json")
    assert resolved.is_relative_to(EVAL_OUT_DIR)


def test_bare_filename_resolves_relative_to_cwd_and_is_refused() -> None:
    """A bare filename (no eval/out/ prefix) resolves relative to CWD, which is virtually never
    eval/out/ itself — this is exactly the ingest_retrieve.py/qa.py bug this guard exists to catch
    (their OLD defaults were bare filenames that landed wherever the operator's shell CWD was)."""
    with pytest.raises(SystemExit, match=r"REFUSING to write"):
        assert_eval_out("stage_a_retrieval.json")


def test_absolute_path_outside_eval_out_is_refused() -> None:
    with pytest.raises(SystemExit, match=r"REFUSING to write"):
        assert_eval_out("/tmp/stage_a_retrieval.json")


def test_dot_dot_escape_is_refused() -> None:
    """A relative path that `..`s its way back out of eval/out/ must be caught by the RESOLVED
    (absolute, symlink/`..`-collapsed) comparison, not a naive string-prefix check."""
    with pytest.raises(SystemExit, match=r"REFUSING to write"):
        assert_eval_out("eval/out/../../escaped.json")


def test_sibling_dir_that_shares_a_string_prefix_is_refused() -> None:
    """`eval/out_evil/` shares the STRING prefix `eval/out` with `eval/out/` but is NOT a
    subdirectory of it — a naive `str.startswith()` guard would wrongly accept this; the real
    guard must use `Path.relative_to`, which correctly rejects it."""
    with pytest.raises(SystemExit, match=r"REFUSING to write"):
        assert_eval_out("eval/out_evil/report.json")


def test_refusal_message_names_the_offending_path() -> None:
    with pytest.raises(SystemExit) as exc_info:
        assert_eval_out("/tmp/leaked.json")
    assert "leaked.json" in str(exc_info.value)
