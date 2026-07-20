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

ASPECT DISJOINTNESS (generator-enforced, not vibes): every template value carries a life-domain
`aspect` tag from the closed `_ASPECTS` vocabulary (update-slot templates tag the slot; freeform
pair-halves are tagged in `_FREEFORM_META`; the union is the exported `CONTENT_META` map,
content -> (slot, aspect)). An `unrelated` label claims the two facts share NO slot or subject —
that claim is only defensible when the facts also live in different life domains, so the multi-fact
generators reject-and-redraw any draw that would label two same-aspect (or same-slot, or
same-content) facts `unrelated` (redraw = the next rng draw in fixed order, bounded attempts, then
a deterministic linear probe over the pool). After generation `_check_aspect_disjointness` sweeps
the ENTIRE goldset — standalone AND multi cases — asserting every `unrelated` pair joins two facts
with different contents AND different source slots AND different aspects, every `coexist` pair two
different slots, and every `supersede` pair one shared slot; any violation exits 2. Coexist pairs
MAY share an aspect (that is what coexist means); unrelated pairs MUST NOT.

Beyond schema validity, the generated goldset must also clear a set of hard CONTENT bars (computed
from the generated data, not hand-counted) — see `_check_bars`: total cases >= 90, total pairs >=
300, unrelated pairs >= 150, supersede pairs >= 70, coexist pairs >= 30, hr supersede pairs >= 12,
hr unrelated pairs >= 20, coexist-trap cases >= 6. A regen that falls under any bar (e.g. a template
list accidentally shrunk) fails loudly (`exit 2`) instead of silently shipping a thinner goldset.

Usage:
  python eval/goldsets/synth_supersede.py --out eval/goldsets/supersede_v1.json
  python eval/goldsets/synth_supersede.py --check   # regen == committed AND schema/bars/disjointness-clean
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

SEED = 20260707
GOLDSET_NAME = "panella-supersede-confusion-matrix"
VERSION = "v1"
_HERE = Path(__file__).resolve().parent
DEFAULT_OUT = _HERE / "supersede_v1.json"
SCHEMA_PATH = _HERE / "supersede.schema.json"

# Closed aspect vocabulary (chief-fixed). A tag names the LIFE DOMAIN a fact lives in; two facts
# sharing a tag are same-cluster and must never be labeled `unrelated` with each other. Coarse
# tagging errs SAFE (an over-shared tag only narrows which template combinations the multi
# generators may pair as unrelated); fine-splitting errs DANGEROUS (a same-cluster pair slips
# through as `unrelated`). `sport` deliberately covers the whole athletic/games-played cluster
# (gym routines, team sports, racket sports, yoga, running, chess-as-mind-sport) — splitting a
# separate fitness tag out of it would let "Tuesday gym routine x switched to tennis" through,
# a confirmed blind-judge finding. Not every tag must be used (`fitness` and `reading` are
# currently absorbed by `sport` and `media` for exactly that clustering reason).
_ASPECTS = frozenset(
    {
        "work", "home", "location", "transport", "devices", "tech_tools", "services", "media",
        "music", "reading", "fitness", "sport", "hobby", "dining", "beverage", "social", "routine",
        "health", "identity", "contacts", "safety",
    }
)


class UpdateTemplate(NamedTuple):
    """A same-slot value-changing update template: `earlier` -> `later` on `slot`, whose values
    both live in the `aspect` life domain (same slot => same aspect by construction)."""

    slot: str
    aspect: str
    earlier: str
    later: str


