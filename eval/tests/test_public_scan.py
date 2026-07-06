"""Unit tests for eval/public_scan.py::scan_line — the pure per-line rule engine `make
eval-public-scan` runs over every tracked file. Covers the two pattern classes the brief calls out
as gaps in the original scanner: quoted-JSON metric keys and markdown-table metric cells.

Every example string in this file is built at RUNTIME (never a literal in the source), for two
reasons: (1) it stops `eval-public-scan` itself from flagging this test file when it scans the
tracked tree, and (2) it exercises `scan_line` exactly the way a real leaked value would arrive —
as a string the function receives, not as a regex literal it was written against."""
from __future__ import annotations

from eval.public_scan import scan_line

# Built at runtime so this file never contains a literal quoted-JSON metric key or table cell —
# see module docstring.
_RECALL_KEY = "recall" + "@1"
_QA_ACC_KEY = "QA-" + "accuracy"
_KEY_CORRECTNESS_KEY = "key_" + "correctness"


def test_unquoted_metric_pattern_still_matches() -> None:
    """Baseline: the pre-existing unquoted form must still be caught (regression guard for the
    refactor that split _METRIC_PATTERNS into _UNQUOTED/_QUOTED groups)."""
    line = f"{_RECALL_KEY}: 0.9\n"
    reasons = scan_line("some/file.py", line)
    assert any("matched" in r for r in reasons)


def test_quoted_json_recall_key_is_caught() -> None:
    """The gap the review called out: a metric dict pasted verbatim (`"recall@1": 0.9`) was
    invisible to the original unquoted-only regex — this is the new quoted-JSON pattern group."""
    line = '{"' + _RECALL_KEY + '": 0.923}\n'
    reasons = scan_line("README.md", line)
    assert any("matched" in r for r in reasons)


def test_quoted_json_qa_accuracy_key_is_caught() -> None:
    line = '"' + _QA_ACC_KEY + '": 0.65\n'
    reasons = scan_line("some/doc.md", line)
    assert any("matched" in r for r in reasons)


def test_quoted_json_key_correctness_key_is_caught() -> None:
    line = '"' + _KEY_CORRECTNESS_KEY + '": 0.038\n'
    reasons = scan_line("some/doc.md", line)
    assert any("matched" in r for r in reasons)


def test_quoted_json_pattern_is_exempted_for_eval_tests_files() -> None:
    """The narrow, deliberate carve-out: eval/tests/*.py fixture data that hand-builds a dict
    literal keyed by a metric name (synthetic scorer-test input, never a published result) must
    NOT trip the quoted-JSON pattern group — this is exactly what
    eval/tests/test_compare_lanes.py's fixture rows look like."""
    line = '    {"type": "t1", "' + _RECALL_KEY + '": 1.0, "recall@5": 1.0}\n'
    reasons = scan_line("eval/tests/test_compare_lanes.py", line)
    assert reasons == []


def test_unquoted_pattern_still_fires_inside_eval_tests_files() -> None:
    """The carve-out is narrow: it exempts ONLY the quoted-JSON group. A stray real leaked value
    pasted into an eval/tests/*.py file as a bare (unquoted) form must still be caught — the
    exemption must not become a blanket \"tests are never scanned\" hole."""
    line = f"    # observed {_RECALL_KEY}: 0.923 in a real run (should never be committed)\n"
    reasons = scan_line("eval/tests/test_something.py", line)
    assert any("matched" in r for r in reasons)


def test_quoted_json_pattern_still_fires_for_non_test_python_files() -> None:
    """The eval/tests/ exemption is file-scoped, not extension-scoped — a NON-test .py file (e.g.
    a script under eval/longmemeval/) with a leaked quoted-JSON metric must still be caught."""
    line = '    result = {"' + _RECALL_KEY + '": 0.923}\n'
    reasons = scan_line("eval/longmemeval/some_script.py", line)
    assert any("matched" in r for r in reasons)


def test_markdown_table_cell_with_bare_decimal_is_caught() -> None:
    """The other gap the review called out: a markdown-table cell whose entire content is a bare
    decimal (the exact shape a RENDERED eval/REPORT.template.md would take) was invisible to the
    original scanner, which only checked bare 0.xxx sequences with no table-structure awareness."""
    line = "| single-session | 10 | 10 | 0.923 | 0.887 | -0.036 |\n"
    reasons = scan_line("eval/out/report.md", line)
    assert any("markdown-table metric cell" in r for r in reasons)


def test_markdown_table_cell_negative_delta_is_caught() -> None:
    line = "| OVERALL | 500 | 500 | -0.1 | 92.3 | 1 |\n"
    reasons = scan_line("some/report.md", line)
    assert any("markdown-table metric cell" in r for r in reasons)


def test_markdown_table_cell_check_only_applies_to_md_files() -> None:
    """Table-shaped text inside a non-markdown file (e.g. a Python triple-quoted string building a
    table) is not scanned by the table-cell rule — that rule is markdown-file-scoped by design."""
    line = "| single-session | 10 | 10 | 0.923 | 0.887 | -0.036 |\n"
    reasons = scan_line("eval/render_report.py", line)
    assert not any("markdown-table metric cell" in r for r in reasons)


def test_template_row_with_unrendered_placeholder_is_not_flagged() -> None:
    """eval/REPORT.template.md's own template rows mix a still-unrendered {{PLACEHOLDER}} value
    cell with a FIXED constant bar cell (e.g. a '0' or '1.0' pass/fail threshold) in the SAME row
    — this is permanent template content, not a rendered result, and must not trip the table-cell
    check merely because the row contains a bare-looking bar number."""
    line = "| harmful_collisions | {{KC_HARMFUL_COLLISIONS}} | 0 |\n"
    reasons = scan_line("eval/REPORT.template.md", line)
    assert reasons == []


def test_fully_rendered_row_without_any_placeholder_is_flagged() -> None:
    """The counterpart to the previous test: once EVERY cell in a row is a real number (no
    {{PLACEHOLDER}} token survives), it reads as a rendered report and must be caught."""
    line = "| harmful_collisions | 0.0 | 0 |\n"
    reasons = scan_line("eval/REPORT.template.md", line)
    assert any("markdown-table metric cell" in r for r in reasons)


def test_doc_bare_number_pattern_unaffected_by_the_new_rules() -> None:
    """Regression guard: the pre-existing doc-scoped bare-0.xxx check (README.md/SCHEMA.md/docs/)
    must keep working unchanged alongside the two new pattern classes."""
    line = "observed accuracy 0.65 on the held-out set\n"
    reasons = scan_line("README.md", line)
    assert any("bare 0.xxx in a doc file" in r for r in reasons)


def test_clean_line_produces_no_hits() -> None:
    reasons = scan_line("eval/longmemeval/ingest_retrieve.py", "print('status: running', flush=True)\n")
    assert reasons == []
