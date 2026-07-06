"""Unit tests for eval/longmemeval/qa.py's fail-closed completeness contract: ANY reader/judge
transport error must mark the written envelope "complete": false with an "errors" count, and
main() must exit nonzero — not just when EVERY row errors (the old, narrower behavior).

--out must resolve under eval/out/ (assert_eval_out, exercised directly in test_paths.py) — these
tests write into a uniquely-named scratch subdirectory of the REAL eval/out/ (gitignored, cleaned
up via the `scratch_out` fixture) rather than monkeypatching the guard away, so the guard itself
stays exercised end-to-end, not bypassed."""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

import eval.longmemeval.qa as qa_module
from eval._paths import EVAL_OUT_DIR
from eval.longmemeval.qa import main


@pytest.fixture
def scratch_out():
    """A unique, real subdirectory under eval/out/ — passes assert_eval_out unmodified, cleaned up
    after the test regardless of outcome."""
    scratch = EVAL_OUT_DIR / f"test-qa-fail-closed-{uuid.uuid4().hex[:8]}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture(autouse=True)
def _dummy_openai_key(monkeypatch):
    """qa.py's main() calls _openai_key() (which would sys.exit without ANY key set) whenever
    --reader-transport/--judge-transport is "openai" — even though every test here fakes `chat`
    directly and never actually sends the key anywhere. Set a harmless placeholder so main() gets
    past that gate; the fake `chat` never reads this value."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-key-never-sent")


def _retrieval_row(qid: str, qtype: str = "single-session") -> dict:
    return {
        "qid": qid,
        "type": qtype,
        "question": f"question for {qid}?",
        "gold": "the answer",
        "question_date": None,
        "reader_context": "some retrieved context",
        "lane": "facade",
        "recall@5": 1.0,
    }


def _write_retrieval(scratch: Path, rows: list[dict]) -> Path:
    path = scratch / "retr.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_all_rows_succeed_writes_complete_envelope_and_exits_zero(scratch_out, monkeypatch):
    rows = [_retrieval_row("q1"), _retrieval_row("q2")]
    retr_path = _write_retrieval(scratch_out, rows)
    out_path = scratch_out / "qa.json"

    def fake_chat(key, model, system, user, max_tokens=256, transport="openai"):
        return "the correct answer, yes"

    monkeypatch.setattr(qa_module, "chat", fake_chat)

    exit_code = main(
        ["--retr", str(retr_path), "--out", str(out_path), "--reader-transport", "openai", "--judge-transport", "openai"]
    )

    assert exit_code == 0
    envelope = json.loads(out_path.read_text(encoding="utf-8"))
    assert envelope["complete"] is True
    assert envelope["errors"] == 0
    assert len(envelope["rows"]) == 2


def test_single_transport_error_marks_incomplete_and_exits_nonzero(scratch_out, monkeypatch):
    """The exact gap the review found: the OLD code only failed when ALL rows errored. A single
    flaky transport call among many successful ones used to exit 0 with a silently-deflated-n
    accuracy number. Now: even ONE errored row marks the envelope incomplete and exits nonzero."""
    rows = [_retrieval_row("q1"), _retrieval_row("q2"), _retrieval_row("q3")]
    retr_path = _write_retrieval(scratch_out, rows)
    out_path = scratch_out / "qa.json"

    call_count = {"n": 0}

    def flaky_chat(key, model, system, user, max_tokens=256, transport="openai"):
        call_count["n"] += 1
        # Fail exactly one call, succeed everything else — proves a SINGLE error among N
        # successes still trips fail-closed. --workers 1 makes call order deterministic.
        if call_count["n"] == 2:
            return "__ERR__500"
        return "the correct answer, yes"

    monkeypatch.setattr(qa_module, "chat", flaky_chat)

    exit_code = main(
        [
            "--retr", str(retr_path), "--out", str(out_path),
            "--reader-transport", "openai", "--judge-transport", "openai", "--workers", "1",
        ]
    )

    assert exit_code != 0
    assert exit_code != 2  # NOT the "all rows errored" fatal path — this is the NEW partial-incompleteness path
    envelope = json.loads(out_path.read_text(encoding="utf-8"))
    assert envelope["complete"] is False
    assert envelope["errors"] == 1
    assert len(envelope["rows"]) == 3  # all rows still written, including the errored one


def test_all_rows_error_is_the_severe_fatal_path_exit_2(scratch_out, monkeypatch):
    """The pre-existing "0 scored" fatal case must still exit specifically 2 (distinct from the
    NEW partial-incompleteness exit code) — a regression guard for the fatal path."""
    rows = [_retrieval_row("q1")]
    retr_path = _write_retrieval(scratch_out, rows)
    out_path = scratch_out / "qa.json"

    def always_erroring_chat(key, model, system, user, max_tokens=256, transport="openai"):
        return "__ERR__500"

    monkeypatch.setattr(qa_module, "chat", always_erroring_chat)

    exit_code = main(
        ["--retr", str(retr_path), "--out", str(out_path), "--reader-transport", "openai", "--judge-transport", "openai"]
    )

    assert exit_code == 2
    envelope = json.loads(out_path.read_text(encoding="utf-8"))
    assert envelope["complete"] is False
    assert envelope["errors"] == 1
