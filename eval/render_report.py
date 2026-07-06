#!/usr/bin/env python3
"""Render eval/REPORT.template.md into eval/out/report.md with a run's actual numbers.

HARD CONSTRAINT: this script is the ONLY place numbers cross from eval/out/*.json into a rendered
document, and that document is written under eval/out/ (gitignored) — never into the template
itself, never into a tracked file. `make eval-public-scan` greps the TRACKED tree for metric-looking
patterns to catch any drift from this rule.

Inputs (all optional — a section renders "not run" if its input is missing):
  --store-retrieval   eval/out/stage_a_retrieval.store.json  (ingest_retrieve.py --lane store)
  --facade-retrieval  eval/out/stage_a_retrieval.facade.json (ingest_retrieve.py --lane facade)
  --lane-comparison   eval/out/lane_comparison.json           (compare_lanes.py)
  --qa                eval/out/stage_a_qa.json                (qa.py)
  --key-correctness   eval/out/key_correctness_report.json    (key_correctness_eval.py --out)
  --supersede-report  eval/out/supersede_report.json          (score_supersede.py --out)
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = _HERE / "REPORT.template.md"
DEFAULT_OUT = _HERE / "out" / "report.md"


def _load_json(path: Path | None) -> dict | list | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _qa_agg(qa_rows: list[dict]) -> dict[str, dict[str, float]]:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in qa_rows:
        if r.get("errored"):
            continue
        by_type[r["type"]].append(r)
    out = {}
    for t, rs in by_type.items():
        out[t] = {"n": len(rs), "acc": sum(x["correct"] for x in rs) / len(rs)}
    all_scored = [r for r in qa_rows if not r.get("errored")]
    out["OVERALL"] = {
        "n": len(all_scored),
        "acc": (sum(x["correct"] for x in all_scored) / len(all_scored)) if all_scored else None,
    }
    return out


def render(
    *,
    template_path: Path = DEFAULT_TEMPLATE,
    out_path: Path = DEFAULT_OUT,
    lane_comparison: dict | None = None,
    qa_rows: list[dict] | None = None,
    key_correctness: dict | None = None,
    supersede_report: dict | None = None,
    dataset_name: str = "n/a",
    dataset_sha256: str = "n/a",
    panella_commit: str = "n/a",
    compose_project: str = "panella-eval",
    store_port: str = "18000",
    facade_port: str = "18001",
    http_profile: str = "serving",
    n_per_type: str = "n/a",
    reader_model: str = "n/a",
    judge_model: str = "n/a",
    reader_transport: str = "n/a",
    judge_transport: str = "n/a",
    reader_k: str = "n/a",
) -> str:
    text = template_path.read_text(encoding="utf-8")
    subs: dict[str, str] = {
        "DATASET_NAME": dataset_name,
        "DATASET_SHA256": dataset_sha256,
        "PANELLA_COMMIT": panella_commit,
        "COMPOSE_PROJECT": compose_project,
        "STORE_PORT": store_port,
        "FACADE_PORT": facade_port,
        "HTTP_PROFILE": http_profile,
        "RUN_STARTED_AT": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "N_PER_TYPE": n_per_type,
        "READER_MODEL": reader_model,
        "JUDGE_MODEL": judge_model,
        "READER_TRANSPORT": reader_transport,
        "JUDGE_TRANSPORT": judge_transport,
        "READER_K": reader_k,
    }

    # Retrieval recall@k table.
    if lane_comparison:
        per_type_rows = []
        overall = {}
        for row in lane_comparison["per_type"]:
            line = (
                f"| {row['type']} | {row['store_n']} | {row['facade_n']} | "
                f"{_fmt(row.get('store_recall@1'))} | {_fmt(row.get('facade_recall@1'))} | {_fmt(row.get('delta_recall@1'))} | "
                f"{_fmt(row.get('store_recall@5'))} | {_fmt(row.get('facade_recall@5'))} | {_fmt(row.get('delta_recall@5'))} | "
                f"{_fmt(row.get('store_recall@10'))} | {_fmt(row.get('facade_recall@10'))} | {_fmt(row.get('delta_recall@10'))} |"
            )
            if row["type"] == "OVERALL":
                overall = row
            else:
                per_type_rows.append(line)
        subs["PER_TYPE_ROWS"] = " |\n| ".join(per_type_rows) if per_type_rows else "(no per-type rows)"
        subs["OVERALL_STORE_N"] = str(overall.get("store_n", "n/a"))
        subs["OVERALL_FACADE_N"] = str(overall.get("facade_n", "n/a"))
        subs["OVERALL_STORE_R1"] = _fmt(overall.get("store_recall@1"))
        subs["OVERALL_FACADE_R1"] = _fmt(overall.get("facade_recall@1"))
        subs["OVERALL_DELTA_R1"] = _fmt(overall.get("delta_recall@1"))
        subs["OVERALL_STORE_R5"] = _fmt(overall.get("store_recall@5"))
        subs["OVERALL_FACADE_R5"] = _fmt(overall.get("facade_recall@5"))
        subs["OVERALL_DELTA_R5"] = _fmt(overall.get("delta_recall@5"))
        subs["OVERALL_STORE_R10"] = _fmt(overall.get("store_recall@10"))
        subs["OVERALL_FACADE_R10"] = _fmt(overall.get("facade_recall@10"))
        subs["OVERALL_DELTA_R10"] = _fmt(overall.get("delta_recall@10"))
        delta_rows = [
            f"| {d['delta']} | {d['shipped_default']} | {d['effect']} |" for d in lane_comparison["intentional_lane_deltas"]
        ]
        subs["INTENTIONAL_LANE_DELTAS_ROWS"] = " |\n| ".join(delta_rows)
    else:
        subs["PER_TYPE_ROWS"] = "(lane comparison not run)"
        for k in (
            "OVERALL_STORE_N", "OVERALL_FACADE_N", "OVERALL_STORE_R1", "OVERALL_FACADE_R1", "OVERALL_DELTA_R1",
            "OVERALL_STORE_R5", "OVERALL_FACADE_R5", "OVERALL_DELTA_R5", "OVERALL_STORE_R10", "OVERALL_FACADE_R10",
            "OVERALL_DELTA_R10",
        ):
            subs[k] = "n/a"
        subs["INTENTIONAL_LANE_DELTAS_ROWS"] = "(lane comparison not run — see compare_lanes.py's INTENTIONAL_LANE_DELTAS)"

    # QA-accuracy table.
    if qa_rows:
        agg = _qa_agg(qa_rows)
        rows = [f"| {t} | {v['n']} | {_fmt(v['acc'])} |" for t, v in sorted(agg.items()) if t != "OVERALL"]
        subs["QA_PER_TYPE_ROWS"] = " |\n| ".join(rows) if rows else "(no scored rows)"
        subs["QA_OVERALL_N"] = str(agg.get("OVERALL", {}).get("n", "n/a"))
        subs["QA_OVERALL_ACC"] = _fmt(agg.get("OVERALL", {}).get("acc"))
    else:
        subs["QA_PER_TYPE_ROWS"] = "(QA-accuracy not run)"
        subs["QA_OVERALL_N"] = "n/a"
        subs["QA_OVERALL_ACC"] = "n/a"

    # key_correctness section.
    if key_correctness:
        rep = key_correctness.get("report", {})
        subs["KC_SCHEMA_VALIDITY"] = _fmt(rep.get("schema_validity"))
        subs["KC_KEY_CORRECTNESS"] = _fmt(rep.get("key_correctness"))
        subs["KC_KEY_STABILITY"] = _fmt(rep.get("key_stability"))
        subs["KC_SUPERSEDE_PRECISION"] = _fmt(rep.get("supersede_precision"))
        subs["KC_HARMFUL_COLLISIONS"] = str(rep.get("harmful_collisions", "n/a"))
        subs["KC_NEG_FP_RATE"] = _fmt(rep.get("negative_false_positive_rate"))
        subs["KC_VERDICT"] = str(key_correctness.get("verdict", "n/a"))
    else:
        for k in ("KC_SCHEMA_VALIDITY", "KC_KEY_CORRECTNESS", "KC_KEY_STABILITY", "KC_SUPERSEDE_PRECISION", "KC_HARMFUL_COLLISIONS", "KC_NEG_FP_RATE", "KC_VERDICT"):
            subs[k] = "n/a (not run)"

    # supersede confusion-matrix section.
    if supersede_report:
        precision = supersede_report.get("precision", {})
        recall = supersede_report.get("recall", {})
        for label in ("supersede", "coexist", "unrelated"):
            subs[f"SUP_PRECISION_{label.upper()}"] = _fmt(precision.get(label))
            subs[f"SUP_RECALL_{label.upper()}"] = _fmt(recall.get(label))
        subs["SUP_FALSE_MERGE_COUNT"] = str(supersede_report.get("false_merge_count", "n/a"))
    else:
        for label in ("SUPERSEDE", "COEXIST", "UNRELATED"):
            subs[f"SUP_PRECISION_{label}"] = "n/a (not run)"
            subs[f"SUP_RECALL_{label}"] = "n/a (not run)"
        subs["SUP_FALSE_MERGE_COUNT"] = "n/a (not run)"

    for key, value in subs.items():
        text = text.replace("{{" + key + "}}", str(value))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--store-retrieval", type=Path, default=None)
    ap.add_argument("--facade-retrieval", type=Path, default=None)
    ap.add_argument("--lane-comparison", type=Path, default=None)
    ap.add_argument("--qa", type=Path, default=None)
    ap.add_argument("--key-correctness", type=Path, default=None)
    ap.add_argument("--supersede-report", type=Path, default=None)
    ap.add_argument("--dataset-name", default="n/a")
    ap.add_argument("--dataset-sha256", default="n/a")
    ap.add_argument("--panella-commit", default="n/a")
    ap.add_argument("--compose-project", default="panella-eval")
    ap.add_argument("--store-port", default="18000")
    ap.add_argument("--facade-port", default="18001")
    ap.add_argument("--http-profile", default="serving")
    ap.add_argument("--n-per-type", default="n/a")
    ap.add_argument("--reader-model", default="n/a")
    ap.add_argument("--judge-model", default="n/a")
    ap.add_argument("--reader-transport", default="n/a")
    ap.add_argument("--judge-transport", default="n/a")
    ap.add_argument("--reader-k", default="n/a")
    a = ap.parse_args(argv)

    render(
        template_path=a.template,
        out_path=a.out,
        lane_comparison=_load_json(a.lane_comparison),
        qa_rows=_load_json(a.qa),
        key_correctness=_load_json(a.key_correctness),
        supersede_report=_load_json(a.supersede_report),
        dataset_name=a.dataset_name,
        dataset_sha256=a.dataset_sha256,
        panella_commit=a.panella_commit,
        compose_project=a.compose_project,
        store_port=a.store_port,
        facade_port=a.facade_port,
        http_profile=a.http_profile,
        n_per_type=a.n_per_type,
        reader_model=a.reader_model,
        judge_model=a.judge_model,
        reader_transport=a.reader_transport,
        judge_transport=a.judge_transport,
        reader_k=a.reader_k,
    )
    print(f"wrote {a.out} (no numbers printed to stdout)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
