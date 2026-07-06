"""Unit tests for eval/render_report.py — asserts the template's placeholders are all filled and
that report rendering writes ONLY under the given out_path (never printing numbers elsewhere)."""
from __future__ import annotations

import re

from eval.render_report import DEFAULT_TEMPLATE, render


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
