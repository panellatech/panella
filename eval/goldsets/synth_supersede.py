#!/usr/bin/env python3
"""Generate the supersede confusion-matrix goldset v1 (synthetic, fixed seed, deterministic).

v1 REPLACES v0 in the repo (v0 lives in git history — same seed, same generation approach, just a
much larger and richer template pool). Produces a goldset spanning the three pair labels (supersede
/ coexist / unrelated — see SCHEMA.md), synthetic content ONLY (no real names/data — every
entity/value below is a placeholder), PLUS a high-risk (`high_risk: true`) slice: same-slot
value-changing updates over sensitive attributes (medication, legal name, emergency contact, home-
address-sharing, primary physician, dietary restriction), and high-risk-flavored facts paired with a
later UNRELATED benign fact — the deadliest false-merge trap (recency + sensitivity must not cause a
classifier to merge them). Deterministic: a fixed seed + fixed generation order means a fresh regen
is byte-identical to the committed `supersede_v1.json` (`--check` asserts this, plus schema validity
against `supersede.schema.json`).

Beyond schema validity, the generated goldset must clear a set of hard CONTENT bars (computed from
the generated data, not hand-counted) — see `_check_bars`: total cases >= 90, total pairs >= 300,
unrelated pairs >= 150, supersede pairs >= 70, coexist pairs >= 30, hr supersede pairs >= 12, hr
unrelated pairs >= 20, coexist-trap cases >= 6. A regen that falls under any bar (e.g. a template
list accidentally shrunk) fails loudly (`exit 2`) instead of silently shipping a thinner goldset.

Usage:
  python eval/goldsets/synth_supersede.py --out eval/goldsets/supersede_v1.json
  python eval/goldsets/synth_supersede.py --check   # regen == committed AND schema-valid AND bars-clean
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
VERSION = "v1"
_HERE = Path(__file__).resolve().parent
DEFAULT_OUT = _HERE / "supersede_v1.json"
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
    # --- v1 additions: ~16 new benign slots, drawn from the pre-allocated benign families
    # (vehicle/car model, laptop/tablet model, team name, office building, streaming service,
    # cloud storage provider, internet provider, sport/gym routine, hobby class, note-taking app,
    # keyboard layout, favorite cuisine style). ---
    ("car_model", "drives a Comet Roadster", "upgraded to a Comet Voyager"),
    ("bicycle_model", "rides a Drift Cycles Meridian", "upgraded to a Drift Cycles Apex"),
    ("laptop_model", "codes on a Fenwick Slate 13", "upgraded to a Fenwick Slate 15"),
    ("tablet_model", "sketches on a Halcyon Pad Mini", "upgraded to a Halcyon Pad Pro"),
    ("team_name", "plays for the Emberfall Hawks", "now plays for the Driftwood Otters"),
    ("office_building", "works out of the Meridian Tower office", "moved to the Cascadia Point office"),
    ("video_streaming_service", "streams shows on Lumen Play", "switched to Orbit Screen"),
    ("music_streaming_service", "listens to music on Auria Sound", "switched to Wavelength Audio"),
    ("cloud_storage_provider", "backs up files to Driftbox Cloud", "switched to Cirrus Vault"),
    ("internet_provider", "has home internet through Fenwick Broadband", "switched to Northgate Fiber"),
    ("sport", "plays recreational soccer on weekends", "switched to recreational tennis on weekends"),
    ("workout_routine", "follows a strength-training gym routine", "switched to a running-focused training routine"),
    ("hobby_class", "takes a pottery class on Thursdays", "switched to a woodworking class on Thursdays"),
    ("notetaking_app", "takes notes in Quill Notes", "switched to Lattice Notes"),
    ("keyboard_layout", "types on a QWERTY keyboard layout", "switched to a Dvorak keyboard layout"),
    ("cuisine_style", "cooks mostly Mediterranean-inspired meals", "shifted to mostly Southeast-Asian-inspired meals"),
]

# High-risk slot templates — same tuple shape as `_SUPERSEDE_SLOTS`, but every value is drawn from
# the pre-allocated HIGH-RISK families (food allergy, medication, dietary restriction, legal name,
# emergency contact, home-address-sharing constraint, primary physician). Every generated case's
# pair carries `"high_risk": true` (see `_case_hr_supersede`). Medication/physician names are
# entirely FICTIONAL (e.g. "Veltrazine", "Dr. Naomi Falkirk") — never a real drug or a real person.
_HR_SUPERSEDE_SLOTS: list[tuple[str, str, str]] = [
    ("food_allergy", "manages a mild tree-nut sensitivity flagged by their care team", "was diagnosed with a severe tree-nut allergy after follow-up testing"),
    ("medication", "takes Veltrazine daily for hypertension", "switched to Norvexol last month"),
    ("dietary_restriction", "follows a low-sodium diet ordered by their care team", "moved to a gluten-free diet after a recent diagnosis"),
    ("legal_name", "goes by the legal name Marlowe Kestrel", "legally changed their name to Marlowe Ashgrove last spring"),
    ("emergency_contact", "lists Priya Osgood as their emergency contact", "updated their emergency contact to Devon Achebe last month"),
    ("home_address_sharing", "has a standing rule to never share their home address with anyone who asks", "updated the rule to allow sharing their home address only with verified family members"),
    ("primary_physician", "sees Dr. Naomi Falkirk as their primary physician", "switched to Dr. Osei Bramwell as their primary physician"),
    ("secondary_medication", "takes Halbrivan for a chronic migraine condition", "switched to Quenavir after their neurologist changed the plan"),
    ("religious_dietary_restriction", "keeps halal dietary requirements when dining out", "shifted to kosher dietary requirements after a household change"),
    ("secondary_physician", "sees Dr. Kavita Lindqvist for specialist care", "was referred to Dr. Tobias Renner for specialist care after a practice change"),
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
    # --- v1 additions ---
    ("keeps a small herb garden on the balcony", "enjoys long-distance trail running"),
    ("prefers aisle seats on short flights", "collects retro video game cartridges"),
    ("journals every morning before work", "plays chess online in the evenings"),
    ("is a night owl who works best after 9pm", "volunteers at a local animal shelter twice a month"),
    ("enjoys painting watercolor landscapes", "follows a weekly meal-prep routine on Sundays"),
    ("prefers podcasts over music while commuting", "keeps an aquarium with two goldfish"),
    ("practices yoga twice a week", "reads science fiction novels on weekends"),
    ("prefers a minimalist desk setup", "enjoys birdwatching on weekend hikes"),
    ("collects vinyl records from the 1990s", "attends a monthly board game night"),
    ("is training for a half-marathon", "prefers historical documentaries over dramas"),
    ("grows tomatoes in a backyard garden", "keeps a list of favorite hiking trails"),
    ("prefers text messages over phone calls", "enjoys home-brewing coffee on weekends"),
    ("prefers window seats on long flights", "bakes sourdough bread on weekends"),
    ("keeps a running gratitude list each night", "enjoys building model trains as a hobby"),
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
    # --- v1 additions: cross-matching v1's new benign supersede/coexist values ---
    ("drives a Comet Roadster", "plays for the Emberfall Hawks"),
    ("codes on a Fenwick Slate 13", "works out of the Meridian Tower office"),
    ("streams shows on Lumen Play", "backs up files to Driftbox Cloud"),
    ("listens to music on Auria Sound", "has home internet through Fenwick Broadband"),
    ("plays recreational soccer on weekends", "takes a pottery class on Thursdays"),
    ("takes notes in Quill Notes", "types on a QWERTY keyboard layout"),
    ("cooks mostly Mediterranean-inspired meals", "follows a strength-training gym routine"),
    ("rides a Drift Cycles Meridian", "sketches on a Halcyon Pad Mini"),
    ("now plays for the Driftwood Otters", "keeps a small herb garden on the balcony"),
    ("moved to the Cascadia Point office", "prefers aisle seats on short flights"),
    ("switched to Orbit Screen", "journals every morning before work"),
    ("switched to Cirrus Vault", "is a night owl who works best after 9pm"),
    ("switched to Northgate Fiber", "enjoys painting watercolor landscapes"),
    ("switched to a woodworking class on Thursdays", "prefers podcasts over music while commuting"),
    ("switched to Lattice Notes", "practices yoga twice a week"),
    ("switched to a Dvorak keyboard layout", "prefers a minimalist desk setup"),
    ("shifted to mostly Southeast-Asian-inspired meals", "collects vinyl records from the 1990s"),
    ("switched to a running-focused training routine", "is training for a half-marathon"),
    ("upgraded to a Comet Voyager", "grows tomatoes in a backyard garden"),
    ("upgraded to a Drift Cycles Apex", "prefers text messages over phone calls"),
    ("switched to recreational tennis on weekends", "prefers window seats on long flights"),
    ("upgraded to a Halcyon Pad Pro", "keeps a running gratitude list each night"),
]

# High-risk-flavored `unrelated` pairs — an hr fact (from `_HR_SUPERSEDE_SLOTS`'s families, in
# EITHER its earlier or later phrasing) paired with a later BENIGN fact that shares no slot with it.
# Every case's pair carries `"high_risk": true`. This is the deadliest false-merge trap: recency
# alone (or "this looks sensitive so it must be important/related") must NOT cause a classifier to
# merge a high-risk fact with an unrelated benign one.
_HR_UNRELATED_PAIRS: list[tuple[str, str]] = [
    ("is allergic to shellfish", "switched to the Quill Notes note-taking app"),
    ("takes Veltrazine daily for hypertension", "now plays for the Driftwood Otters"),
    ("follows a low-sodium diet ordered by their care team", "upgraded to a Comet Voyager"),
    ("goes by the legal name Marlowe Kestrel", "streams shows on Lumen Play"),
    ("lists Priya Osgood as their emergency contact", "backs up files to Driftbox Cloud"),
    ("has a standing rule to never share their home address with anyone who asks", "has home internet through Fenwick Broadband"),
    ("sees Dr. Naomi Falkirk as their primary physician", "takes a pottery class on Thursdays"),
    ("takes Halbrivan for a chronic migraine condition", "types on a QWERTY keyboard layout"),
    ("keeps halal dietary requirements when dining out", "cooks mostly Mediterranean-inspired meals"),
    ("was diagnosed with a severe tree-nut allergy after follow-up testing", "codes on a Fenwick Slate 13"),
    ("switched to Norvexol last month", "works out of the Meridian Tower office"),
    ("moved to a gluten-free diet after a recent diagnosis", "sketches on a Halcyon Pad Mini"),
    ("legally changed their name to Marlowe Ashgrove last spring", "listens to music on Auria Sound"),
    ("updated their emergency contact to Devon Achebe last month", "rides a Drift Cycles Meridian"),
    ("updated the rule to allow sharing their home address only with verified family members", "plays recreational soccer on weekends"),
    ("switched to Dr. Osei Bramwell as their primary physician", "follows a strength-training gym routine"),
    ("switched to Quenavir after their neurologist changed the plan", "drives a Comet Roadster"),
    ("shifted to kosher dietary requirements after a household change", "takes notes in Quill Notes"),
    ("carries an epinephrine auto-injector for a peanut allergy", "collects vinyl records from the 1990s"),
    ("takes Sorevastin nightly for a thyroid condition", "prefers a minimalist desk setup"),
    ("is legally known as Elowen Bramhall after a recent name change", "enjoys painting watercolor landscapes"),
    ("relies on Jonas Whitfield as their emergency contact", "grows tomatoes in a backyard garden"),
    ("never shares their home address unless a visit is pre-approved by a family member", "prefers text messages over phone calls"),
    ("sees Dr. Imara Voss for ongoing care as their primary physician", "keeps an aquarium with two goldfish"),
]

# A `coexist`-looking trap that is ACTUALLY a `supersede` (SCHEMA.md's "negative" example for
# coexist): two "lives in X" statements about the SAME slot, phrased independently.
_COEXIST_TRAP_SLOTS: list[tuple[str, str, str]] = [
    ("home_city_trap", "lives in Rivertown", "has recently settled into life in Lakeside"),
    ("employer_trap", "works at Northwind Traders", "has recently started a new role at Contoso Labs"),
    # --- v1 additions ---
    ("car_model_trap", "drives a Comet Roadster", "has recently gotten comfortable behind the wheel of a Comet Voyager"),
    ("team_name_trap", "plays for the Emberfall Hawks", "has recently settled in as a player for the Driftwood Otters"),
    ("streaming_trap", "streams shows on Lumen Play", "has recently gotten hooked on Orbit Screen for their shows"),
    ("notetaking_trap", "takes notes in Quill Notes", "has recently moved all their notes over to Lattice Notes"),
    ("cuisine_trap", "cooks mostly Mediterranean-inspired meals", "has recently been leaning into Southeast-Asian-inspired meals for dinner"),
    ("office_building_trap", "works out of the Meridian Tower office", "has recently gotten settled into the Cascadia Point office day-to-day"),
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


def _case_hr_supersede(idx: int, slot: str, earlier_val: str, later_val: str, base: datetime) -> dict[str, Any]:
    """Same shape as `_case_supersede`, but the pair carries `"high_risk": true` — a genuine
    same-slot value-changing update over a sensitive attribute (medication, legal name, emergency
    contact, home-address-sharing, primary physician, dietary restriction)."""
    case_id = f"sc-hrsupersede-{idx:04d}-{slot}"
    facts = [
        {"fact_id": "f-earlier", "content": earlier_val, "date": _dt(base, 0)},
        {"fact_id": "f-later", "content": later_val, "date": _dt(base, 120)},
    ]
    pairs = [{"earlier_id": "f-earlier", "later_id": "f-later", "label": "supersede", "high_risk": True}]
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


def _case_hr_unrelated(idx: int, val_a: str, val_b: str, base: datetime) -> dict[str, Any]:
    """Same shape as `_case_unrelated`, but the pair carries `"high_risk": true` — an hr-flavored
    fact paired with a later unrelated BENIGN fact. The deadliest false-merge trap this goldset
    carries: recency + sensitivity must not cause a classifier to merge them."""
    case_id = f"sc-hrunrelated-{idx:04d}"
    facts = [
        {"fact_id": "f-a", "content": val_a, "date": _dt(base, 0)},
        {"fact_id": "f-b", "content": val_b, "date": _dt(base, 45)},
    ]
    pairs = [{"earlier_id": "f-a", "later_id": "f-b", "label": "unrelated", "high_risk": True}]
    current_truth = [
        {
            "fact_id": "f-a",
            "rationale": "no shared slot with f-b; both stand as independently true (high-risk value must not be merged on recency alone)",
        },
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
    """A larger case (4-6 facts, mixed slots) exercising a MIX of pair labels in one case, so the
    confusion-matrix scorer sees realistic cross-pair interaction, not just 2-fact cases. On EVEN
    idx (a deterministic, fixed property of the loop counter — not an extra source of randomness)
    the case also folds in a genuine `coexist` pair (f5/f3, drawn from the SAME `_COEXIST_PAIRS`
    tuple, matching how `_case_coexist` itself defines a coexist pair) so the scorer sees coexist
    interacting with supersede/unrelated inside a single multi-fact case, not only in isolation."""
    case_id = f"sc-multi-{idx:04d}"
    slot_a = rng.choice(_SUPERSEDE_SLOTS)
    coexist_a, coexist_b = rng.choice(_COEXIST_PAIRS)
    unrelated_val = rng.choice(_UNRELATED_PAIRS)[0]
    facts = [
        {"fact_id": "f1", "content": slot_a[1], "date": _dt(base, 0)},
        {"fact_id": "f2", "content": slot_a[2], "date": _dt(base, 90)},
        {"fact_id": "f3", "content": coexist_b, "date": _dt(base, 10)},
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
    if idx % 2 == 0:
        facts.append({"fact_id": "f5", "content": coexist_a, "date": _dt(base, 5)})
        pairs.append({"earlier_id": "f5", "later_id": "f3", "label": "coexist"})
        pairs.append({"earlier_id": "f1", "later_id": "f5", "label": "unrelated"})
        pairs.append({"earlier_id": "f5", "later_id": "f4", "label": "unrelated"})
        current_truth.append({"fact_id": "f5", "rationale": "independent fact, coexists with f3"})
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def _case_hr_multi_fact(idx: int, rng: random.Random, base: datetime) -> dict[str, Any]:
    """A multi-fact case mixing ONE high-risk supersede pair with unrelated pairs — the deadliest
    shape for a downstream classifier: a sensitive same-slot update sitting alongside unrelated
    benign noise in the SAME case, not in isolation."""
    case_id = f"sc-hrmulti-{idx:04d}"
    hr_slot = rng.choice(_HR_SUPERSEDE_SLOTS)
    unrelated_a, unrelated_b = rng.choice(_UNRELATED_PAIRS)
    facts = [
        {"fact_id": "f1", "content": hr_slot[1], "date": _dt(base, 0)},
        {"fact_id": "f2", "content": hr_slot[2], "date": _dt(base, 90)},
        {"fact_id": "f3", "content": unrelated_a, "date": _dt(base, 15)},
        {"fact_id": "f4", "content": unrelated_b, "date": _dt(base, 210)},
    ]
    pairs = [
        {"earlier_id": "f1", "later_id": "f2", "label": "supersede", "high_risk": True},
        {"earlier_id": "f1", "later_id": "f3", "label": "unrelated"},
        {"earlier_id": "f3", "later_id": "f4", "label": "unrelated"},
        {"earlier_id": "f2", "later_id": "f4", "label": "unrelated"},
    ]
    current_truth = [
        {"fact_id": "f2", "rationale": f"latest {hr_slot[0]} fact, not superseded by any later pair"},
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
    for i, (slot, earlier_val, later_val) in enumerate(_HR_SUPERSEDE_SLOTS):
        cases.append(_case_hr_supersede(i, slot, earlier_val, later_val, base))
    for i, (val_a, val_b) in enumerate(_COEXIST_PAIRS):
        cases.append(_case_coexist(i, val_a, val_b, base))
    for i, (val_a, val_b) in enumerate(_UNRELATED_PAIRS):
        cases.append(_case_unrelated(i, val_a, val_b, base))
    for i, (val_a, val_b) in enumerate(_HR_UNRELATED_PAIRS):
        cases.append(_case_hr_unrelated(i, val_a, val_b, base))
    for i, (slot, earlier_val, later_val) in enumerate(_COEXIST_TRAP_SLOTS):
        cases.append(_case_coexist_trap(i, slot, earlier_val, later_val, base))
    # Deterministic "random" multi-fact cases: rng draws happen in a FIXED order over the fixed
    # seed, so regeneration is byte-identical.
    n_multi = 30
    for i in range(n_multi):
        cases.append(_case_multi_fact(i, rng, base))
    n_hrmulti = 6
    for i in range(n_hrmulti):
        cases.append(_case_hr_multi_fact(i, rng, base))

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
    pair_allowed = {"earlier_id", "later_id", "label", "high_risk"}
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
            if "high_risk" in pair and not isinstance(pair["high_risk"], bool):
                errors.append(f"case {cid}: pair high_risk must be a bool, got {pair['high_risk']!r}")
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


def _check_bars(data: dict[str, Any]) -> list[str]:
    """Hard CONTENT bars (beyond schema validity) the goldset must clear — computed from the
    GENERATED data, not hand-counted, so a template edit that shrinks coverage fails loudly instead
    of silently shipping a thinner goldset. Returns a list of failure messages (empty = all bars
    cleared)."""
    cases = data.get("cases", [])
    all_pairs = [p for c in cases for p in c["pairs"]]
    by_label: dict[str, list[dict[str, Any]]] = {"supersede": [], "coexist": [], "unrelated": []}
    for p in all_pairs:
        by_label[p["label"]].append(p)
    hr_supersede = [p for p in by_label["supersede"] if p.get("high_risk") is True]
    hr_unrelated = [p for p in by_label["unrelated"] if p.get("high_risk") is True]
    coexist_trap_cases = [c for c in cases if c["case_id"].startswith("sc-coexisttrap-")]

    bars = [
        ("total cases", len(cases), 90),
        ("total pairs", len(all_pairs), 300),
        ("unrelated pairs", len(by_label["unrelated"]), 150),
        ("supersede pairs", len(by_label["supersede"]), 70),
        ("coexist pairs", len(by_label["coexist"]), 30),
        ("hr supersede pairs", len(hr_supersede), 12),
        ("hr unrelated pairs", len(hr_unrelated), 20),
        ("coexist-trap cases", len(coexist_trap_cases), 6),
    ]
    return [f"{name}: got {got}, need >= {need}" for name, got, need in bars if got < need]


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

    bar_errors = _check_bars(data)
    if bar_errors:
        print("CONTENT BAR FAILURES:", file=__import__("sys").stderr)
        for e in bar_errors:
            print(f"  - {e}", file=__import__("sys").stderr)
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
        print(f"CHECK OK: {args.out} is schema-valid, content-bars-clean, and byte-identical to a fresh regen ({len(data['cases'])} cases, seed={args.seed})")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} ({len(data['cases'])} cases, seed={args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
