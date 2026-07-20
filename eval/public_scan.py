#!/usr/bin/env python3
"""`make eval-public-scan` — greps TRACKED files (git ls-files; never eval/out/, which is
gitignored and numeric by design) for metric-looking patterns and exits non-zero on hits. The
public-number gate this backs opens only after the dispatcher personally reproduces the run.

Patterns checked (the brief's exact list; see _METRIC_PATTERNS_UNQUOTED / _METRIC_PATTERNS_QUOTED
below for the exact regexes — deliberately NOT restated here as literal example strings, so this
docstring cannot trip its own scan; this rule also applies to every inline comment in this module,
not just this docstring):
  - each named metric (recall@N / QA-accuracy / key_correctness) immediately followed by a number,
    in BOTH the unquoted `name<sep>number` form (code/log-line style) and the quoted-JSON
    `"name"<sep>number` form (a metric dict pasted verbatim into a commit message, doc, or PR body
    uses this shape, which the unquoted-form regex alone does not match)
  - a markdown-table cell whose ENTIRE content is a bare decimal number, independent of which
    metric name labels the column — the shape a RENDERED `eval/REPORT.template.md` takes, as
    opposed to the still-templated form (an unreplaced `{{PLACEHOLDER}}` token in the same row
    marks it as template content, not a result, and is excluded)
  - bare bracketed-decimal sequences in docs (README.md / eval/README.md / SCHEMA.md / docs/) —
    scoped to DOCS specifically (not source code), because source code legitimately contains
    such literals as THRESHOLDS/CONSTANTS (a minimum-stability constant, a similarity tau, etc.)
    that are not eval RESULTS — scanning all tracked files for any such literal would
    false-positive on every threshold constant in the codebase, which is not what this gate is for.

Known, deliberate exclusion: `eval/tests/*.py` fixture data that happens to LOOK like a metric dict
(a hand-built dict literal a unit test feeds into a scorer to assert its math) is test input, not
a published eval result — see `_is_test_fixture_file` below for the narrow carve-out. This exempts
ONLY the quoted-JSON pattern group (the Python-dict-literal shape) for `eval/tests/*.py` files;
the unquoted-form and markdown-table checks still run against test files unconditionally, so a
real leaked number pasted as a stray comment or string is still caught. Test fixtures never ship a
real run's numbers — they are synthetic values chosen to exercise scorer logic, committed
regardless of whether any eval has ever run, so this carve-out does not weaken the gate.
"""
from __future__ import annotations

import re
import subprocess
import sys

_METRIC_NAMES = (
    r"recall@\d+",
    r"QA-acc(?:uracy)?",
    r"key_correctness",
)

# Unquoted key, code/log-line style: name followed by `:` or `=` then a digit. Checked against
# EVERY tracked file, including eval/tests/*.py — a stray real leaked number pasted as a bare
# `name: 0.9` comment/string would still be caught here even where the quoted-JSON check below is
# narrowly exempted for test fixtures.
_METRIC_PATTERNS_UNQUOTED = [re.compile(rf"{name}\s*[:=]\s*\d") for name in _METRIC_NAMES]

# Quoted-JSON style: a metric dict pasted verbatim (e.g. into a commit message, a doc code fence,
# or a PR body) quotes the key and always uses `:` (never `=`) — a different token sequence than
# the unquoted form above. This is ALSO the exact shape a Python dict literal takes when keyed by
# one of these metric names, which `eval/tests/*.py` legitimately uses as hand-built fixture input
# to a scorer under test (synthetic values exercising scorer math, never a published eval result)
# — see `_is_test_fixture_file` for the narrow, file-scoped exemption from JUST this pattern group.
_METRIC_PATTERNS_QUOTED = [re.compile(rf'"{name}"\s*:\s*\d') for name in _METRIC_NAMES]

# A markdown-table cell whose entire (trimmed) content is a bare decimal number — the exact shape
# eval/REPORT.template.md's rendered recall@k / QA-accuracy / precision / recall columns take,
# independent of which metric name labels the column header. Matches `| 0.923 |`, `| -0.1 |`,
# `| 92.3 |` (a percentage-style cell), deliberately NOT restricted to a `0.xxx` shape like the
# doc-only bare-number check below, since a committed report table could carry any of those forms.
_TABLE_CELL_METRIC_RE = re.compile(r"\|\s*-?\d+(?:\.\d+)?\s*\|")

# A row that still contains an UNREPLACED `{{PLACEHOLDER}}` token is, by construction, the
# template itself — not a rendered report. eval/REPORT.template.md's own "Bar" columns
# legitimately carry FIXED constant thresholds (`1.0`, `0`, `>=0.90`) alongside still-unrendered
# `{{KC_...}}` value cells in the SAME row; those bars are permanent template content, not a
# result, and must not trip the table-cell check merely for sharing a row with a placeholder.
_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")

