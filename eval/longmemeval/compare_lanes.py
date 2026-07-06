#!/usr/bin/env python3
"""Compare the store-lane and facade-lane retrieval dumps, emit recall@k side-by-side + delta.

Honest framing (locked, see eval/README.md and eval/REPORT.template.md): the two lanes are NOT the
same ranking function. The facade path adds profile top-k caps, tenant/read allowlists, lifecycle
filtering, overfetch/backfill, and wing-boost (panella/client.py `search` + `panella_adapter.py`
overfetch). This script therefore reports "the operational governed read path vs the raw store read
path on the same corpus" — never "pure recall parity" — and ALWAYS emits the enumerated
"intentional lane deltas" table alongside the numbers so a reader can see exactly what differs and
why, in the SAME run's actual config values (never a cherry-picked subset).

HARD CONSTRAINT: this script writes its comparison JSON to eval/out/ only. It does not print any
metric value to stdout — status/progress lines only.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path

# The facade features that differ from a pure store search, and whether each is ON by default for
# the shipped self-host box (docs/SELF_HOST.md `serving` profile — panella/config_render.py). Any
# facade feature that is OFF by default for the shipped box MUST stay OFF in the eval box too (the
# brief's "out-of-box posture, no cherry-picking" rule) — this table is the single source the
# report template reads, so a future flip in config_render.py needs a matching update HERE.
INTENTIONAL_LANE_DELTAS = [
    {
        "delta": "profile top-k cap",
        "shipped_default": "max_query_k: 20 (serving profile)",
        "effect": "facade search() clamps k to min(requested, profile.max_query_k) — panella/client.py MemoryClient.search",
    },
    {
        "delta": "tenant/read allowlist (ABAC)",
        "shipped_default": "read_allowlist: ['<owner_wing>/*'] (serving profile)",
        "effect": "rows outside the owner wing/room are filtered before the caller ever sees them — panella/client.py _filter_hits",
    },
    {
        "delta": "lifecycle validity filtering",
        "shipped_default": "always on (not profile-gated)",
        "effect": "candidate/superseded/tombstoned rows are excluded from recall on BOTH lanes — panella_adapter.py EXCLUDED_RECALL_STATUSES — listed for completeness even though it does not differ between lanes",
    },
    {
        "delta": "overfetch + backfill",
        "shipped_default": "PANELLA_OVERFETCH_N = 40 (adapter-level, both lanes call the same adapter)",
        "effect": "the validity drop runs on every search and can remove top-k hits, so a backfill pool is always fetched — panella_adapter.py search_memories",
    },
    {
        "delta": "wing soft-boost",
        "shipped_default": "wing_boost: {default: 1.0} (serving profile — i.e. OFF/neutral out of the box)",
        "effect": "when a profile sets a non-1.0 boost, scores are multiplied by the wing_boost factor before ranking — panella_adapter.py search_memories + panella/client.py _apply_profile_boost",
    },
    {
        "delta": "reader++ / cross-encoder reranking",
        "shipped_default": "PANELLA_READER=off, PANELLA_RERANKER=off (both OFF by default)",
        "effect": "when enabled, panella/reader.py re-orders hits post-filter — OFF here to match the shipped out-of-box posture",
    },
]


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _agg_recall(rows: list[dict]) -> dict[str, dict[str, float]]:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_type[r["type"]].append(r)
    out: dict[str, dict[str, float]] = {}
    for t, rs in by_type.items():
        out[t] = {
            k: sum(x[k] for x in rs if x.get(k) is not None) / max(1, sum(1 for x in rs if x.get(k) is not None))
            for k in ("recall@1", "recall@5", "recall@10")
        }
        out[t]["n"] = len(rs)
    all_rows = rows
    out["OVERALL"] = {
        k: sum(x[k] for x in all_rows if x.get(k) is not None) / max(1, sum(1 for x in all_rows if x.get(k) is not None))
        for k in ("recall@1", "recall@5", "recall@10")
    }
    out["OVERALL"]["n"] = len(all_rows)
    return out


def compare(store_path: Path, facade_path: Path) -> dict:
    store_rows = _load(store_path)
    facade_rows = _load(facade_path)
    store_agg = _agg_recall(store_rows)
    facade_agg = _agg_recall(facade_rows)
    types = sorted(set(store_agg) | set(facade_agg))
    per_type = []
    for t in types:
        s = store_agg.get(t, {})
        f = facade_agg.get(t, {})
        row = {"type": t, "store_n": s.get("n", 0), "facade_n": f.get("n", 0)}
        for k in ("recall@1", "recall@5", "recall@10"):
            sv, fv = s.get(k), f.get(k)
            row[f"store_{k}"] = sv
            row[f"facade_{k}"] = fv
            row[f"delta_{k}"] = (fv - sv) if (sv is not None and fv is not None) else None
        per_type.append(row)
    return {
        "per_type": per_type,
        "intentional_lane_deltas": INTENTIONAL_LANE_DELTAS,
        "framing": (
            "This compares the operational governed read path (facade) vs the raw store read path "
            "on the SAME corpus. These are NOT the same ranking function — see intentional_lane_deltas. "
            "This is not a leaderboard entry."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", required=True, help="path to the store-lane retrieval dump JSON")
    ap.add_argument("--facade", required=True, help="path to the facade-lane retrieval dump JSON")
    ap.add_argument("--out", default=os.path.join("eval", "out", "lane_comparison.json"))
    a = ap.parse_args(argv)
    result = compare(Path(a.store), Path(a.facade))
    out_path = Path(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote lane comparison to {out_path} (per-type + intentional lane deltas; no numbers on stdout)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
