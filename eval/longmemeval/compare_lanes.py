#!/usr/bin/env python3
"""Compare the store-lane and facade-lane retrieval dumps, emit recall@k side-by-side + delta.

Honest framing (locked, see eval/README.md and eval/REPORT.template.md): the two lanes are NOT the
same ranking function. The facade path adds profile top-k caps, tenant/read allowlists, lifecycle
filtering, overfetch/backfill, and wing-boost (panella/client.py `search` + `panella_adapter.py`
overfetch). The store lane, by contrast, is raw store search with NO governance semantics at all —
no profile, no allowlist, no lifecycle filter, no overfetch/backfill, no boost (see
`search_store()` in ingest_retrieve.py: a bare POST to `/api/search`). This script therefore
reports "the operational governed read path vs the raw store read path on the same corpus" — never
"pure recall parity" — and ALWAYS emits the enumerated "intentional lane deltas" table alongside
the numbers so a reader can see exactly what differs and why.

Every delta value in that table is DERIVED AT RUNTIME from the real serving-profile render
(`panella/config_render.py::render_serving_profile`, the SAME source `eval/longmemeval/visibility.py`
reads) and the real `panella_adapter.py`/`panella/reader.py` constants/env vars — never a
hand-maintained hardcoded copy that could silently drift from the actual shipped config. See
`_derive_intentional_lane_deltas`.

HARD CONSTRAINT: this script writes its comparison JSON to eval/out/ only. It does not print any
metric value to stdout — status/progress lines only.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from eval._paths import assert_eval_out
from eval.longmemeval.visibility import eval_box_governance_env

# The store lane's own description (locked wording, the finding's exact framing): the store lane
# calls /api/search directly (see ingest_retrieve.py's search_store()) — no profile is resolved, no
# ABAC filter runs, no lifecycle-status exclusion, no overfetch/backfill, no boost. It is the raw
# baseline every facade-side delta below is measured AGAINST.
STORE_LANE_DESCRIPTION = "raw store search (no governance semantics)"


def _derive_intentional_lane_deltas(governance: Any | None = None) -> list[dict[str, str]]:
    """Compute the facade-side "intentional lane deltas" table from the REAL rendered serving
    profile + the real adapter/reader constants — never a hardcoded guess. Any facade feature that
    is OFF by default for the shipped box (docs/SELF_HOST.md) stays OFF here because it IS off on
    the box this function actually queries (out-of-box posture, no cherry-picking, by
    construction — there is no hardcoded value left to drift from the real config).

    `governance` accepts an explicit override (matching `eval/longmemeval/visibility.py`'s
    `eval_wing_room` pattern) so tests can inject a synthetic `Governance` instead of touching the
    box's real live governance; a real run leaves it None and resolves
    `panella.governance.current_governance()`.
    """
    # Deferred imports: keep this module importable without a full panella install for the parts
    # of the test suite that only exercise `compare()`'s pure recall-aggregation math (mirrors
    # visibility.py's own deferred-import style for the same reason).
    from panella import panella_adapter
    from panella.config_render import render_serving_profile
    from panella.governance import current_governance
    from panella.reader import ENV_READER, ENV_RERANKER

    if governance is not None:
        gov = governance
        profile = yaml.safe_load(render_serving_profile(gov))
    else:
        with eval_box_governance_env():
            gov = current_governance()
            profile = yaml.safe_load(render_serving_profile(gov))

    max_query_k = profile["max_query_k"]
    read_allowlist = profile["read_allowlist"]
    wing_boost_default = profile["wing_boost"]["default"]
    reader_state = os.environ.get(ENV_READER, "off")
    reranker_state = os.environ.get(ENV_RERANKER, "off")
    excluded_statuses = sorted(panella_adapter.EXCLUDED_RECALL_STATUSES)
    overfetch_n = panella_adapter.PANELLA_OVERFETCH_N

    return [
        {
            "delta": "profile top-k cap",
            "shipped_default": f"max_query_k: {max_query_k} (serving profile, rendered live)",
            "effect": "facade search() clamps k to min(requested, profile.max_query_k) — panella/client.py MemoryClient.search",
        },
        {
            "delta": "tenant/read allowlist (ABAC)",
            "shipped_default": f"read_allowlist: {read_allowlist!r} (serving profile, rendered live)",
            "effect": "rows outside the owner wing/room are filtered before the caller ever sees them — panella/client.py _filter_hits",
        },
        {
            "delta": "lifecycle validity filtering",
            "shipped_default": f"excluded statuses: {excluded_statuses!r} (panella_adapter.EXCLUDED_RECALL_STATUSES, read live)",
            "effect": "candidate/superseded/tombstoned rows are excluded from recall on the facade lane only — panella_adapter.py — listed for completeness; the store lane applies no such filter at all",
        },
        {
            "delta": "overfetch + backfill",
            "shipped_default": f"PANELLA_OVERFETCH_N = {overfetch_n} (adapter-level, read live; facade lane only)",
            "effect": "the validity drop runs on every facade search and can remove top-k hits, so a backfill pool is always fetched — panella_adapter.py search_memories",
        },
        {
            "delta": "wing soft-boost",
            "shipped_default": f"wing_boost.default: {wing_boost_default} (serving profile, rendered live — 1.0 == neutral/off)",
            "effect": "when a profile sets a non-1.0 boost, scores are multiplied by the wing_boost factor before ranking — panella_adapter.py search_memories + panella/client.py _apply_profile_boost",
        },
        {
            "delta": "reader++ / cross-encoder reranking",
            "shipped_default": f"{ENV_READER}={reader_state}, {ENV_RERANKER}={reranker_state} (read live from this run's actual environment)",
            "effect": "when enabled, panella/reader.py re-orders hits post-filter — reflects whatever THIS box's env actually has set, never a hardcoded default",
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


def compare(store_path: Path, facade_path: Path, *, governance: Any | None = None) -> dict:
    """`governance` accepts an explicit override for tests (see `_derive_intentional_lane_deltas`);
    a real run leaves it None and resolves the box's actual live governance."""
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
        "store_lane": STORE_LANE_DESCRIPTION,
        "intentional_lane_deltas": _derive_intentional_lane_deltas(governance),
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
    out_path = assert_eval_out(a.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote lane comparison to {out_path} (per-type + intentional lane deltas; no numbers on stdout)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
