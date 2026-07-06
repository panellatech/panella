#!/usr/bin/env python3
"""Drive `make eval-smoke`: n=2/type end-to-end on BOTH lanes against the throwaway eval box,
writing a MACHINE-READABLE eval/out/smoke-status.json (per-stage pass/fail/skipped + canary
result) — the dispatcher reads this file, never prose or stdout. Never fakes a result: a stage
that cannot run is recorded "skipped", never silently reported as passing.

Requires `make eval-up` to have already minted eval/out/compose.env + eval/out/state.env, and a
smoke-sized fixture dataset at eval/out/smoke_fixture.json (created by `make eval-selftest` via
`eval/tests/fixtures/smoke_dataset.json`, copied in by the Makefile so this script has no
dataset-shape knowledge duplicated from the tests).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

OUT_DIR = Path("eval/out")
STATUS_PATH = OUT_DIR / "smoke-status.json"


def _write_status(status: dict) -> None:
    STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")


def _run(status: dict, stage: str, argv: list[str], env: dict[str, str] | None = None) -> bool:
    result = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    ok = result.returncode == 0
    status["stages"][stage] = "pass" if ok else "fail"
    if not ok:
        sys.stderr.write(f"[{stage}] FAILED (exit {result.returncode})\n{result.stdout}\n{result.stderr}\n")
    return ok


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    status: dict = {"stages": {}, "canary": "NOT-RUN", "overall": "fail"}

    env = dict(os.environ)
    for env_file in ("eval/out/compose.env", "eval/out/state.env"):
        path = Path(env_file)
        if not path.exists():
            print(f"eval-smoke: {env_file} missing — run `make eval-up` first")
            status["stages"]["preflight"] = "fail"
            _write_status(status)
            return 1
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                env[key] = value
    env["PANELLA_EVAL_API_KEY"] = env.get("PANELLA_API_KEY", "")

    smoke_data = OUT_DIR / "smoke_fixture.json"
    if not smoke_data.exists():
        print("eval-smoke: eval/out/smoke_fixture.json missing — run `make eval-selftest` first (it stages the smoke fixture)")
        status["stages"]["preflight"] = "fail"
        _write_status(status)
        return 1

    ok_store = _run(
        status,
        "ingest_retrieve_store",
        [
            sys.executable, "-m", "eval.longmemeval.ingest_retrieve", "--lane", "store", "--n-per-type", "2",
            "--data", str(smoke_data), "--out", "eval/out/smoke_retrieval.store.json",
        ],
        env=env,
    )
    ok_facade = _run(
        status,
        "ingest_retrieve_facade",
        [
            sys.executable, "-m", "eval.longmemeval.ingest_retrieve", "--lane", "facade", "--n-per-type", "2",
            "--data", str(smoke_data), "--out", "eval/out/smoke_retrieval.facade.json",
        ],
        env=env,
    )
    # ingest_retrieve.py's facade lane runs the visibility canary itself before any recall is
    # computed (exit 3 on canary failure) — reflect that outcome here rather than re-deriving it.
    status["canary"] = "pass" if ok_facade else "fail-or-not-reached"

    ok_compare = False
    if ok_store and ok_facade:
        ok_compare = _run(
            status,
            "compare_lanes",
            [
                sys.executable, "-m", "eval.longmemeval.compare_lanes",
                "--store", "eval/out/smoke_retrieval.store.json",
                "--facade", "eval/out/smoke_retrieval.facade.json",
                "--out", "eval/out/smoke_lane_comparison.json",
            ],
        )
    else:
        status["stages"]["compare_lanes"] = "skipped"

    ok_report = False
    if ok_compare:
        ok_report = _run(
            status,
            "render_report",
            [
                sys.executable, "eval/render_report.py",
                "--lane-comparison", "eval/out/smoke_lane_comparison.json",
                "--out", "eval/out/smoke_report.md",
            ],
        )
    else:
        status["stages"]["render_report"] = "skipped"

    status["overall"] = "pass" if (ok_store and ok_facade and ok_compare and ok_report) else "fail"
    _write_status(status)
    print(f"eval-smoke: wrote {STATUS_PATH} (overall={status['overall']}; no metric values here)")
    return 0 if status["overall"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
