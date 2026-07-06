#!/usr/bin/env python3
"""Generate the supersede confusion-matrix goldset v0 (synthetic, fixed seed, deterministic).

Produces >=40 cases spanning the three pair labels (supersede / coexist / unrelated — see
SCHEMA.md), synthetic content ONLY (no real names/data — every entity/value below is a
placeholder). Deterministic: a fixed seed + fixed generation order means a fresh regen is
byte-identical to the committed `supersede_v0.json` (`--check` asserts this, plus schema
validity against `supersede.schema.json`).

Usage:
  python eval/goldsets/synth_supersede.py --out eval/goldsets/supersede_v0.json
  python eval/goldsets/synth_supersede.py --check   # regen == committed AND schema-valid
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SEED = 20260707
GOLDSET_NAME = "panella-supersede-confusion-matrix"
VERSION = "v0"
_HERE = Path(__file__).resolve().parent
DEFAULT_OUT = _HERE / "supersede_v0.json"
SCHEMA_PATH = _HERE / "supersede.schema.json"

# Synthetic slot templates — (slot_name, earlier_value, later_value). Every value here is a
# placeholder (fictional company/city/product names), never real personal data. Each template
# generates one `supersede` case: two facts for the SAME slot, later supersedes earlier.
_SUPERSEDE_SLOTS: list[tuple[str, str, str]] = [
    ("employer", "works at Northwind Traders", "now works at Contoso Labs"),
    ("home_city", "lives in Rivertown", "moved to Lakeside last month"),
    ("code_editor", "codes in Nimbus Editor", "switched to Vertex IDE"),
    ("programming_language", "writes mostly Solstice-lang", "writes mostly Umbra-lang now"),
    ("coffee_style", "drinks black coffee, no sugar", "switched to oat-milk lattes"),
    ("job_title", "works as a systems analyst", "was promoted to engineering lead"),
    ("phone_model", "carries a Solstice X12", "upgraded to a Solstice X14"),
    ("commute_mode", "commutes by bicycle", "now commutes by train"),
    ("primary_browser", "uses Aurora Browser", "switched to Meridian Browser"),
    ("gym_membership", "has a membership at Ironclad Gym", "switched to Summit Fitness"),
]

# Synthetic independent-fact templates for `coexist` pairs — two facts about DIFFERENT slots that
# both remain true at once (never update one another).
_COEXIST_PAIRS: list[tuple[str, str]] = [
    ("avoids gluten in their diet", "enjoys ambient electronic music"),
    ("prefers window seats when traveling", "keeps a succulent on their desk"),
    ("reads mostly historical fiction", "plays badminton on weekends"),
    ("dislikes spicy food", "collects vintage postage stamps"),
    ("works a hybrid schedule", "volunteers at a community garden monthly"),
    ("uses a standing desk", "subscribes to a weekly astronomy newsletter"),
    ("prefers tea over coffee in the evening", "practices calligraphy as a hobby"),
    ("keeps a bullet journal", "follows a strict Tuesday gym routine"),
    ("is left-handed", "enjoys documentary films"),
    ("has two houseplants named after constellations", "bikes to the farmers market on Saturdays"),
]

# Synthetic unrelated-fact templates for `unrelated` pairs — two facts sharing NO slot/subject.
# Deliberately overlapping in surface form with the supersede slots above (a naive
# recency-only classifier would be tempted to merge these; that is exactly the trap).
_UNRELATED_PAIRS: list[tuple[str, str]] = [
    ("works at Northwind Traders", "drinks black coffee, no sugar"),
    ("lives in Rivertown", "codes in Nimbus Editor"),
    ("carries a Solstice X12", "prefers the Nimbus code editor"),
    ("commutes by bicycle", "reads mostly historical fiction"),
    ("uses Aurora Browser", "has a membership at Ironclad Gym"),
    ("was promoted to engineering lead", "keeps a succulent on their desk"),
    ("writes mostly Solstice-lang", "volunteers at a community garden monthly"),
    ("switched to oat-milk lattes", "is left-handed"),
    ("moved to Lakeside last month", "practices calligraphy as a hobby"),
    ("switched to Summit Fitness", "follows a strict Tuesday gym routine"),
]

# A `coexist`-looking trap that is ACTUALLY a `supersede` (SCHEMA.md's "negative" example for
# coexist): two "lives in X" statements about the SAME slot, phrased independently.
_COEXIST_TRAP_SLOTS: list[tuple[str, str, str]] = [
    ("home_city_trap", "lives in Rivertown", "has recently settled into life in Lakeside"),
    ("employer_trap", "works at Northwind Traders", "has recently started a new role at Contoso Labs"),
]


def _dt(base: datetime, days_offset: int) -> str:
    return (base + timedelta(days=days_offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _case_supersede(idx: int, slot: str, earlier_val: str, later_val: str, base: datetime) -> dict[str, Any]:
    case_id = f"sc-supersede-{idx:04d}-{slot}"
    facts = [
        {"fact_id": "f-earlier", "content": earlier_val, "date": _dt(base, 0)},
        {"fact_id": "f-later", "content": later_val, "date": _dt(base, 120)},
    ]
    pairs = [{"earlier_id": "f-earlier", "later_id": "f-later", "label": "supersede"}]
    current_truth = [{"fact_id": "f-later", "rationale": f"latest {slot} fact, not superseded by any later pair"}]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def _case_coexist(idx: int, val_a: str, val_b: str, base: datetime) -> dict[str, Any]:
    case_id = f"sc-coexist-{idx:04d}"
    facts = [
        {"fact_id": "f-a", "content": val_a, "date": _dt(base, 0)},
        {"fact_id": "f-b", "content": val_b, "date": _dt(base, 30)},
    ]
    pairs = [{"earlier_id": "f-a", "later_id": "f-b", "label": "coexist"}]
    current_truth = [
        {"fact_id": "f-a", "rationale": "independent fact, not updated by any later pair"},
        {"fact_id": "f-b", "rationale": "independent fact, coexists with f-a"},
    ]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def _case_unrelated(idx: int, val_a: str, val_b: str, base: datetime) -> dict[str, Any]:
    case_id = f"sc-unrelated-{idx:04d}"
    facts = [
        {"fact_id": "f-a", "content": val_a, "date": _dt(base, 0)},
        {"fact_id": "f-b", "content": val_b, "date": _dt(base, 45)},
    ]
    pairs = [{"earlier_id": "f-a", "later_id": "f-b", "label": "unrelated"}]
    current_truth = [
        {"fact_id": "f-a", "rationale": "no shared slot with f-b; both stand as independently true"},
        {"fact_id": "f-b", "rationale": "no shared slot with f-a; both stand as independently true"},
    ]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def _case_coexist_trap(idx: int, slot: str, earlier_val: str, later_val: str, base: datetime) -> dict[str, Any]:
    """SCHEMA.md's coexist-negative worked example: surface form suggests two independent
    statements, but they share a slot — the correct label is `supersede`, not `coexist`."""
    case_id = f"sc-coexisttrap-{idx:04d}-{slot}"
    facts = [
        {"fact_id": "f-earlier", "content": earlier_val, "date": _dt(base, 0)},
        {"fact_id": "f-later", "content": later_val, "date": _dt(base, 135)},
    ]
    pairs = [{"earlier_id": "f-earlier", "later_id": "f-later", "label": "supersede"}]
    current_truth = [
        {"fact_id": "f-later", "rationale": f"same {slot} slot as f-earlier despite independent phrasing; later wins"}
    ]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def _case_multi_fact(idx: int, rng: random.Random, base: datetime) -> dict[str, Any]:
    """A larger case (4 facts, 3 slots) exercising a MIX of pair labels in one case, so the
    confusion-matrix scorer sees realistic cross-pair interaction, not just 2-fact cases."""
    case_id = f"sc-multi-{idx:04d}"
    slot_a = rng.choice(_SUPERSEDE_SLOTS)
    coexist_val = rng.choice(_COEXIST_PAIRS)[1]
    unrelated_val = rng.choice(_UNRELATED_PAIRS)[0]
    facts = [
        {"fact_id": "f1", "content": slot_a[1], "date": _dt(base, 0)},
        {"fact_id": "f2", "content": slot_a[2], "date": _dt(base, 90)},
        {"fact_id": "f3", "content": coexist_val, "date": _dt(base, 10)},
        {"fact_id": "f4", "content": unrelated_val, "date": _dt(base, 200)},
    ]
    pairs = [
        {"earlier_id": "f1", "later_id": "f2", "label": "supersede"},
        {"earlier_id": "f1", "later_id": "f3", "label": "unrelated"},
        {"earlier_id": "f3", "later_id": "f4", "label": "unrelated"},
        {"earlier_id": "f2", "later_id": "f4", "label": "unrelated"},
    ]
    current_truth = [
        {"fact_id": "f2", "rationale": "supersedes f1 on the same slot"},
        {"fact_id": "f3", "rationale": "independent fact, no supersede pair involves it as the earlier side of a same-slot update"},
        {"fact_id": "f4", "rationale": "independent fact, unrelated to every other fact in this case"},
    ]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def generate(seed: int = SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    base = datetime(2024, 1, 8, 9, 0, 0, tzinfo=UTC)
    cases: list[dict[str, Any]] = []

    for i, (slot, earlier_val, later_val) in enumerate(_SUPERSEDE_SLOTS):
        cases.append(_case_supersede(i, slot, earlier_val, later_val, base))
    for i, (val_a, val_b) in enumerate(_COEXIST_PAIRS):
        cases.append(_case_coexist(i, val_a, val_b, base))
    for i, (val_a, val_b) in enumerate(_UNRELATED_PAIRS):
        cases.append(_case_unrelated(i, val_a, val_b, base))
    for i, (slot, earlier_val, later_val) in enumerate(_COEXIST_TRAP_SLOTS):
        cases.append(_case_coexist_trap(i, slot, earlier_val, later_val, base))
    # Deterministic "random" multi-fact cases: rng draws happen in a FIXED order over the fixed
    # seed, so regeneration is byte-identical.
    n_multi = 12
    for i in range(n_multi):
        cases.append(_case_multi_fact(i, rng, base))

    cases.sort(key=lambda c: c["case_id"])
    for case in cases:
        case["pairs"].sort(key=lambda p: (p["earlier_id"], p["later_id"]))

    return {"goldset": GOLDSET_NAME, "version": VERSION, "seed": seed, "cases": cases}


def _validate_schema(data: dict[str, Any]) -> list[str]:
    """Minimal dependency-free JSON Schema structural check (no `jsonschema` package dependency —
    eval/requirements.txt stays small). Checks the shape this schema actually constrains: required
    keys, enum membership, additionalProperties=false at each level. Returns a list of error
    strings (empty = valid)."""
    errors: list[str] = []
    if data.get("goldset") != GOLDSET_NAME:
        errors.append(f"goldset must be {GOLDSET_NAME!r}, got {data.get('goldset')!r}")
    if "version" not in data or not isinstance(data["version"], str):
        errors.append("version must be a string")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("cases must be a non-empty array")
        return errors
    top_allowed = {"goldset", "version", "seed", "cases"}
    extra_top = set(data) - top_allowed
    if extra_top:
        errors.append(f"unexpected top-level keys: {sorted(extra_top)}")
    case_allowed = {"case_id", "facts", "pairs", "current_truth"}
    fact_allowed = {"fact_id", "content", "date"}
    pair_allowed = {"earlier_id", "later_id", "label"}
    ct_allowed = {"fact_id", "rationale"}
    seen_case_ids: set[str] = set()
    for case in cases:
        cid = case.get("case_id")
        if not isinstance(cid, str) or not cid:
            errors.append(f"case missing valid case_id: {case!r}")
            continue
        if cid in seen_case_ids:
            errors.append(f"duplicate case_id: {cid}")
        seen_case_ids.add(cid)
        extra = set(case) - case_allowed
        if extra:
            errors.append(f"case {cid}: unexpected keys {sorted(extra)}")
        facts = case.get("facts")
        if not isinstance(facts, list) or not facts:
            errors.append(f"case {cid}: facts must be a non-empty array")
            continue
        fact_ids: set[str] = set()
        for fact in facts:
            fid = fact.get("fact_id")
            if not isinstance(fid, str) or not fid:
                errors.append(f"case {cid}: fact missing valid fact_id: {fact!r}")
                continue
            fact_ids.add(fid)
            extra_f = set(fact) - fact_allowed
            if extra_f:
                errors.append(f"case {cid} fact {fid}: unexpected keys {sorted(extra_f)}")
            for req in ("content", "date"):
                if req not in fact:
                    errors.append(f"case {cid} fact {fid}: missing {req!r}")
        pairs = case.get("pairs")
        if not isinstance(pairs, list):
            errors.append(f"case {cid}: pairs must be an array")
            pairs = []
        for pair in pairs:
            extra_p = set(pair) - pair_allowed
            if extra_p:
                errors.append(f"case {cid}: pair has unexpected keys {sorted(extra_p)}")
            for fk in ("earlier_id", "later_id"):
                if pair.get(fk) not in fact_ids:
                    errors.append(f"case {cid}: pair {fk}={pair.get(fk)!r} not in this case's facts")
            if pair.get("label") not in ("supersede", "coexist", "unrelated"):
                errors.append(f"case {cid}: pair has invalid label {pair.get('label')!r}")
        current_truth = case.get("current_truth")
        if not isinstance(current_truth, list) or not current_truth:
            errors.append(f"case {cid}: current_truth must be a non-empty array")
            continue
        for ct in current_truth:
            extra_ct = set(ct) - ct_allowed
            if extra_ct:
                errors.append(f"case {cid}: current_truth entry has unexpected keys {sorted(extra_ct)}")
            if ct.get("fact_id") not in fact_ids:
                errors.append(f"case {cid}: current_truth fact_id={ct.get('fact_id')!r} not in this case's facts")
            if not ct.get("rationale"):
                errors.append(f"case {cid}: current_truth entry missing rationale")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument(
        "--check",
        action="store_true",
        help="assert the on-disk goldset is schema-valid AND a byte-identical regen from the fixed seed (determinism proof); writes nothing",
    )
    args = ap.parse_args(argv)

    data = generate(args.seed)
    errors = _validate_schema(data)
    if errors:
        print("SCHEMA VALIDATION FAILED:", file=__import__("sys").stderr)
        for e in errors:
            print(f"  - {e}", file=__import__("sys").stderr)
        return 2

    if len(data["cases"]) < 40:
        print(f"FAIL: generated {len(data['cases'])} cases, need >= 40", file=__import__("sys").stderr)
        return 2

    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.out.exists():
            print(f"CHECK FAILED: {args.out} does not exist yet — run without --check first", file=__import__("sys").stderr)
            return 2
        on_disk = args.out.read_text(encoding="utf-8")
        if on_disk != rendered:
            print(f"CHECK FAILED: regeneration differs from {args.out} — determinism broken", file=__import__("sys").stderr)
            return 2
        print(f"CHECK OK: {args.out} is schema-valid and byte-identical to a fresh regen ({len(data['cases'])} cases, seed={args.seed})")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} ({len(data['cases'])} cases, seed={args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