# Synthetic slot templates. Every value here is a placeholder (fictional company/city/product
# names), never real personal data. Each template generates one `supersede` case: two facts for the
# SAME slot, later supersedes earlier.
_SUPERSEDE_SLOTS: list[UpdateTemplate] = [
    UpdateTemplate("employer", "work", "works at Northwind Traders", "now works at Contoso Labs"),
    UpdateTemplate("home_city", "location", "lives in Rivertown", "moved to Lakeside last month"),
    UpdateTemplate("code_editor", "tech_tools", "codes in Nimbus Editor", "switched to Vertex IDE"),
    UpdateTemplate("programming_language", "tech_tools", "writes mostly Solstice-lang", "writes mostly Umbra-lang now"),
    UpdateTemplate("coffee_style", "beverage", "drinks black coffee, no sugar", "switched to oat-milk lattes"),
    UpdateTemplate("job_title", "work", "works as a systems analyst", "was promoted to engineering lead"),
    UpdateTemplate("phone_model", "devices", "carries a Solstice X12", "upgraded to a Solstice X14"),
    UpdateTemplate("commute_mode", "transport", "commutes by bicycle", "now commutes by train"),
    UpdateTemplate("primary_browser", "tech_tools", "uses Aurora Browser", "switched to Meridian Browser"),
    UpdateTemplate("gym_membership", "sport", "has a membership at Ironclad Gym", "switched to Summit Fitness"),
    # --- v1 additions: ~16 new benign slots, drawn from the pre-allocated benign families
    # (vehicle/car model, laptop/tablet model, team name, office building, streaming service,
    # cloud storage provider, internet provider, sport/gym routine, hobby class, note-taking app,
    # keyboard layout, favorite cuisine style). ---
    UpdateTemplate("car_model", "transport", "drives a Comet Roadster", "upgraded to a Comet Voyager"),
    UpdateTemplate("bicycle_model", "transport", "rides a Drift Cycles Meridian", "upgraded to a Drift Cycles Apex"),
    UpdateTemplate("laptop_model", "devices", "codes on a Fenwick Slate 13", "upgraded to a Fenwick Slate 15"),
    UpdateTemplate("tablet_model", "devices", "sketches on a Halcyon Pad Mini", "upgraded to a Halcyon Pad Pro"),
    UpdateTemplate("team_name", "sport", "plays for the Emberfall Hawks", "now plays for the Driftwood Otters"),
    UpdateTemplate("office_building", "work", "works out of the Meridian Tower office", "moved to the Cascadia Point office"),
    UpdateTemplate("video_streaming_service", "media", "streams shows on Lumen Play", "switched to Orbit Screen"),
    UpdateTemplate("music_streaming_service", "music", "listens to music on Auria Sound", "switched to Wavelength Audio"),
    UpdateTemplate("cloud_storage_provider", "services", "backs up files to Driftbox Cloud", "switched to Cirrus Vault"),
    UpdateTemplate("internet_provider", "services", "has home internet through Fenwick Broadband", "switched to Northgate Fiber"),
    UpdateTemplate("sport", "sport", "plays recreational soccer on weekends", "switched to recreational tennis on weekends"),
    UpdateTemplate("workout_routine", "sport", "follows a strength-training gym routine", "switched to a running-focused training routine"),
    UpdateTemplate("hobby_class", "hobby", "takes a pottery class on Thursdays", "switched to a woodworking class on Thursdays"),
    UpdateTemplate("notetaking_app", "tech_tools", "takes notes in Quill Notes", "switched to Lattice Notes"),
    UpdateTemplate("keyboard_layout", "devices", "types on a QWERTY keyboard layout", "switched to a Dvorak keyboard layout"),
    UpdateTemplate("cuisine_style", "dining", "cooks mostly Mediterranean-inspired meals", "shifted to mostly Southeast-Asian-inspired meals"),
]

