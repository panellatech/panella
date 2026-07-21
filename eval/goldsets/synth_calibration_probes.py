#!/usr/bin/env python3
"""Generate the public K1 calibration universe from natural-language surfaces.

Each registry slot owns a deterministic pool of deliberately non-registry
surfaces.  The raw domain is a long-tail paraphrase, while the value and
evidence are realistic user data.  A generation sweep imports the live
resolver contracts and rejects deterministic hits, leaks, and routing drift.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from panella.resolver.blocking import assemble_blocking
from panella.resolver.normalize import resolver_normalize
from panella.resolver.registry import load_registry
from panella.resolver.risk import compute_risk_evidence
from panella.resolver.types import ResolveRequest

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "calibration_probes_v1.json"
SCAFFOLD_WORDS = frozenset({"fictional", "calibration", "synthetic", "probe", "placeholder"})

# Every tuple is (long-tail raw-domain paraphrase, extracted value, evidence template).
# Templates intentionally end with ``{uid}``: resolver_calibration's hermetic provider
# recovers the expected answer from that final whitespace-separated token.
# This remains ``calibration_probes_v1.json`` because downstream artifacts bind its hash, not its name.
SURFACE_POOLS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "fact:legal_name": (("official_identity_words", "Marin Cole", "My full name on the passport is Marin Cole {uid}"), ("legal_identity_record", "Amina Rios", "The passport lists my full identity as Amina Rios {uid}")),
    "fact:chosen_name": (("everyday_calling_words", "Nia", "At the cafe, everyone uses my nickname Nia {uid}"), ("everyday_calling_label", "Zee", "Friends use Zee when they greet me {uid}")),
    "fact:pronoun": (("self_reference_words", "they/them", "I'm this user; use they/them when referring to me {uid}"), ("user_reference_style", "she/her", "Use she/her when referring to this user {uid}")),
    "fact:home_city": (("hometown_place_label", "Port Alder", "I grew up in Port Alder, my hometown {uid}"), ("place_i_call_home", "Cedar Bay", "Cedar Bay is where I put down roots {uid}")),
    "fact:medical_allergy": (("adverse_reaction_detail", "cashews", "I am allergic to cashews, so I avoid them {uid}"), ("medical_reaction_note", "pecans", "Pecans make me allergic, so I avoid them {uid}")),
    "fact:medical_condition": (("long_term_health_detail", "migraine", "My chronic migraines are managed with rest {uid}"), ("ongoing_medical_note", "tinnitus", "Tinnitus is a chronic issue I manage with rest {uid}")),
    "fact:medication": (("daily_prescription_detail", "Luminara", "I follow a prescription for Luminara after breakfast {uid}"), ("daily_treatment_note", "Velora", "My daily pill is Velora after breakfast {uid}")),
    "preference:diet": (("everyday_meal_pattern", "plant-forward", "Most meals I choose follow a plant-forward eating style {uid}"), ("usual_meal_pattern", "pescatarian", "I mostly eat pescatarian meals {uid}")),
    "constraint:dietary_restriction": (("ingredient_avoidance_rule", "no shellfish", "I keep shellfish off my plate because it is restricted food {uid}"), ("dietary_choice_limit", "no sesame", "Sesame stays off my plate {uid}")),
    "fact:home_address": (("where_mail_reaches_me", "14 Harbor Lane", "Please send letters to 14 Harbor Lane, my street address {uid}"), ("where_parcels_arrive", "88 Juniper Road", "Mail comes to 88 Juniper Road on my street {uid}")),
    "fact:household_member": (("people_under_my_roof", "Rae, my cousin", "Rae is my housemate at the moment {uid}"), ("household_roster_note", "Mara, my aunt", "Mara shares my living space {uid}")),
    "fact:home_type": (("dwelling_shape_detail", "a loft apartment", "I live in a loft, the kind of residence I like {uid}"), ("dwelling_type_detail", "a townhouse", "I live in a townhouse {uid}")),
    "preference:home_amenity": (("room_feature_wish", "sunny balcony", "A sunny balcony is the house feature I would miss most {uid}"), ("amenity_wish", "reading nook", "A reading nook makes a place feel right {uid}")),
    "fact:employer": (("weekday_badge_destination", "Northline Labs", "Northline Labs is the company whose door I badge into every morning {uid}"), ("organization_i_badge_into", "Northwind Robotics", "I badge into Northwind Robotics every workday {uid}")),
    "fact:job_title": (("role_on_org_chart", "product researcher", "On the org chart, my position is product researcher {uid}"), ("org_chart_role", "service designer", "I serve as a service designer {uid}")),
    "fact:work_location": (("where_i_do_my_shift", "Harbor Tower", "I usually work from the office location called Harbor Tower {uid}"), ("usual_location", "Riverside Annex", "My desk is in Riverside Annex {uid}")),
    "preference:work_style": (("how_teamwork_feels", "quiet focus blocks", "For collaboration, I prefer quiet focus blocks {uid}"), ("working_rhythm", "asynchronous updates", "Asynchronous updates help me collaborate {uid}")),
    "preference:career_goal": (("future_work_aim", "lead a research team", "My professional goal is to lead a research team {uid}"), ("future_aspiration", "direct a design studio", "I hope to direct a design studio {uid}")),
    "fact:vehicle": (("daily_road_transport", "blue Kestrel hatchback", "My main car is a blue Kestrel hatchback {uid}"), ("daily_road_transport", "silver Ardent wagon", "This transport is used by this user each day {uid}")),
    "preference:commute_mode": (("getting_to_work_way", "tram", "The tram is my preferred commute {uid}"), ("travel_mode_choice", "ferry", "I take the ferry to get to work {uid}")),
    "preference:travel_class": (("seat_comfort_tier", "premium economy", "For long flights, I book premium economy cabin class {uid}"), ("preferred_flight_seating", "business", "For long flights, I book business {uid}")),
    "preference:parking_preference": (("car_storage_preference", "covered garage", "A covered garage is where I like to leave my car {uid}"), ("required_garage_cover", "shaded carport", "I need a shaded carport for my vehicle {uid}")),
    "fact:phone_model": (("handheld_device_model", "Aster One", "My smartphone is an Aster One {uid}"), ("handset_model_detail", "Nova 7", "I carry a Nova 7 each day {uid}")),
    "fact:computer_model": (("desk_machine_model", "Orchid 14", "The laptop on my desk is an Orchid 14 {uid}"), ("primary_machine_model", "Juniper 16", "Juniper 16 runs my projects {uid}")),
    "preference:code_editor": (("coding_tool_choice", "Zed", "Zed is the editor open when I write code {uid}"), ("code_workspace_tool", "Helix", "Helix is open when I write code {uid}")),
    "preference:browser": (("web_reading_tool", "Firefox", "Firefox is what I use to browse the web {uid}"), ("web_navigation_tool", "Orion", "Orion opens the sites I visit {uid}")),
    "fact:email": (("inbox_account_detail", "marin@postlane.test", "Messages reach my inbox at marin@postlane.test {uid}"), ("inbox_destination", "amina@relay.test", "Notes reach amina@relay.test in my inbox {uid}")),
    "preference:email_service": (("mail_handling_platform", "Postlane", "Postlane is the mail provider I rely on {uid}"), ("mail_delivery_platform", "Relay", "Relay handles the messages I send {uid}")),
    "fact:messaging_handle": (("chat_identity_label", "marin_on_the_move", "Friends can find my username as marin_on_the_move {uid}"), ("account_handle_detail", "zee_road", "Friends find my handle as zee_road {uid}")),
    "preference:messaging_service": (("conversation_platform_choice", "Signal", "Signal is the chat service I use with friends {uid}"), ("preferred_chat_network", "Wire", "I use Wire to message friends {uid}")),
    "fact:morning_routine": (("first_hours_habit", "tea and a walk", "My morning habit starts with tea and a walk {uid}"), ("morning_dawn_pattern", "stretching and tea", "Stretching and tea start my day {uid}")),
    "fact:sleep_schedule": (("nightly_rest_timing", "11:00 pm", "My usual bedtime is 11:00 pm {uid}"), ("sleep_night_pattern", "10:30 pm", "Lights go out at 10:30 pm {uid}")),
    "preference:meeting_time": (("calendar_slot_choice", "Tuesday afternoons", "Tuesday afternoons are my preferred meeting schedule {uid}"), ("meeting_calendar_slot", "Thursday mornings", "Thursday mornings work best for me {uid}")),
    "constraint:availability_window": (("times_i_can_join", "weekdays after 3", "My free time begins after 3 on weekdays {uid}"), ("time_window_note", "weekends before noon", "I can join before noon on weekends {uid}")),
    "fact:partner_name": (("person_i_share_life_with", "Jordan Lee", "My spouse is Jordan Lee {uid}"), ("person_i_share_life_with", "Avery Moss", "Avery Moss is my spouse {uid}")),
    "fact:relationship_status": (("current_partnership_state", "married", "I am married, and that is my current status {uid}"), ("partnership_state", "engaged", "My relationship is engaged {uid}")),
    "fact:social_circle": (("people_i_spend_time_with", "the Tuesday climbers", "The Tuesday climbers are my friend group {uid}"), ("friendship_circle", "the Saturday runners", "The Saturday runners are who I see most {uid}")),
    "preference:social_activity": (("group_fun_choice", "board games", "Board games are my favorite group activity {uid}"), ("group_pastime_choice", "community gardening", "Community gardening is the group pastime I enjoy {uid}")),
    "fact:pet_name": (("animal_calling_word", "Miso", "Miso is my pet's name {uid}"), ("animal_calling_word", "Pip", "Pip comes when I call {uid}")),
    "fact:pet_type": (("animal_species_detail", "greyhound", "A greyhound is the pet species I share my home with {uid}"), ("animal_species_detail", "tabby cat", "A tabby cat shares my home {uid}")),
    "preference:pet_care": (("animal_daily_care", "evening brush", "An evening brush is the pet routine that works best {uid}"), ("animal_daily_care", "morning grooming", "Morning grooming works best for us {uid}")),
    "preference:cuisine": (("food_region_flavor", "Levantine", "Levantine is the food preference I crave on weekends {uid}"), ("regional_style_note", "Ethiopian", "Ethiopian dishes are what I crave {uid}")),
    "preference:music_genre": (("sound_mood_choice", "jazz", "Jazz is my favorite music when I am cooking {uid}"), ("preferred_sound_style", "ambient", "Ambient helps me focus while cooking {uid}")),
    "preference:fitness_goal": (("movement_outcome_aim", "run 10K", "My workout goal is to run a 10K {uid}"), ("training_goal_aim", "cycle 50 km", "I am training to cycle 50 km {uid}")),
    "preference:wellness_routine": (("personal_recharge_habit", "evening journaling", "Evening journaling is my self care after busy days {uid}"), ("self_reflection_practice", "sunset writing", "I write to reset after busy days {uid}")),
    "fact:income_range": (("earnings_band_detail", "$90,000-$110,000", "My salary falls between $90,000 and $110,000 {uid}"), ("annual_compensation_band", "$120,000-$140,000", "My compensation is $120,000 to $140,000 {uid}")),
    "preference:budget_preference": (("purchase_spending_limit", "under $80", "For headphones, my spending budget is under $80 {uid}"), ("purchase_spending_ceiling", "under $120", "I keep headphone spending under $120 {uid}")),
    "fact:payment_method": (("checkout_tender_choice", "Visa debit", "At checkout, payment is by debit card {uid}"), ("checkout_payment_tender", "Mastercard credit", "I use Mastercard credit as payment at checkout {uid}")),
    "preference:shopping_style": (("buying_decision_habit", "compare reviews first", "I compare reviews first; it is my buying style {uid}"), ("buying_decision_habit", "wait for sales", "I wait for sales before choosing {uid}")),
    "constraint:emergency_contact": (("who_to_call_first", "Rae Cole, 555-0148", "In an emergency, call Rae Cole at 555-0148 {uid}"), ("urgent_call_person", "Mara Cole, 555-0186", "Call Mara Cole at 555-0186 in an urgent situation {uid}")),
    "preference:contact_preference": (("how_to_reach_me", "text message", "A text message is the contact method I answer fastest {uid}"), ("contact_reply_channel", "voice note", "Voice notes get my quickest response {uid}")),
    "constraint:accessibility_need": (("support_for_daily_tasks", "captions", "Captions are the accommodation I need during video calls {uid}"), ("video_call_support", "live captions", "I need live captions as an accommodation on video calls {uid}")),
    "constraint:safety_boundary": (("personal_harm_limit", "no surprise visits", "Unexpected visits feel unsafe, so I need advance notice {uid}"), ("personal_visit_rule", "no unannounced callers", "Unannounced callers feel unsafe, so I need notice {uid}")),
}


def _surface(slot_id: str, index: int, slot_count: int) -> tuple[str, str, str]:
    pool = SURFACE_POOLS.get(slot_id)
    if not pool:
        raise ValueError(f"missing natural-language surface pool for {slot_id}")
    return pool[(index // slot_count) % len(pool)]


def generate() -> dict[str, Any]:
    registry = load_registry()
    if set(SURFACE_POOLS) != set(registry.by_id):
        raise ValueError("surface pools must cover exactly the live registry slots")
    if any(len(pool) != 2 for pool in SURFACE_POOLS.values()):
        raise ValueError("every slot must have exactly two calibration surface bands")
    probes: list[dict[str, str]] = []
    routed: dict[str, int] = {"benign": 0, "hr": 0}
    selected_bands: set[int] = set()
    # Candidate surfaces are deliberately oversampled.  The retained pools are selected by the
    # live blocking receipt, not by the expected slot's risk bit: choice-set top-up can route a
    # superficially benign probe to hr.
    index = 0
    while routed["benign"] < 60 or routed["hr"] < 36:
        if index >= 10_000:
            raise RuntimeError("unable to fill the routed calibration probe floors")
        slot = registry.slots[index % len(registry.slots)]
        uid = f"cal-{index + 1:04d}"
        raw_domain, value, evidence_template = _surface(slot.slot_id, index, len(registry.slots))
        probe = {
            "probe_uid": uid,
            "kind": slot.kind,
            "raw_domain": raw_domain,
            "value": value,
            "evidence_text": evidence_template.format(uid=uid),
            "expected_slot_id": slot.slot_id,
        }
        request = ResolveRequest(uid, slot.kind, raw_domain, value, probe["evidence_text"])
        blocked = assemble_blocking(request, registry, compute_risk_evidence(request, registry))
        if not blocked.forced_overflow and slot.slot_id in blocked.receipt.choice_set and routed[blocked.receipt.slice] < {"benign": 60, "hr": 36}[blocked.receipt.slice]:
            probes.append(probe | {"slice": blocked.receipt.slice})
            routed[blocked.receipt.slice] += 1
            selected_bands.add((index // len(registry.slots)) % len(SURFACE_POOLS[slot.slot_id]))
        index += 1
    if selected_bands != {0, 1}:
        raise RuntimeError("calibration probe universe must emit both surface bands")
    document = {"version": "v2", "probes": probes}
    _sweep(document)
    return document


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(token for token in resolver_normalize(value).split("_") if token)


def _contains_ngram(tokens: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return any(tokens[index:index + len(needle)] == needle for index in range(len(tokens) - len(needle) + 1))


def _is_hard_band(probe: dict[str, str], target: Any) -> bool:
    tokens = _tokens(" ".join((probe["raw_domain"], probe["value"], probe["evidence_text"])))
    return not any(_contains_ngram(tokens, _tokens(surface)) for surface in (target.domain, *target.aliases))


def _sweep(document: dict[str, Any]) -> None:
    registry = load_registry()
    probes = document["probes"]
    if len(probes) < 96 or len({probe["probe_uid"] for probe in probes}) != len(probes):
        raise ValueError("probe universe must have unique uid coverage")
    evidence_prefixes = {_tokens(probe["evidence_text"])[:3] for probe in probes}
    if len(evidence_prefixes) < 20:
        raise ValueError("probe universe must have at least 20 distinct 3-token evidence prefixes")
    hard_band_count = sum(_is_hard_band(probe, registry.by_id[probe["expected_slot_id"]]) for probe in probes)
    if hard_band_count < 40:
        raise ValueError("probe universe is below the hard-band floor")
    if hard_band_count == len(probes):
        raise ValueError("probe universe must emit both calibration surface bands")
    if len({(probe["raw_domain"], probe["value"]) for probe in probes}) < 90:
        raise ValueError("probe universe has fewer than 90 distinct surface pairs")
    counts = {"benign": 0, "hr": 0}
    for probe in probes:
        request = ResolveRequest(probe["probe_uid"], probe["kind"], probe["raw_domain"], probe["value"], probe["evidence_text"])
        normalized = resolver_normalize(request.raw_domain)
        if f"{request.kind}:{normalized}" in registry.by_id or request.raw_domain in registry.alias_raw or normalized in registry.alias_folded:
            raise ValueError(f"probe {request.request_uid} hits a deterministic resolver layer")
        target = registry.by_id.get(probe["expected_slot_id"])
        if target is None:
            raise ValueError(f"probe {request.request_uid} has unknown expected slot")
        target_tokens = _tokens(target.domain)
        for field_name, field_value in (("value", request.value), ("evidence_text", request.evidence_text)):
            if _contains_ngram(_tokens(field_value), target_tokens):
                raise ValueError(f"probe {request.request_uid} leaks expected domain token sequence in {field_name}")
            if any(re.search(rf"\b{re.escape(word)}\b", field_value, flags=re.IGNORECASE) for word in SCAFFOLD_WORDS):
                raise ValueError(f"probe {request.request_uid} contains scaffold word in {field_name}")
        evidence = compute_risk_evidence(request, registry)
        blocked = assemble_blocking(request, registry, evidence)
        if blocked.forced_overflow or probe["slice"] != blocked.receipt.slice:
            raise ValueError(f"probe {request.request_uid} has inconsistent routed slice")
        if target.slot_id not in blocked.receipt.choice_set:
            raise ValueError(f"probe {request.request_uid} omits expected slot from choice set")
        if target.high_risk and target.slot_id not in evidence.matched_hr_slot_ids:
            raise ValueError(f"probe {request.request_uid} lacks target high-risk evidence")
        counts[blocked.receipt.slice] += 1
    if counts["benign"] < 60 or counts["hr"] < 36:
        raise ValueError("probe universe is below the frozen calibration floor")


def _canonical(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    content = _canonical(generate())
    if args.check:
        if not args.out.exists() or args.out.read_text(encoding="utf-8") != content:
            raise SystemExit("calibration probe universe is not byte-identical to deterministic generation")
        print("calibration probes: OK")
        return 0
    args.out.write_text(content, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
