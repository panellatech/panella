#!/usr/bin/env python3
"""`make eval-public-scan` — greps TRACKED files (git ls-files; never eval/out/, which is
gitignored and numeric by design) for metric-looking patterns and exits non-zero on hits. The
public-number gate this backs opens only after the dispatcher personally reproduces the run.

Patterns checked (the brief's exact list; see _METRIC_PATTERNS below for the exact regexes —
deliberately NOT restated here as literal example strings, so this docstring cannot trip its own
scan):
  - the "recall@" metric name immediately followed by an index and then a number
  - the "QA-acc"/"QA-accuracy" metric name followed by a number
  - the "key_correctness" metric name followed by a number
  - bare bracketed-decimal sequences in docs (README.md / eval/README.md / SCHEMA.md / docs/) —
    scoped to DOCS specifically (not source code), because source code legitimately contains
    such literals as THRESHOLDS/CONSTANTS (a minimum-stability constant, a similarity tau, etc.)
    that are not eval RESULTS — scanning all tracked files for any such literal would
    false-positive on every threshold constant in the codebase, which is not what this gate is for.
"""
from __future__ import annotations

import re
import subprocess
import sys

_METRIC_PATTERNS = [
    re.compile(r"recall@\d+\s*[:=]\s*\d"),
    re.compile(r"QA-acc(?:uracy)?\s*[:=]\s*0\.\d"),
    re.compile(r"key_correctness\s*[:=]\s*0\.\d"),
]

# Docs where a bare 0.xxx sequence would read as a published result rather than a code constant.
_DOC_GLOBS = ("README.md", "SCHEMA.md")
_DOC_DIR_PREFIXES = ("docs/",)
_BARE_NUMBER_RE = re.compile(r"\b0\.\d{2,4}\b")


def _tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _is_doc_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _DOC_GLOBS:
        return True
    return any(path.startswith(prefix) for prefix in _DOC_DIR_PREFIXES)


def main() -> int:
    files = _tracked_files()
    hits: list[str] = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, start=1):
                    for pattern in _METRIC_PATTERNS:
                        if pattern.search(line):
                            hits.append(f"{path}:{lineno}: {line.strip()} (matched {pattern.pattern!r})")
                    if _is_doc_file(path) and _BARE_NUMBER_RE.search(line):
                        hits.append(f"{path}:{lineno}: {line.strip()} (bare 0.xxx in a doc file)")
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