# High-risk slot templates — same shape, but every value is drawn from the pre-allocated HIGH-RISK
# families (food allergy, medication, dietary restriction, legal name, emergency contact,
# home-address-sharing constraint, primary physician). Every generated case's pair carries
# `"high_risk": true` (see `_case_hr_supersede`). Medication/physician names are entirely FICTIONAL
# (e.g. "Veltrazine", "Dr. Naomi Falkirk") — never a real drug or a real person.
_HR_SUPERSEDE_SLOTS: list[UpdateTemplate] = [
    UpdateTemplate("food_allergy", "health", "manages a mild tree-nut sensitivity flagged by their care team", "was diagnosed with a severe tree-nut allergy after follow-up testing"),
    UpdateTemplate("medication", "health", "takes Veltrazine daily for hypertension", "switched to Norvexol last month"),
    UpdateTemplate("dietary_restriction", "health", "follows a low-sodium diet ordered by their care team", "moved to a gluten-free diet after a recent diagnosis"),
    UpdateTemplate("legal_name", "identity", "goes by the legal name Marlowe Kestrel", "legally changed their name to Marlowe Ashgrove last spring"),
    UpdateTemplate("emergency_contact", "contacts", "lists Priya Osgood as their emergency contact", "updated their emergency contact to Devon Achebe last month"),
    UpdateTemplate("home_address_sharing", "safety", "has a standing rule to never share their home address with anyone who asks", "updated the rule to allow sharing their home address only with verified family members"),
    UpdateTemplate("primary_physician", "health", "sees Dr. Naomi Falkirk as their primary physician", "switched to Dr. Osei Bramwell as their primary physician"),
    UpdateTemplate("secondary_medication", "health", "takes Halbrivan for a chronic migraine condition", "switched to Quenavir after their neurologist changed the plan"),
    UpdateTemplate("religious_dietary_restriction", "dining", "keeps halal dietary requirements when dining out", "shifted to kosher dietary requirements after a household change"),
    UpdateTemplate("secondary_physician", "health", "sees Dr. Kavita Lindqvist for specialist care", "was referred to Dr. Tobias Renner for specialist care after a practice change"),
]

# Synthetic independent-fact templates for `coexist` pairs — two facts about DIFFERENT slots that
# both remain true at once (never update one another). Halves MAY share an aspect (coexist is the
# related-aspects label — see SCHEMA.md's coexist-vs-unrelated discriminator) but MUST have
# different slots.
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

# Synthetic unrelated-fact templates for `unrelated` pairs — two facts sharing NO slot/subject AND
# no aspect (`_check_aspect_disjointness` mechanically enforces all three of content/slot/aspect
# disjointness over every one of these pairs). Deliberately overlapping in surface form with the
# supersede slots above (a naive recency-only classifier would be tempted to merge these; that is
# exactly the trap).
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
    ("switched to Summit Fitness", "enjoys documentary films"),
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
    ("switched to a running-focused training routine", "prefers historical documentaries over dramas"),
    ("upgraded to a Comet Voyager", "grows tomatoes in a backyard garden"),
    ("upgraded to a Drift Cycles Apex", "prefers text messages over phone calls"),
    ("switched to recreational tennis on weekends", "prefers window seats on long flights"),
    ("upgraded to a Halcyon Pad Pro", "keeps a running gratitude list each night"),
]

