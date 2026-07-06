"""Unit tests for eval/render_report.py — asserts the template's placeholders are all filled and
that report rendering writes ONLY under the given out_path (never printing numbers elsewhere)."""
from __future__ import annotations

from pathlib import Path

import re

from eval.render_report import DEFAULT_TEMPLATE, _qa_rows_from_envelope, render


def test_render_with_no_inputs_fills_every_placeholder(tmp_path) -> None:
    """Even with zero inputs, every {{PLACEHOLDER}} token must be replaced (with an explicit
    'not run' marker) — a report that still contains a literal {{...}} token is a rendering bug,
    not a valid 'nothing to report' state."""
    out_path = tmp_path / "report.md"
    rendered = render(template_path=DEFAULT_TEMPLATE, out_path=out_path)
    assert out_path.exists()
    leftover = re.findall(r"\{\{[A-Z_]+\}\}", rendered)
    assert leftover == [], f"unfilled placeholders: {leftover}"


def test_render_fills_lane_comparison_numbers(tmp_path) -> None:
    out_path = tmp_path / "report.md"
    lane_comparison = {
        "per_type": [
            {
                "type": "OVERALL",
                "store_n": 10,
                "facade_n": 10,
                "store_recall@1": 0.8,
                "facade_recall@1": 0.7,
                "delta_recall@1": -0.1,
                "store_recall@5": 0.9,
                "facade_recall@5": 0.9,
                "delta_recall@5": 0.0,
                "store_recall@10": 1.0,
                "facade_recall@10": 1.0,
                "delta_recall@10": 0.0,
            }
        ],
        "intentional_lane_deltas": [{"delta": "d1", "shipped_default": "x", "effect": "y"}],
        "framing": "not a leaderboard entry",
    }
    rendered = render(template_path=DEFAULT_TEMPLATE, out_path=out_path, lane_comparison=lane_comparison)
    assert "0.800" in rendered  # OVERALL_STORE_R1
    assert "-0.100" in rendered  # OVERALL_DELTA_R1
    assert "d1" in rendered and "y" in rendered


def test_render_writes_only_to_out_path(tmp_path, capsys) -> None:
    out_path = tmp_path / "nested" / "report.md"
    render(template_path=DEFAULT_TEMPLATE, out_path=out_path)
    assert out_path.exists()
    # Nothing else should have been created alongside it in tmp_path (no stray artifact).
    assert list(tmp_path.iterdir()) == [tmp_path / "nested"]


_QA_ROW = {"qid": "q1", "type": "single-session", "lane": "facade", "correct": True, "errored": False}


def test_incomplete_qa_envelope_refuses_to_report_accuracy(tmp_path) -> None:
    """qa.py's fail-closed envelope marks "complete": false whenever any reader/judge row
    transport-errored. render_report.py MUST refuse to report a QA-accuracy number from it —
    printing an incomplete-run notice instead of a (silently-deflated-n) accuracy figure."""
    out_path = tmp_path / "report.md"
    qa_data = {"complete": False, "errors": 1, "rows": [_QA_ROW]}
    rendered = render(template_path=DEFAULT_TEMPLATE, out_path=out_path, qa_data=qa_data)
    assert "QA incomplete" in rendered


def test_incomplete_qa_envelope_does_not_render_a_per_type_table(tmp_path) -> None:
    """A more precise check than substring-presence: the per-type QA rows placeholder must carry
    the incomplete notice, not a rendered `| single-session | N | acc |` row."""
    out_path = tmp_path / "report.md"
    qa_data = {"complete": False, "errors": 1, "rows": [_QA_ROW]}
    rendered = render(template_path=DEFAULT_TEMPLATE, out_path=out_path, qa_data=qa_data)
    assert "| single-session |" not in rendered


def test_complete_qa_envelope_renders_accuracy_normally(tmp_path) -> None:
    """The counterpart: a "complete": true envelope (or the old bare-list shape, for back-compat)
    renders the QA-accuracy table exactly as before — the incomplete-refusal path must not
    swallow legitimate complete runs."""
    out_path = tmp_path / "report.md"
    qa_data = {"complete": True, "errors": 0, "rows": [_QA_ROW]}
    rendered = render(template_path=DEFAULT_TEMPLATE, out_path=out_path, qa_data=qa_data)
    assert "QA incomplete" not in rendered
    assert "| single-session | 1 | 1.000 |" in rendered


def test_legacy_bare_list_qa_shape_is_treated_as_complete(tmp_path) -> None:
    """Back-compat: a qa_data that is still the OLD bare-list shape (no envelope wrapper) is
    treated as complete — only the NEW envelope shape can ever declare itself incomplete."""
    out_path = tmp_path / "report.md"
    rendered = render(template_path=DEFAULT_TEMPLATE, out_path=out_path, qa_data=[_QA_ROW])
    assert "QA incomplete" not in rendered
    assert "| single-session | 1 | 1.000 |" in rendered


def test_qa_rows_from_envelope_unit() -> None:
    """Direct unit coverage of the small envelope-unwrapping helper."""
    assert _qa_rows_from_envelope(None) == (None, True)
    assert _qa_rows_from_envelope([_QA_ROW]) == ([_QA_ROW], True)
    rows, complete = _qa_rows_from_envelope({"complete": False, "errors": 2, "rows": [_QA_ROW]})
    assert rows == [_QA_ROW]
    assert complete is False


def test_per_type_rows_are_not_double_piped():
    """Emitted per-type rows carry their own leading/trailing pipes; the template placeholder must
    be bare, or every rendered row gains empty edge cells (`| | ... | |`) and the markdown table
    misaligns (GH-bot P3)."""
    template = Path("eval/REPORT.template.md").read_text()
    assert "| {{PER_TYPE_ROWS}} |" not in template
    assert "| {{QA_PER_TYPE_ROWS}} |" not in template
    assert "{{PER_TYPE_ROWS}}" in template
    assert "{{QA_PER_TYPE_ROWS}}" in template
