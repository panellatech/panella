"""Shared enforcement: every tool that writes a metric artifact (recall@k, QA-accuracy,
key_correctness, supersede precision/recall, or a rendered report containing any of the above)
MUST resolve its `--out` path through `assert_eval_out` before writing. This is the mechanical
half of the brief's "no eval numbers outside eval/out/" constraint — `eval/public_scan.py` is the
other half (it greps the TRACKED tree for anything that slipped past this gate anyway).

No exceptions, no flag to bypass: a path that resolves outside `eval/out/` is a hard `exit 2`,
never a warning, never a silent redirect.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The repo root is two parents up from this file (eval/_paths.py -> eval/ -> repo root), matching
# every other eval/ module's `Path(__file__).resolve().parent`-style anchoring (never CWD-relative
# — a tool invoked from a different directory must still resolve the SAME eval/out/).
_REPO_ROOT = Path(__file__).resolve().parent.parent
EVAL_OUT_DIR = _REPO_ROOT / "eval" / "out"


def assert_eval_out(path: str | Path) -> Path:
    """Resolve `path` and hard-fail (exit 2) unless it lands under `eval/out/`. Returns the
    resolved absolute Path on success so callers can use it directly (e.g.
    `out_path = assert_eval_out(args.out)`).

    Resolution is relative to CWD when `path` is relative (matching `argparse`/`Path` defaults —
    every caller here is a CLI script the operator runs from the repo root), then compared against
    the canonical `eval/out/` directory. A path that resolves outside it (a bare filename that
    would land in CWD, an absolute path elsewhere, or a `../` escape) is refused before any write.
    """
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(EVAL_OUT_DIR)
    except ValueError:
        sys.exit(
            f"REFUSING to write metric output to {resolved} — it is not under {EVAL_OUT_DIR} "
            "(the brief's hard constraint: ALL numeric output lands ONLY under eval/out/, which "
            "is gitignored). Pass an --out path under eval/out/, e.g. "
            f"eval/out/{Path(path).name}."
        )
    return resolved