# High-risk-flavored `unrelated` pairs — an hr fact (from `_HR_SUPERSEDE_SLOTS`'s families, in
# EITHER its earlier or later phrasing) paired with a later BENIGN fact that shares no slot/aspect
# with it. Every case's pair carries `"high_risk": true`. This is the deadliest false-merge trap:
# recency alone (or "this looks sensitive so it must be important/related") must NOT cause a
# classifier to merge a high-risk fact with an unrelated benign one.
_HR_UNRELATED_PAIRS: list[tuple[str, str]] = [
    ("is allergic to shellfish", "switched to the Quill Notes note-taking app"),
    ("takes Veltrazine daily for hypertension", "now plays for the Driftwood Otters"),
    ("follows a low-sodium diet ordered by their care team", "upgraded to a Comet Voyager"),
    ("goes by the legal name Marlowe Kestrel", "streams shows on Lumen Play"),
    ("lists Priya Osgood as their emergency contact", "backs up files to Driftbox Cloud"),
    ("has a standing rule to never share their home address with anyone who asks", "has home internet through Fenwick Broadband"),
    ("sees Dr. Naomi Falkirk as their primary physician", "takes a pottery class on Thursdays"),
    ("takes Halbrivan for a chronic migraine condition", "types on a QWERTY keyboard layout"),
    ("keeps halal dietary requirements when dining out", "collects retro video game cartridges"),
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
# coexist): two statements about the SAME slot, phrased independently. `slot` holds the BASE slot
# name (identical to the matching `_SUPERSEDE_SLOTS` entry, so `CONTENT_META` stays consistent for
# the earlier-values the two pools share); the case_id appends a `_trap` suffix.
_COEXIST_TRAP_SLOTS: list[UpdateTemplate] = [
    UpdateTemplate("home_city", "location", "lives in Rivertown", "has recently settled into life in Lakeside"),
    UpdateTemplate("employer", "work", "works at Northwind Traders", "has recently started a new role at Contoso Labs"),
    # --- v1 additions ---
    UpdateTemplate("car_model", "transport", "drives a Comet Roadster", "has recently gotten comfortable behind the wheel of a Comet Voyager"),
    UpdateTemplate("team_name", "sport", "plays for the Emberfall Hawks", "has recently settled in as a player for the Driftwood Otters"),
    UpdateTemplate("video_streaming_service", "media", "streams shows on Lumen Play", "has recently gotten hooked on Orbit Screen for their shows"),
    UpdateTemplate("notetaking_app", "tech_tools", "takes notes in Quill Notes", "has recently moved all their notes over to Lattice Notes"),
    UpdateTemplate("cuisine_style", "dining", "cooks mostly Mediterranean-inspired meals", "has recently been leaning into Southeast-Asian-inspired meals for dinner"),
    UpdateTemplate("office_building", "work", "works out of the Meridian Tower office", "has recently gotten settled into the Cascadia Point office day-to-day"),
]

# (slot, aspect) for every pair-pool half that is NOT already an update-template value (those
# register from their templates). Slot names are the SEMANTIC slot: two values describing the same
# real-world slot share the name even across pools (e.g. "prefers the Nimbus code editor" IS the
# code_editor slot in different words), so the multi-fact same-slot rejection sees through
# paraphrase. Distinct facts may share a slot name only when they can never be labeled together
# (the import-time build + `_check_aspect_disjointness` catch a violation mechanically).
_FREEFORM_META: dict[str, tuple[str, str]] = {
    # coexist-pool halves
    "avoids gluten in their diet": ("gluten_avoidance", "dining"),
    "enjoys ambient electronic music": ("music_genre", "music"),
    "prefers window seats when traveling": ("seat_preference", "transport"),
    "keeps a succulent on their desk": ("desk_plant", "home"),
    "reads mostly historical fiction": ("reading_genre", "media"),
    "plays badminton on weekends": ("badminton", "sport"),
    "dislikes spicy food": ("spice_tolerance", "dining"),
    "collects vintage postage stamps": ("stamp_collection", "hobby"),
    "works a hybrid schedule": ("work_schedule", "work"),
    "volunteers at a community garden monthly": ("volunteering", "social"),
    "uses a standing desk": ("desk_setup", "home"),
    "subscribes to a weekly astronomy newsletter": ("newsletter_subscription", "media"),
    "prefers tea over coffee in the evening": ("evening_drink", "beverage"),
    "practices calligraphy as a hobby": ("calligraphy", "hobby"),
    "keeps a bullet journal": ("journaling_method", "routine"),
    "follows a strict Tuesday gym routine": ("gym_schedule", "sport"),
    "is left-handed": ("handedness", "identity"),
    "enjoys documentary films": ("documentary_preference", "media"),
    "has two houseplants named after constellations": ("houseplants", "home"),
    "bikes to the farmers market on Saturdays": ("market_trip", "transport"),
    "keeps a small herb garden on the balcony": ("herb_garden", "hobby"),
    "enjoys long-distance trail running": ("trail_running", "sport"),
    "prefers aisle seats on short flights": ("seat_preference", "transport"),
    "collects retro video game cartridges": ("game_cartridge_collection", "hobby"),
    "journals every morning before work": ("journaling_habit", "routine"),
    "plays chess online in the evenings": ("chess", "sport"),
    "is a night owl who works best after 9pm": ("chronotype", "routine"),
    "volunteers at a local animal shelter twice a month": ("volunteering", "social"),
    "enjoys painting watercolor landscapes": ("painting", "hobby"),
    "follows a weekly meal-prep routine on Sundays": ("meal_prep", "dining"),
    "prefers podcasts over music while commuting": ("podcast_preference", "media"),
    "keeps an aquarium with two goldfish": ("aquarium_keeping", "hobby"),
    "practices yoga twice a week": ("yoga", "sport"),
    "reads science fiction novels on weekends": ("reading_genre", "media"),
    "prefers a minimalist desk setup": ("desk_setup", "home"),
    "enjoys birdwatching on weekend hikes": ("birdwatching", "hobby"),
    "collects vinyl records from the 1990s": ("vinyl_collection", "music"),
    "attends a monthly board game night": ("board_game_night", "social"),
    "is training for a half-marathon": ("half_marathon_training", "sport"),
    "prefers historical documentaries over dramas": ("documentary_preference", "media"),
    "grows tomatoes in a backyard garden": ("vegetable_garden", "hobby"),
    "keeps a list of favorite hiking trails": ("hiking_trails", "hobby"),
    "prefers text messages over phone calls": ("communication_preference", "social"),
    "enjoys home-brewing coffee on weekends": ("coffee_brewing", "beverage"),
    "prefers window seats on long flights": ("seat_preference", "transport"),
    "bakes sourdough bread on weekends": ("baking", "dining"),
    "keeps a running gratitude list each night": ("gratitude_practice", "routine"),
    "enjoys building model trains as a hobby": ("model_trains", "hobby"),
    # unrelated-pool halves not covered above / by update templates
    "prefers the Nimbus code editor": ("code_editor", "tech_tools"),
    # hr-unrelated-pool halves not covered by hr update templates
    "is allergic to shellfish": ("food_allergy", "health"),
    "carries an epinephrine auto-injector for a peanut allergy": ("food_allergy", "health"),
    "takes Sorevastin nightly for a thyroid condition": ("medication", "health"),
    "is legally known as Elowen Bramhall after a recent name change": ("legal_name", "identity"),
    "relies on Jonas Whitfield as their emergency contact": ("emergency_contact", "contacts"),
    "never shares their home address unless a visit is pre-approved by a family member": ("home_address_sharing", "safety"),
    "sees Dr. Imara Voss for ongoing care as their primary physician": ("primary_physician", "health"),
    "switched to the Quill Notes note-taking app": ("notetaking_app", "tech_tools"),
}


def _build_content_meta() -> dict[str, tuple[str, str]]:
    """content -> (slot, aspect) over EVERY template value in every pool. Raises at import on a
    conflicting registration (the same content string claiming two different slots/aspects), an
    out-of-vocabulary aspect, or a pair-pool half with no registration — so an untagged or
    inconsistently-tagged template can never silently generate."""
    meta: dict[str, tuple[str, str]] = {}

    def put(content: str, slot: str, aspect: str) -> None:
        if aspect not in _ASPECTS:
            raise ValueError(f"aspect {aspect!r} for {content!r} is outside the closed _ASPECTS vocabulary")
        prev = meta.get(content)
        if prev is not None and prev != (slot, aspect):
            raise ValueError(f"conflicting meta for {content!r}: {prev} vs {(slot, aspect)}")
        meta[content] = (slot, aspect)

    for t in (*_SUPERSEDE_SLOTS, *_HR_SUPERSEDE_SLOTS, *_COEXIST_TRAP_SLOTS):
        put(t.earlier, t.slot, t.aspect)
        put(t.later, t.slot, t.aspect)
    for content, (slot, aspect) in _FREEFORM_META.items():
        put(content, slot, aspect)
    for a, b in (*_COEXIST_PAIRS, *_UNRELATED_PAIRS, *_HR_UNRELATED_PAIRS):
        for txt in (a, b):
            if txt not in meta:
                raise ValueError(f"pair-pool value has no (slot, aspect) registration: {txt!r}")
    return meta


# The exported aspect map (content -> (slot, aspect)); `_check_aspect_disjointness` and the tests
# both recompute the relatedness invariants from it.
CONTENT_META: dict[str, tuple[str, str]] = _build_content_meta()


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
    case_id = f"sc-coexisttrap-{idx:04d}-{slot}_trap"
    facts = [
        {"fact_id": "f-earlier", "content": earlier_val, "date": _dt(base, 0)},
        {"fact_id": "f-later", "content": later_val, "date": _dt(base, 135)},
    ]
    pairs = [{"earlier_id": "f-earlier", "later_id": "f-later", "label": "supersede"}]
    current_truth = [
        {"fact_id": "f-later", "rationale": f"same {slot} slot as f-earlier despite independent phrasing; later wins"}
    ]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


# Bounded rng attempts per constrained draw before the deterministic linear-probe fallback.
_MAX_RNG_ATTEMPTS = 6


def _draw(rng: random.Random, pool: list, accept) -> Any:
    """Deterministic constrained draw: up to `_MAX_RNG_ATTEMPTS` rng index draws (each rejection
    consumes the NEXT rng value in fixed order), then a deterministic linear probe from the last
    drawn index (so an unlucky run of draws cannot spin unboundedly and the result stays a pure
    function of seed + pools). Raises RuntimeError when NO pool entry is acceptable — a
    template-pool curation bug that must fail generation loudly (main() maps it to exit 2)."""
    idx = 0
    for _ in range(_MAX_RNG_ATTEMPTS):
        idx = rng.randrange(len(pool))
        if accept(pool[idx]):
            return pool[idx]
    for step in range(1, len(pool) + 1):
        cand = pool[(idx + step) % len(pool)]
        if accept(cand):
            return cand
    raise RuntimeError(
        "no acceptable template in pool after bounded redraw — the template pools are too narrow "
        "for the aspect/slot/content disjointness constraints; widen the pools"
    )


def _case_multi_fact(idx: int, rng: random.Random, base: datetime) -> dict[str, Any]:
    """A larger case (4-6 facts, mixed slots) exercising a MIX of pair labels in one case, so the
    confusion-matrix scorer sees realistic cross-pair interaction, not just 2-fact cases. On EVEN
    idx (a deterministic, fixed property of the loop counter — not an extra source of randomness)
    the case also folds in a genuine `coexist` pair (f5/f3, drawn from the SAME `_COEXIST_PAIRS`
    tuple, matching how `_case_coexist` itself defines a coexist pair).

    Cross-pool draws are CONSTRAINED (see `_draw`): a candidate is rejected while its content
    equals any already-drawn fact's content, its source slot equals any already-drawn fact's slot,
    or its aspect equals the aspect of any already-drawn fact it will be labeled `unrelated` with —
    so no same-cluster (or verbatim-duplicate) pair can ever be labeled `unrelated` here. The f5/f3
    coexist pair may share an aspect (coexist is the related-aspects label) but never a slot."""
    case_id = f"sc-multi-{idx:04d}"
    include_coexist = idx % 2 == 0
    slot_t = _SUPERSEDE_SLOTS[rng.randrange(len(_SUPERSEDE_SLOTS))]

    def accept_coexist(pair: tuple[str, str]) -> bool:
        a_txt, b_txt = pair
        b_slot, b_aspect = CONTENT_META[b_txt]
        # f3 = b_txt: content/slot-unique vs f1/f2, aspect-disjoint from them (pair (f1,f3) is
        # labeled unrelated; f2 shares f1's slot template, so one aspect check covers both).
        if b_txt in (slot_t.earlier, slot_t.later) or b_slot == slot_t.slot or b_aspect == slot_t.aspect:
            return False
        if include_coexist:
            a_slot, a_aspect = CONTENT_META[a_txt]
            # f5 = a_txt: content/slot-unique vs f1/f2 AND vs f3 (the (f5,f3) coexist pair needs
            # different slots), aspect-disjoint from f1 (pair (f1,f5) is labeled unrelated). f5's
            # aspect MAY equal f3's — that pair is labeled coexist.
            if a_txt in (slot_t.earlier, slot_t.later, b_txt):
                return False
            if a_slot in (slot_t.slot, b_slot) or a_aspect == slot_t.aspect:
                return False
        return True

    coexist_a, coexist_b = _draw(rng, _COEXIST_PAIRS, accept_coexist)
    drawn_contents = {slot_t.earlier, slot_t.later, coexist_b}
    drawn_slots = {slot_t.slot, CONTENT_META[coexist_b][0]}
    blocked_aspects = {slot_t.aspect, CONTENT_META[coexist_b][1]}  # pairs (f2,f4) and (f3,f4)
    if include_coexist:
        drawn_contents.add(coexist_a)
        drawn_slots.add(CONTENT_META[coexist_a][0])
        blocked_aspects.add(CONTENT_META[coexist_a][1])  # pair (f5,f4)

    def accept_unrelated(pair: tuple[str, str]) -> bool:
        v = pair[0]
        v_slot, v_aspect = CONTENT_META[v]
        return v not in drawn_contents and v_slot not in drawn_slots and v_aspect not in blocked_aspects

    unrelated_val = _draw(rng, _UNRELATED_PAIRS, accept_unrelated)[0]

    facts = [
        {"fact_id": "f1", "content": slot_t.earlier, "date": _dt(base, 0)},
        {"fact_id": "f2", "content": slot_t.later, "date": _dt(base, 90)},
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
    if include_coexist:
        facts.append({"fact_id": "f5", "content": coexist_a, "date": _dt(base, 5)})
        pairs.append({"earlier_id": "f5", "later_id": "f3", "label": "coexist"})
        pairs.append({"earlier_id": "f1", "later_id": "f5", "label": "unrelated"})
        pairs.append({"earlier_id": "f5", "later_id": "f4", "label": "unrelated"})
        current_truth.append({"fact_id": "f5", "rationale": "independent fact, coexists with f3"})
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def _case_hr_multi_fact(idx: int, rng: random.Random, base: datetime) -> dict[str, Any]:
    """A multi-fact case mixing ONE high-risk supersede pair with unrelated pairs — the deadliest
    shape for a downstream classifier: a sensitive same-slot update sitting alongside unrelated
    benign noise in the SAME case, not in isolation. The benign f3/f4 tuple is drawn under the same
    disjointness constraints as `_case_multi_fact`: content/slot-unique vs the hr facts,
    aspect-disjoint from the hr slot (pairs (f1,f3)/(f2,f4) are labeled unrelated), and internally
    content/slot/aspect-disjoint (pair (f3,f4) is labeled unrelated too)."""
    case_id = f"sc-hrmulti-{idx:04d}"
    hr_t = _HR_SUPERSEDE_SLOTS[rng.randrange(len(_HR_SUPERSEDE_SLOTS))]

    def accept_tuple(pair: tuple[str, str]) -> bool:
        a_txt, b_txt = pair
        a_slot, a_aspect = CONTENT_META[a_txt]
        b_slot, b_aspect = CONTENT_META[b_txt]
        if a_txt in (hr_t.earlier, hr_t.later) or b_txt in (hr_t.earlier, hr_t.later) or a_txt == b_txt:
            return False
        if hr_t.slot in (a_slot, b_slot) or a_slot == b_slot:
            return False
        return hr_t.aspect not in (a_aspect, b_aspect) and a_aspect != b_aspect

    unrelated_a, unrelated_b = _draw(rng, _UNRELATED_PAIRS, accept_tuple)
    facts = [
        {"fact_id": "f1", "content": hr_t.earlier, "date": _dt(base, 0)},
        {"fact_id": "f2", "content": hr_t.later, "date": _dt(base, 90)},
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
        {"fact_id": "f2", "rationale": f"latest {hr_t.slot} fact, not superseded by any later pair"},
        {"fact_id": "f3", "rationale": "independent fact, no supersede pair involves it as the earlier side of a same-slot update"},
        {"fact_id": "f4", "rationale": "independent fact, unrelated to every other fact in this case"},
    ]
    return {"case_id": case_id, "facts": facts, "pairs": pairs, "current_truth": current_truth}


def generate(seed: int = SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    base = datetime(2024, 1, 8, 9, 0, 0, tzinfo=UTC)
    cases: list[dict[str, Any]] = []

    for i, t in enumerate(_SUPERSEDE_SLOTS):
        cases.append(_case_supersede(i, t.slot, t.earlier, t.later, base))
    for i, t in enumerate(_HR_SUPERSEDE_SLOTS):
        cases.append(_case_hr_supersede(i, t.slot, t.earlier, t.later, base))
    for i, (val_a, val_b) in enumerate(_COEXIST_PAIRS):
        cases.append(_case_coexist(i, val_a, val_b, base))
    for i, (val_a, val_b) in enumerate(_UNRELATED_PAIRS):
        cases.append(_case_unrelated(i, val_a, val_b, base))
    for i, (val_a, val_b) in enumerate(_HR_UNRELATED_PAIRS):
        cases.append(_case_hr_unrelated(i, val_a, val_b, base))
    for i, t in enumerate(_COEXIST_TRAP_SLOTS):
        cases.append(_case_coexist_trap(i, t.slot, t.earlier, t.later, base))
    # Deterministic "random" multi-fact cases: rng draws (including every redraw a rejection
    # triggers) happen in a FIXED order over the fixed seed, so regeneration is byte-identical.
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


def _check_aspect_disjointness(data: dict[str, Any]) -> list[str]:
    """The generator-enforced relatedness invariant, swept over the ENTIRE goldset (standalone AND
    multi cases): every `unrelated` pair joins two facts with different contents AND different
    source slots AND different aspects; every `coexist` pair joins two different slots (a shared
    aspect is allowed — that is what coexist means); every `supersede` pair joins one shared slot.
    Fact contents resolve through the exported `CONTENT_META` map; an unresolvable content is
    itself a violation (the map must cover every generated value). Returns failure messages
    (empty = clean)."""
    errors: list[str] = []
    for case in data.get("cases", []):
        cid = case.get("case_id", "<missing case_id>")
        content_by_id = {f["fact_id"]: f["content"] for f in case.get("facts", [])}
        for pair in case.get("pairs", []):
            e_txt = content_by_id.get(pair.get("earlier_id"))
            l_txt = content_by_id.get(pair.get("later_id"))
            e_meta = CONTENT_META.get(e_txt)
            l_meta = CONTENT_META.get(l_txt)
            if e_meta is None or l_meta is None:
                missing = e_txt if e_meta is None else l_txt
                errors.append(f"case {cid}: fact content not covered by CONTENT_META: {missing!r}")
                continue
            label = pair.get("label")
            if label == "unrelated":
                if e_txt == l_txt:
                    errors.append(f"case {cid}: unrelated pair joins IDENTICAL contents {e_txt!r}")
                if e_meta[0] == l_meta[0]:
                    errors.append(f"case {cid}: unrelated pair shares slot {e_meta[0]!r} ({e_txt!r} x {l_txt!r})")
                if e_meta[1] == l_meta[1]:
                    errors.append(f"case {cid}: unrelated pair shares aspect {e_meta[1]!r} ({e_txt!r} x {l_txt!r})")
            elif label == "coexist":
                if e_meta[0] == l_meta[0]:
                    errors.append(f"case {cid}: coexist pair shares slot {e_meta[0]!r} ({e_txt!r} x {l_txt!r})")
            elif label == "supersede":
                if e_meta[0] != l_meta[0]:
                    errors.append(f"case {cid}: supersede pair spans slots {e_meta[0]!r} != {l_meta[0]!r}")
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

    stderr = __import__("sys").stderr
    try:
        data = generate(args.seed)
    except RuntimeError as exc:
        print(f"GENERATION FAILED: {exc}", file=stderr)
        return 2
    errors = _validate_schema(data)
    if errors:
        print("SCHEMA VALIDATION FAILED:", file=stderr)
        for e in errors:
            print(f"  - {e}", file=stderr)
        return 2

    bar_errors = _check_bars(data)
    if bar_errors:
        print("CONTENT BAR FAILURES:", file=stderr)
        for e in bar_errors:
            print(f"  - {e}", file=stderr)
        return 2

    disjoint_errors = _check_aspect_disjointness(data)
    if disjoint_errors:
        print("ASPECT DISJOINTNESS FAILURES:", file=stderr)
        for e in disjoint_errors:
            print(f"  - {e}", file=stderr)
        return 2

    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.out.exists():
            print(f"CHECK FAILED: {args.out} does not exist yet — run without --check first", file=stderr)
            return 2
        on_disk = args.out.read_text(encoding="utf-8")
        if on_disk != rendered:
            print(f"CHECK FAILED: regeneration differs from {args.out} — determinism broken", file=stderr)
            return 2
        print(
            f"CHECK OK: {args.out} is schema-valid, content-bars-clean, aspect-disjointness-clean, "
            f"and byte-identical to a fresh regen ({len(data['cases'])} cases, seed={args.seed})"
        )
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.out} ({len(data['cases'])} cases, seed={args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