# Docs where a bare 0.xxx sequence would read as a published result rather than a code constant.
_DOC_GLOBS = ("README.md", "SCHEMA.md")
_DOC_DIR_PREFIXES = ("docs/",)
_BARE_NUMBER_RE = re.compile(r"\b0\.\d{2,4}\b")

# Per-file, per-literal exemptions from ONLY the doc bare-number rule. The c0-3b-drift evidence
# bundle documents the METHODOLOGY it was run with — 0.9999 is the cosine-similarity acceptance
# THRESHOLD passed to the drift check, not a measured eval result (the module docstring's
# threshold-vs-result distinction, which the docs/ prefix scope cannot express on its own).
# Deliberately literal-scoped, not file-scoped: any OTHER bare number added to these same files
# still trips the rule, and every metric-name / table-cell pattern still runs here unchanged.
_DOC_BARE_NUMBER_ALLOWLIST: dict[str, frozenset[str]] = {
    "docs/evidence/c0-3b-drift/compare.py": frozenset({"0.9999"}),
    "docs/evidence/c0-3b-drift/corpus.txt": frozenset({"0.9999"}),
    "docs/evidence/c0-3b-drift/evidence.md": frozenset({"0.9999"}),
    "docs/evidence/c0-3b-drift/run_drift.sh": frozenset({"0.9999"}),
}


def _tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _is_doc_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _DOC_GLOBS:
        return True
    return any(path.startswith(prefix) for prefix in _DOC_DIR_PREFIXES)


def _is_markdown_file(path: str) -> bool:
    """Any tracked `.md` file — a superset of `_is_doc_file` (which only covers the named
    README.md/SCHEMA.md globs + docs/). A markdown table is a markdown-file concept, so the
    table-cell metric check scans every `.md` (e.g. `eval/REPORT.template.md` itself, which should
    only ever contain `{{PLACEHOLDER}}` tokens, never a filled-in number — catching a stray
    accidentally-committed filled report is exactly the point)."""
    return path.endswith(".md")


def _is_test_fixture_file(path: str) -> bool:
    """`eval/tests/*.py` — the ONLY files exempted from the quoted-JSON metric-pattern group (see
    module docstring). Deliberately narrow: a Python test module, and only under eval/tests/, not
    any `.py` file anywhere (a leaked number in a NON-test module must still be caught)."""
    return path.startswith("eval/tests/") and path.endswith(".py")


def scan_line(path: str, line: str) -> list[str]:
    """Apply every rule to a single (path, line) pair, returning zero or more short reason strings
    (empty = clean; e.g. "bare 0.xxx in a doc file"). Pure and file-I/O-free — this is what
    `eval/tests/test_public_scan.py` exercises directly, so each pattern class (unquoted,
    quoted-JSON, doc bare-number, markdown-table-cell) is unit-tested without needing a real git
    checkout or filesystem. Callers own formatting the reason alongside path/lineno/line content."""
    reasons: list[str] = []
    for pattern in _METRIC_PATTERNS_UNQUOTED:
        if pattern.search(line):
            reasons.append(f"matched {pattern.pattern!r}")
    if not _is_test_fixture_file(path):
        for pattern in _METRIC_PATTERNS_QUOTED:
            if pattern.search(line):
                reasons.append(f"matched {pattern.pattern!r}")
    if _is_doc_file(path):
        allowed = _DOC_BARE_NUMBER_ALLOWLIST.get(path, frozenset())
        if any(m not in allowed for m in _BARE_NUMBER_RE.findall(line)):
            reasons.append("bare 0.xxx in a doc file")
    if (
        _is_markdown_file(path)
        and _TABLE_CELL_METRIC_RE.search(line)
        and not _PLACEHOLDER_TOKEN_RE.search(line)
    ):
        reasons.append("markdown-table metric cell")
    return reasons


def main() -> int:
    files = _tracked_files()
    hits: list[str] = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, start=1):
                    for reason in scan_line(path, line):
                        hits.append(f"{path}:{lineno}: {line.strip()} ({reason})")
        except (OSError, UnicodeDecodeError):
            continue

    print("eval-public-scan: scanning TRACKED files for metric-looking patterns...")
    if hits:
        print("eval-public-scan: FAIL — metric-looking patterns found in tracked files:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        print(
            "eval-public-scan: numeric output belongs ONLY under eval/out/ (gitignored).",
            file=sys.stderr,
        )
        return 1
    print("eval-public-scan: PASS — no metric-looking patterns in the tracked tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
