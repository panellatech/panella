#!/usr/bin/env python3
"""K0 goldset scaffolding — SHADOW preference-extraction eval (key_correctness scorer). NO durable
writes. Ported from the proven upstream extraction eval (renamed `extraction_eval.py` ->
`key_correctness_eval.py`; the local `preference_extraction.py` replaces the daemon-internal
extractor it originally called — same scoring contract, portable transport).

Runs the reference extractor (`eval.goldsets.preference_extraction.extract_preferences`) over the
synthetic fixture's per-session facts + a labeled negative/high-risk set, then scores it against the
hand-labeled goldset (`fixtures/extraction_goldset_v1.json`) to answer ONE question: *can the
extractor produce stable, non-colliding canonical keys at high enough precision to build a
key-addressable fact store?*

The LLM is INJECTED (``chat_fn``): a real run binds an OpenAI-compatible transport or a local codex
CLI subprocess; hermetic tests inject a fake. Scoring is deterministic given the extractor outputs.
This module performs NO durable writes and never touches a store — it only reads fixtures and
scores extraction quality (the 0.038-weakness metric the construction rung attacks).

GO/NO-GO bars:
  schema validity 100% | 0 critical false collisions | supersede precision >=0.95 (1.0 on high-risk) |
  key stability >=0.90 | negatives produce 0 colliding keys | every predicted key listed for manual review.
Decision: PASS -> proceed | precision-pass/stability-weak -> ADD-only-no-supersede | any critical
collision -> NO-GO-supersede.

HONEST SCOPE: the v1 fixture extends the v0 high-risk SINGLETONS with several high-risk UPDATE
pairs (medication / emergency-contact / legal-name / primary-physician / dietary-restriction
lifecycles — see ``fixtures/continuity_set_v1.json`` and the matching ``lifecycle``-tagged labels in
``fixtures/extraction_goldset_v1.json``), so high-risk SUPERSEDE precision IS exercisable now: a
clean extractor run CAN report ``high_risk_supersede_proven=true``. This flag stays COMPUTED from
the actual run's results (every high-risk update pair merged on its gold key across the WHOLE
chain, zero high-risk collisions) — it is never asserted merely because the fixture now CONTAINS
high-risk update pairs; a weak or partial extractor still reports ``false`` here even on this
richer fixture. Treat a ``true`` reading as "proven for THIS run's extractor against the current
fixture", not a permanent property of the goldset.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from eval._paths import assert_eval_out
from eval.goldsets.preference_extraction import ChatFn, PreferenceCandidate, extract_preferences, normalize_domain

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFAULT_GOLDSET = _FIXTURES / "extraction_goldset_v1.json"
DEFAULT_FIXTURE = _FIXTURES / "continuity_set_v1.json"

# Gate thresholds (the PASS bars).
KEY_STABILITY_MIN = 0.90
SUPERSEDE_PRECISION_MIN = 0.95
VALUE_MATCH_OVERLAP = 0.5  # token-overlap fraction for a candidate value to count as the gold value


@dataclass(frozen=True)
class GoldItem:
    item_id: str
    text: str
    should_extract: bool
    high_risk: bool
    gold_kind: str | None = None
    gold_key: str | None = None  # gold canonical_key (the slot); None for negatives
    gold_value: str | None = None
    lifecycle: str | None = None
    effective_at: str | None = None
    supersedes: str | None = None
    # An adjacency near-miss probe (expected ADD; must NOT bind its neighbour). Carried so a
    # downstream sim can slice adjacency_false_match. wrong_slot_deny = the realistic wrong-slot
    # traps for a high-risk slot (domain-only), used ONLY by high_risk_slot_recall.
    adjacency_probe: bool = False
    wrong_slot_deny: tuple[str, ...] = ()


def _norm_tokens(s: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]+", " ", str(s).lower()).split())


def value_match(gold_value: str, cand_value: str) -> bool:
    """A candidate value counts as the gold value when >=VALUE_MATCH_OVERLAP of the gold's tokens
    appear in the candidate (handles 'black coffee' vs 'black, no sugar'). Value precision is
    secondary to keys here, so the match is deliberately lenient."""
    gold = _norm_tokens(gold_value)
    if not gold:
        return False
    return len(gold & _norm_tokens(cand_value)) / len(gold) >= VALUE_MATCH_OVERLAP


# Negation/polarity tokens — for HIGH-RISK value grounding, a candidate that differs in NET negation
# from the gold (e.g. 'not penicillin-class' / 'no shellfish allergy' vs 'shellfish') has FLIPPED
# meaning and is NOT a safe match, even though token-overlap value_match would accept it.
_NEGATORS = frozenset(
    {"no", "not", "without", "never", "none", "non", "stopped", "discontinued", "denies", "denied", "deny", "off", "negative"}
)

# RESTRICTION-style values — dietary restrictions are lexically negative without being a polarity
# FLIP: 'gluten-free' == 'no gluten' == 'gluten free diet' all mean the same restriction. A marker
# makes a value restriction-style; direction (exclude vs limit) must still match so 'low-sodium'
# does NOT match 'high-sodium'. Only the dietary slot's values carry markers.
_RESTRICTION_MARKERS = frozenset({"free", "low", "reduced", "no", "without", "avoid", "avoids", "restricted", "minus"})
_RESTRICTION_EXCLUDE = frozenset({"free", "no", "without", "avoid", "avoids", "restricted", "minus"})
_RESTRICTION_STOPWORDS = frozenset({"diet", "of", "in", "a", "an", "the", "my"})


def _restriction_signature(value: str) -> tuple[str, frozenset[str]] | None:
    """``(direction, core_nouns)`` if ``value`` is restriction-style (contains a marker), else
    ``None``. direction = 'exclude' (free/no/without/avoid/restricted/minus) or 'limit' (low/reduced)."""
    toks = _norm_tokens(value)
    if not (toks & _RESTRICTION_MARKERS):
        return None
    direction = "exclude" if (toks & _RESTRICTION_EXCLUDE) else "limit"
    core = toks - _RESTRICTION_MARKERS - _RESTRICTION_STOPWORDS
    return direction, frozenset(core)


def high_risk_value_match(gold_value: str, cand_value: str) -> bool:
    """Safe high-risk value match. Two branches (net-polarity + restriction-equivalence):

    - RESTRICTION-style (dietary): both sides must be restriction-style, SAME direction (exclude vs
      limit), and share a core noun. So 'gluten-free' == 'no gluten' (exclude/{gluten}); 'low-sodium'
      == 'reduced sodium' (limit/{sodium}); but 'low-sodium' vs 'high-sodium' FAILS (one-sided -> not
      restriction-style), and an allergen 'shellfish' vs 'no shellfish allergy' FAILS (one-sided
      restriction-style).
    - NON-restriction (allergens/meds/names/insurers): exactness matters, so require ALL gold tokens
      present in the candidate (NOT the lenient 0.5-overlap value_match) AND equal NET polarity. A
      proper-name/drug PARTIAL overlap is a DANGEROUS false match for these slots -- the all-gold-
      tokens gate rejects a shared-token-but-different-entity pair. A superset candidate still
      matches; a polarity flip still FAILS."""
    g_sig = _restriction_signature(gold_value)
    c_sig = _restriction_signature(cand_value)
    if g_sig is not None or c_sig is not None:
        if g_sig is None or c_sig is None:  # one-sided restriction-style -> not a safe match
            return False
        return g_sig[0] == c_sig[0] and bool(g_sig[1] & c_sig[1])
    gold_tokens = _norm_tokens(gold_value)
    cand_tokens = _norm_tokens(cand_value)
    if not gold_tokens or not (gold_tokens <= cand_tokens):  # ALL gold tokens must be present (no partial)
        return False
    return bool(gold_tokens & _NEGATORS) == bool(cand_tokens & _NEGATORS)


def load_fixture_text(fixture_path: Path = DEFAULT_FIXTURE) -> dict[str, str]:
    """Map each continuity-fixture session id -> its user-turn text (what the extractor sees)."""
    data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for lc in data.get("lifecycles", []):
        for sess in lc.get("sessions", []):
            turns = [str(t.get("content", "")) for t in sess.get("turns", []) if t.get("role") == "user"]
            out[str(sess["sid"])] = "\n".join(turns).strip()
    return out


def load_items(goldset_path: Path = DEFAULT_GOLDSET, fixture_path: Path = DEFAULT_FIXTURE) -> list[GoldItem]:
    """Build the eval items: fixture-referenced labels (text loaded by sid) + inline extra items."""
    gold = json.loads(Path(goldset_path).read_text(encoding="utf-8"))
    fixture_text = load_fixture_text(fixture_path)
    items: list[GoldItem] = []
    for lab in gold.get("labels", []):
        sid = str(lab["source_sid"])
        text = fixture_text.get(sid)
        if text is None:
            raise ValueError(f"goldset label references unknown fixture sid {sid!r}")
        items.append(
            GoldItem(
                item_id=sid,
                text=text,
                should_extract=bool(lab.get("should_extract", True)),
                high_risk=bool(lab.get("high_risk", False)),
                gold_kind=lab.get("kind"),
                gold_key=lab.get("canonical_key"),
                gold_value=lab.get("value"),
                lifecycle=lab.get("lifecycle"),
                effective_at=lab.get("effective_at"),
                supersedes=lab.get("supersedes"),
                adjacency_probe=bool(lab.get("adjacency_probe", False)),
                wrong_slot_deny=tuple(lab.get("wrong_slot_deny", []) or []),
            )
        )
    for extra in gold.get("extra_items", []):
        items.append(
            GoldItem(
                item_id=str(extra["id"]),
                text=str(extra["text"]),
                should_extract=bool(extra.get("should_extract", False)),
                high_risk=bool(extra.get("high_risk", False)),
                gold_kind=extra.get("kind"),
                gold_key=extra.get("canonical_key"),
                gold_value=extra.get("value"),
                lifecycle=extra.get("lifecycle"),
                effective_at=extra.get("effective_at"),
                supersedes=extra.get("supersedes"),
                adjacency_probe=bool(extra.get("adjacency_probe", False)),
                wrong_slot_deny=tuple(extra.get("wrong_slot_deny", []) or []),
            )
        )
    return items


def _best_candidate(item: GoldItem, cands: list[PreferenceCandidate]) -> PreferenceCandidate | None:
    """The candidate that represents this item's gold fact: the (first) one whose value matches the
    gold value; if none match by value, the highest-confidence candidate (so a wrong-value
    extraction still counts as a recall hit but is flagged value-mismatch)."""
    if not cands:
        return None
    if item.gold_value:
        for c in cands:
            if value_match(item.gold_value, c.value):
                return c
    return max(cands, key=lambda c: c.confidence)


@dataclass
class ExtractionReport:
    extraction_recall: float = 0.0
    value_match_rate: float = 0.0
    key_correctness: float = 0.0
    key_stability: float = 0.0
    supersede_precision: float = 0.0
    high_risk_recall: float = 0.0
    high_risk_value_recall: float = 1.0
    high_risk_slot_recall: float = 1.0
    high_risk_key_correctness: float = 1.0
    high_risk_update_pairs: int = 0
    high_risk_supersede_proven: bool = False
    high_risk_collisions: int = 0
    harmful_collisions: int = 0
    negative_false_positive_rate: float = 0.0
    negative_colliding_keys: int = 0
    schema_validity: float = 1.0
    counts: dict = field(default_factory=dict)
    collisions: list = field(default_factory=list)
    per_lifecycle: list = field(default_factory=list)
    keys_for_review: list = field(default_factory=list)


def score(
    items: list[GoldItem],
    extractions: dict[str, list[PreferenceCandidate]],
    parse_stats: dict[str, dict[str, int]] | None = None,
) -> ExtractionReport:
    """Deterministically score extractor outputs against the goldset. ``extractions`` maps item_id ->
    the candidates the extractor produced. ``parse_stats`` (item_id -> {raw_objects, dropped,
    coerced}) lets schema_validity reflect TRUE model compliance over raw objects (counting dropped
    + coerced) -- without it (hand-built extraction tests) schema_validity falls back to
    emitted-candidate validity."""
    rep = ExtractionReport()

    positives = [it for it in items if it.should_extract]
    negatives = [it for it in items if not it.should_extract]

    # Collision/precision/stability/key-correctness score over the VALUE-GROUNDED candidates: those
    # whose value matches the ITEM's labeled gold value. This is the right attribution unit —
    # attributing EVERY emitted candidate to the item's single gold slot would falsely flag a
    # legitimate SECONDARY attribute (e.g. a correct fact:job_title extracted from an employer
    # mention must NOT be recorded as belonging to fact:employer). Value-grounding catches the
    # dangerous case (a shared key carrying DIFFERENT real values from different slots -> it
    # value-matches each item's gold -> attributed to both -> collision) while ignoring legit/junk
    # extras that don't carry a labeled value.
    hr_total = sum(1 for it in positives if it.high_risk)
    recall_hits = value_hits = key_correct = hr_recall = hr_key_correct = hr_value_hits = hr_slot_hits = 0
    grounded_keys: dict[str, set[str]] = {}  # item_id -> keys of candidates value-matching the gold value
    for it in positives:
        cands = extractions.get(it.item_id, [])
        grounded = {c.canonical_key for c in cands if it.gold_value and value_match(it.gold_value, c.value)}
        grounded_keys[it.item_id] = grounded
        best = _best_candidate(it, cands)
        if best is not None:
            recall_hits += 1
            if it.gold_value and value_match(it.gold_value, best.value):
                value_hits += 1
                if it.high_risk and high_risk_value_match(it.gold_value, best.value):
                    hr_value_hits += 1
            if it.high_risk:
                hr_recall += 1
        # high_risk SLOT recall (the wrong-slot-right-value gate): a high-risk item is slot-captured
        # iff SOME candidate has high_risk_value_match (right value, no polarity/direction flip) AND
        # the right kind AND a domain NOT in the slot's curated wrong_slot_deny set (a DENY-list, not
        # a brittle allow-list — a reasonable unanticipated key passes; the dangerous wrong-slot fails).
        if it.high_risk and it.gold_value:
            deny = {normalize_domain(s) for s in it.wrong_slot_deny}
            if any(
                high_risk_value_match(it.gold_value, c.value) and c.kind == it.gold_kind and normalize_domain(c.domain) not in deny
                for c in cands
            ):
                hr_slot_hits += 1
        if it.gold_key and it.gold_key in grounded:
            key_correct += 1
            if it.high_risk:
                hr_key_correct += 1

    n_pos = len(positives) or 1
    rep.extraction_recall = recall_hits / n_pos
    rep.value_match_rate = value_hits / n_pos
    rep.key_correctness = key_correct / n_pos
    rep.high_risk_recall = (hr_recall / hr_total) if hr_total else 1.0
    # high_risk_VALUE_recall = high-risk items captured with the CORRECT value (value-grounded), NOT
    # just "some candidate emitted" — the high-risk SAFETY gate. Synonym-robust (no key-string match)
    # but value-grounded (the actual sensitive value is right).
    rep.high_risk_value_recall = (hr_value_hits / hr_total) if hr_total else 1.0
    # high_risk_SLOT_recall = value-grounded AND right-kind AND not a wrong-slot trap (the PASS gate;
    # supersedes the value-only high_risk_value_recall, which stays REPORTED).
    rep.high_risk_slot_recall = (hr_slot_hits / hr_total) if hr_total else 1.0
    rep.high_risk_key_correctness = (hr_key_correct / hr_total) if hr_total else 1.0
    pos_keys = grounded_keys  # stability uses the grounded keys (a slot's update is "merged" on them)

    # Per-key gold-slot OCCURRENCES over the VALUE-GROUNDED candidates (basis for collision + merge
    # precision). A key emitted for 2+ distinct gold slots (each grounded by that slot's real value)
    # is a harmful cross-slot merge.
    key_slot_occ: dict[str, list[str | None]] = {}
    key_high_risk: dict[str, bool] = {}
    for it in positives:
        for c in extractions.get(it.item_id, []):
            if it.gold_value and value_match(it.gold_value, c.value):
                key_slot_occ.setdefault(c.canonical_key, []).append(it.gold_key)
                if it.high_risk:
                    key_high_risk[c.canonical_key] = True

    # Harmful COLLISIONS: a key emitted for 2+ DIFFERENT gold slots (over ALL candidates).
    for k, slots in sorted(key_slot_occ.items()):
        distinct = {s for s in slots if s is not None}
        if len(distinct) > 1:
            rep.harmful_collisions += 1
            rep.collisions.append({"extractor_key": k, "gold_slots": sorted(distinct)})
            if key_high_risk.get(k):
                rep.high_risk_collisions += 1

    # SUPERSEDE precision = merge PRECISION over ALL candidate pairs sharing a key: of every pair the
    # extractor would merge (same key -> supersede candidate), how many are genuinely the SAME gold
    # slot? A cross-slot merge is a FALSE supersede (would demote a valid distinct fact) -- the
    # dangerous error. Unmerged real updates hurt RECALL (key_stability), not precision.
    merged_pairs = correct_pairs = 0
    for slots in key_slot_occ.values():
        for a, b in combinations(slots, 2):
            merged_pairs += 1
            if a is not None and a == b:
                correct_pairs += 1
    rep.supersede_precision = (correct_pairs / merged_pairs) if merged_pairs else 1.0

    # Key STABILITY = merge RECALL over gold update pairs: a consecutive (older->newer) pair is
    # "merged" when the two sessions share ANY emitted key (so a supersede could fire on that slot).
    by_lifecycle: dict[str, list[GoldItem]] = {}
    for it in positives:
        if it.lifecycle:
            by_lifecycle.setdefault(it.lifecycle, []).append(it)
    gold_pairs = stab_hits = multi = 0
    for lc, lc_items in sorted(by_lifecycle.items()):
        if len(lc_items) < 2:
            continue
        multi += 1
        ordered = sorted(lc_items, key=lambda i: i.effective_at or "")
        lc_ok = lc_pairs = 0
        for older, newer in zip(ordered, ordered[1:], strict=False):
            gold_pairs += 1
            lc_pairs += 1
            if pos_keys[older.item_id] & pos_keys[newer.item_id]:
                stab_hits += 1
                lc_ok += 1
        rep.per_lifecycle.append({"lifecycle": lc, "merged_pairs": lc_ok, "gold_pairs": lc_pairs, "stable": lc_ok == lc_pairs})
    rep.key_stability = (stab_hits / gold_pairs) if gold_pairs else 1.0

    # High-risk SUPERSEDE coverage (HONEST SCOPE): is there any high-risk UPDATE pair, and is it
    # merged on the GOLD key? The shipped fixture has only high-risk SINGLETONS, so this stays False
    # -- high-risk supersede precision is UNPROVEN here. Hard gate for a FUTURE high-risk-supersede
    # rollout (extend the goldset until proven), NOT a bar this eval itself must clear.
    hr_update_pairs = hr_update_ok = 0
    for lc_items in by_lifecycle.values():
        if len(lc_items) < 2 or not any(i.high_risk for i in lc_items):
            continue
        ordered = sorted(lc_items, key=lambda i: i.effective_at or "")
        for older, newer in zip(ordered, ordered[1:], strict=False):
            hr_update_pairs += 1
            gold = newer.gold_key
            if gold and gold in pos_keys[older.item_id] and gold in pos_keys[newer.item_id]:
                hr_update_ok += 1
    rep.high_risk_update_pairs = hr_update_pairs
    rep.high_risk_supersede_proven = hr_update_pairs > 0 and hr_update_ok == hr_update_pairs and rep.high_risk_collisions == 0

    # NEGATIVES: false positives + any negative whose emitted key collides with ANY positive's
    # EMITTED key. The store has a row for EVERY emitted positive key (grounded OR legit-secondary),
    # and merges on the emitted canonical_key -- so a negative landing on any of them is a real
    # junk->real-row merge that a gold-key (or grounded-only) comparison would miss.
    all_emitted_pos_keys = {c.canonical_key for it in positives for c in extractions.get(it.item_id, [])}
    neg_fp = neg_collide = 0
    for it in negatives:
        cands = extractions.get(it.item_id, [])
        if cands:
            neg_fp += 1
            if any(c.canonical_key in all_emitted_pos_keys for c in cands):
                neg_collide += 1
    rep.negative_false_positive_rate = neg_fp / (len(negatives) or 1)
    rep.negative_colliding_keys = neg_collide

    # Schema validity over ALL emitted candidates (kind in the allowed set, non-empty domain+value) +
    # the full key->items map for the mandatory manual review.
    total_cands = valid_cands = 0
    review: dict[str, set[str]] = {}
    for it in items:
        for c in extractions.get(it.item_id, []):
            total_cands += 1
            if c.kind in ("preference", "fact", "constraint") and c.domain and c.value and not c.coerced_kind:
                valid_cands += 1
            review.setdefault(c.canonical_key, set()).add(it.item_id)
    if parse_stats:
        # TRUE schema compliance over RAW model objects: dropped (missing domain/value) + coerced
        # (omitted/invalid kind) are model-contract failures that emitted-only counting would mask (a
        # model returning mostly-malformed output could otherwise score 1.0).
        raw_total = sum(s.get("raw_objects", 0) for s in parse_stats.values())
        raw_bad = sum(s.get("dropped", 0) + s.get("coerced", 0) + s.get("malformed", 0) for s in parse_stats.values())
        rep.schema_validity = ((raw_total - raw_bad) / raw_total) if raw_total else 1.0
    else:
        rep.schema_validity = (valid_cands / total_cands) if total_cands else 1.0
    rep.keys_for_review = [{"canonical_key": k, "from_items": sorted(v)} for k, v in sorted(review.items())]

    rep.counts = {
        "positives": len(positives),
        "negatives": len(negatives),
        "multi_session_lifecycles": multi,
        "gold_update_pairs": gold_pairs,
        "merged_pairs": merged_pairs,
        "high_risk": hr_total,
        "total_candidates": total_cands,
    }
    return rep


def decide(rep: ExtractionReport) -> dict:
    """Apply the GO/NO-GO decision rule."""
    # ANY durable candidate hallucinated on a no-memory NEGATIVE is a precision failure -- even a
    # NON-colliding one a downstream sim never sees. Negatives are owned by this extraction layer, so
    # gate the full FP rate to 0, not just collisions.
    critical_collision = (
        rep.harmful_collisions > 0
        or rep.negative_colliding_keys > 0
        or rep.high_risk_collisions > 0
        or rep.negative_false_positive_rate > 0
    )
    # High-risk SAFETY = the item is extracted about the RIGHT fact (value-grounded high_risk_recall)
    # AND never merged into another slot (high_risk_collisions). It is NOT gated on the extractor
    # matching our gold key STRING: a real extractor legitimately uses stable synonyms -- gating
    # exact-gold-key-match would false-NO-GO a perfectly safe extractor. high_risk_key_correctness
    # stays REPORTED for visibility, not a PASS bar; the intrinsic stability/collision/supersede
    # metrics are synonym-robust.
    precision_ok = (
        rep.supersede_precision >= SUPERSEDE_PRECISION_MIN
        and rep.high_risk_slot_recall >= 1.0  # right value + right kind + not a wrong-slot trap
        and rep.high_risk_collisions == 0
        and rep.schema_validity >= 1.0
    )
    stability_ok = rep.key_stability >= KEY_STABILITY_MIN

    if critical_collision:
        verdict = "NO-GO-SUPERSEDE"
        rationale = "critical false collision (cross-slot or high-risk or negative-into-real-slot) — rethink the key model"
    elif precision_ok and stability_ok:
        verdict = "PASS"
        rationale = "proceed to ADD-only then supersede"
    elif precision_ok and not stability_ok:
        verdict = "ADD-ONLY-NO-SUPERSEDE"
        rationale = "precision ok but key stability weak — ship ADD-only, redesign keys before supersede"
    else:
        verdict = "NO-GO-SUPERSEDE"
        rationale = "supersede/high-risk precision below bar — rethink before any supersede"
    # HONEST-SCOPE caveats: things this PASS does NOT certify (forcing functions for later
    # rollouts). `high_risk_supersede_proven=false` has TWO distinct causes with different owners:
    # a goldset with zero hr update pairs (fix the GOLDSET) vs. an extractor that failed to merge
    # every hr update pair the goldset does contain on its gold key with zero hr collisions (fix
    # the EXTRACTOR) — the caveat names whichever actually happened.
    caveats = []
    if not rep.high_risk_supersede_proven:
        if rep.high_risk_update_pairs == 0:
            caveats.append(
                "high-risk SUPERSEDE precision UNPROVEN (no high-risk update pair in the goldset); "
                "a future high-risk-supersede rollout is gated on extending the goldset until "
                "high_risk_supersede_proven is true"
            )
        else:
            caveats.append(
                "high-risk SUPERSEDE precision UNPROVEN (the goldset contains high-risk update "
                "pairs, but this run did not merge every one on its gold key with zero high-risk "
                "collisions — an extractor coverage failure, not a fixture gap); a future "
                "high-risk-supersede rollout stays gated until high_risk_supersede_proven is true"
            )
    return {
        "verdict": verdict,
        "rationale": rationale,
        "caveats": caveats,
        "high_risk_supersede_proven": rep.high_risk_supersede_proven,
        "bars": {
            "schema_validity": [rep.schema_validity, 1.0],
            "harmful_collisions": [rep.harmful_collisions, 0],
            "high_risk_collisions": [rep.high_risk_collisions, 0],
            "negative_colliding_keys": [rep.negative_colliding_keys, 0],
            "negative_false_positive_rate": [round(rep.negative_false_positive_rate, 3), 0],
            "supersede_precision": [round(rep.supersede_precision, 3), SUPERSEDE_PRECISION_MIN],
            "key_stability": [round(rep.key_stability, 3), KEY_STABILITY_MIN],
            "high_risk_slot_recall": [round(rep.high_risk_slot_recall, 3), 1.0],
            "high_risk_value_recall": [round(rep.high_risk_value_recall, 3), 1.0],
            "high_risk_recall": [round(rep.high_risk_recall, 3), 1.0],
            "high_risk_key_correctness": [round(rep.high_risk_key_correctness, 3), 1.0],
        },
    }


def run_eval(chat_fn: ChatFn, *, goldset_path: Path = DEFAULT_GOLDSET, fixture_path: Path = DEFAULT_FIXTURE) -> dict:
    """End-to-end: load items, run the extractor over each (injected chat_fn), score, decide. Returns
    a JSON-able report. NO durable writes."""
    items = load_items(goldset_path, fixture_path)
    extractions: dict[str, list[PreferenceCandidate]] = {}
    parse_stats: dict[str, dict[str, int]] = {}
    for it in items:
        s: dict[str, int] = {}
        extractions[it.item_id] = extract_preferences(it.text, it.item_id, chat_fn=chat_fn, stats=s)
        parse_stats[it.item_id] = s
    rep = score(items, extractions, parse_stats=parse_stats)
    result = decide(rep)
    result["report"] = {
        "extraction_recall": round(rep.extraction_recall, 3),
        "value_match_rate": round(rep.value_match_rate, 3),
        "key_correctness": round(rep.key_correctness, 3),
        "key_stability": round(rep.key_stability, 3),
        "supersede_precision": round(rep.supersede_precision, 3),
        "high_risk_recall": round(rep.high_risk_recall, 3),
        "high_risk_value_recall": round(rep.high_risk_value_recall, 3),
        "high_risk_slot_recall": round(rep.high_risk_slot_recall, 3),
        "high_risk_key_correctness": round(rep.high_risk_key_correctness, 3),
        "high_risk_update_pairs": rep.high_risk_update_pairs,
        "high_risk_supersede_proven": rep.high_risk_supersede_proven,
        "harmful_collisions": rep.harmful_collisions,
        "high_risk_collisions": rep.high_risk_collisions,
        "negative_false_positive_rate": round(rep.negative_false_positive_rate, 3),
        "negative_colliding_keys": rep.negative_colliding_keys,
        "schema_validity": round(rep.schema_validity, 3),
        "counts": rep.counts,
    }
    result["collisions"] = rep.collisions
    result["per_lifecycle"] = rep.per_lifecycle
    result["keys_for_review"] = rep.keys_for_review
    return result


def _raise_on_transport_error(raw: str) -> str:
    """Fail CLOSED on an LLM transport error. Sentinel strings (``__ERR__401``, ``__ERR__retry``, ...)
    on HTTP/retry failure must never reach ``parse_candidates`` -- they would parse to ``[]`` and a
    bad key / quota outage / invalid model would produce a valid-LOOKING zero-recall / NO-GO report
    with exit 0, corrupting the decision packet. Detect the sentinel and ABORT so an infra failure
    surfaces as an error, never a fake verdict."""
    if raw.startswith("__ERR__"):
        raise RuntimeError(
            f"extractor LLM transport error ({raw}); aborting — a transport failure must NOT masquerade "
            "as a real GO/NO-GO verdict. Fix the key/model/quota and re-run."
        )
    return raw


def _codex_chat_fn(model: str | None = None, *, timeout: float = 240.0, retries: int = 3) -> ChatFn:
    """DEFAULT real-run transport: a local `codex` CLI subprocess (device-auth SUBSCRIPTION -- no
    per-call API key, no cost if you have one configured). Reads the response from
    --output-last-message (codex stdout has banner/plugin noise).

    BOUNDED RETRY (transport resilience, NOT scoring): retry up to ``retries`` times, then FAIL
    CLOSED -- a persistent transport failure must NEVER become a fake verdict."""
    import subprocess
    import tempfile
    import time

    def _chat(system: str, user: str) -> str:
        prompt = f"{system}\n\n---\n\n{user}"
        argv = ["codex", "exec", "--skip-git-repo-check", "--output-last-message", "", "-"]
        if model:
            argv[2:2] = ["--model", model]
        last_err = ""
        for attempt in range(retries):
            with tempfile.NamedTemporaryFile(prefix="k0-extract-", suffix=".txt", delete=False) as tmp:
                out_path = tmp.name
            argv[-2] = out_path  # the --output-last-message slot (argv: ..., out_path, "-")
            try:
                proc = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, check=False)
                text = Path(out_path).read_text(encoding="utf-8") if Path(out_path).exists() else ""
                if proc.returncode == 0 and text.strip():
                    return text
                last_err = f"exit={proc.returncode} empty={not text.strip()}"
            except subprocess.TimeoutExpired:
                last_err = "timeout"
            finally:
                Path(out_path).unlink(missing_ok=True)
            if attempt < retries - 1:
                time.sleep(2.0 * (attempt + 1))
        raise RuntimeError(
            f"codex exec failed after {retries} attempts ({last_err}); aborting — a transport failure must "
            "NOT masquerade as a real GO/NO-GO verdict. Fix the key/model/quota and re-run."
        )

    return _chat


def _openai_chat_fn(model: str) -> ChatFn:
    """Alternative transport: OpenAI chat (per-token COST — prefer --backend codex). Bind key+model,
    fail closed on transport errors."""
    import json as _json
    import os
    import urllib.request

    key = os.environ.get("OPENAI_API_KEY", "")
    if not key and os.environ.get("OPENAI_API_KEY_FILE"):
        key = Path(os.environ["OPENAI_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("set OPENAI_API_KEY or OPENAI_API_KEY_FILE")

    def _chat(system: str, user: str) -> str:
        body = _json.dumps(
            {
                "model": model,
                "temperature": 0,
                "max_tokens": 400,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            }
        ).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return _raise_on_transport_error(_json.load(r)["choices"][0]["message"]["content"].strip())
        except Exception as exc:  # noqa: BLE001
            return _raise_on_transport_error(f"__ERR__{type(exc).__name__}")

    return _chat


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--backend",
        choices=("codex", "openai"),
        default="codex",
        help="extractor transport: codex = local codex CLI subprocess (default, no OpenAI key needed); openai = OpenAI chat (per-token cost)",
    )
    ap.add_argument("--model", default=None, help="extractor model (codex: default device-auth model; openai: gpt-4o-mini)")
    ap.add_argument("--goldset", type=Path, default=DEFAULT_GOLDSET)
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--out", type=Path, default=None, help="write the JSON report here")
    args = ap.parse_args(argv)

    if not args.out:
        # HARD CONSTRAINT compliance: printing the full report to stdout would put metric values on
        # stdout when --out is omitted. Require --out for a real run; only tests call run_eval()
        # directly and handle the dict themselves.
        print("no --out given: report NOT printed (numeric output must land under eval/out/ only); pass --out eval/out/<name>.json", file=sys.stderr)
        return 2
    out_path = assert_eval_out(args.out)

    chat_fn = _codex_chat_fn(args.model) if args.backend == "codex" else _openai_chat_fn(args.model or "gpt-4o-mini")
    result = run_eval(chat_fn, goldset_path=args.goldset, fixture_path=args.fixture)
    text = json.dumps(result, indent=2, sort_keys=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {out_path} (verdict + numbers inside; not printed to stdout)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
