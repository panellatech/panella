"""Deterministic, bounded candidate choice-set assembly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from . import normalize
from .blocking_constants import BLOCKING_STOPWORDS
from .normalize import resolver_normalize
from .registry import RegistrySlot, SlotRegistry
from .types import BlockingReceipt, ResolveRequest, RiskEvidence, SlotView

V1_CHOICE_SET_K = 8
V2_INSTRUMENT_SEED_K = 8  # v2-instrument sweep seed — NOT a production setting.
THETA = (3, 20)  # sweep seed — NOT frozen; chief freezes via follow-up commit after the K1-c §4.4 sweep (selection evidence required)
MAX_FORCED = 8  # frozen; sole overflow anchor (E2); decoupled from V2_INSTRUMENT_SEED_K
V1_WEIGHTS = (3, 2, 1)


def blocking_v2_instrument_rules_canonical(k: int, theta: tuple[int, int]) -> str:
    """Return the frozen content-addressed blocking-rules representation."""
    return json.dumps(
        {
            "choice_set_k": k,
            "max_forced": MAX_FORCED,
            "theta": list(theta),
            "weights": [3, 2, 1],
            "normalizer_rules_hash": normalize.compute_normalizer_rules_hash(),
            "blocking_stopwords": sorted(BLOCKING_STOPWORDS),
            "scoring_drop": sorted(_scoring_drop()),
            "surfaces": {
                "l1": "domain - scoring_drop",
                "l2": "aliases - scoring_drop",
                "l3": "(description - scoring_drop) ∪ blocking_terms",
                "cand": "raw_domain|value|evidence - blocking_stopwords",
            },
            "trigram": {"n": 3, "framing": "^$"},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def blocking_v2_instrument_rules_hash_for(k: int, theta: tuple[int, int]) -> str:
    return hashlib.sha256(blocking_v2_instrument_rules_canonical(k, theta).encode("utf-8")).hexdigest()


def blocking_v1_operative_rules_canonical() -> str:
    """Return the canonical description of the production v1 blocker."""
    return json.dumps(
        {
            "choice_set": {
                "limit": {"base": V1_CHOICE_SET_K, "subtract": "forced_count"},
                "ranked": {"eligible": "score_gt_zero", "order": ["score_desc", "slot_id_asc"]},
            },
            "forced": {
                "deduplicate": True,
                "order": "slot_id_asc",
                "prefix": True,
                "sources": ["risk_matched_hr_slot_ids", "guarded_target_id"],
            },
            "overflow": {
                "if_forced_count_gt": MAX_FORCED,
                "result": {"choice_set": "empty", "receipt_forced_ids": "forced"},
            },
            "scoring": {
                "candidate_surfaces": ["raw_domain", "value", "evidence_text"],
                "eligibility": "score_gt_zero",
                "weights": list(V1_WEIGHTS),
            },
            "slice": {"hr_if_any": ["risk_evidence_any", "choice_set_has_high_risk"]},
            "tokens": {
                "blocking_terms": False,
                "normalizer": "resolver_normalize",
                "normalizer_rules_hash": normalize.compute_normalizer_rules_hash(),
                "split": "underscore",
                "stopwords": False,
                "trigram": False,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def blocking_v1_operative_rules_hash() -> str:
    return hashlib.sha256(blocking_v1_operative_rules_canonical().encode("utf-8")).hexdigest()


# The production binding is intentionally v1-only.  v2 hashes belong solely to diagnostic artifacts.
BLOCKING_RULES_HASH = blocking_v1_operative_rules_hash()


@dataclass(frozen=True)
class BlockingResult:
    receipt: BlockingReceipt
    choices: tuple[SlotView, ...]
    forced_overflow: bool


def _tokens(value: str) -> set[str]:
    return set(filter(None, resolver_normalize(value).split("_")))


def _scoring_drop() -> frozenset[str]:
    """Return the live v2 scoring vocabulary excluded after normalization."""
    return frozenset(normalize.STOPWORDS) | BLOCKING_STOPWORDS


def _slot_score(slot: RegistrySlot, candidate_tokens: set[str]) -> int:
    """Return the original v1 3/2/1 surface-overlap score."""
    domain_tokens = _tokens(slot.domain)
    alias_tokens = set().union(*(_tokens(alias) for alias in slot.aliases)) if slot.aliases else set()
    description_tokens = _tokens(slot.description)
    return (
        V1_WEIGHTS[0] * len(candidate_tokens & domain_tokens)
        + V1_WEIGHTS[1] * len(candidate_tokens & alias_tokens)
        + V1_WEIGHTS[2] * len(candidate_tokens & description_tokens)
    )


def request_candidate_tokens(request: ResolveRequest) -> set[str]:
    """Return normalized three-surface candidate tokens with blocking words removed."""
    return (_tokens(request.raw_domain) | _tokens(request.value) | _tokens(request.evidence_text)) - BLOCKING_STOPWORDS


def _grams(tokens: set[str]) -> set[str]:
    return {framed[index : index + 3] for token in tokens for framed in (f"^{token}$",) for index in range(len(framed) - 2)}


def score_components(slot: RegistrySlot, cand_tokens: set[str]) -> tuple[int, int, int]:
    """Return ``(A, b_num, b_den)`` for already filtering-free candidate tokens."""
    scoring_drop = _scoring_drop()
    l1 = _tokens(slot.domain) - scoring_drop
    l2 = set().union(*(_tokens(alias) for alias in slot.aliases)) - scoring_drop if slot.aliases else set()
    l3 = (_tokens(slot.description) - scoring_drop) | set(slot.blocking_terms)
    a = 3 * len(cand_tokens & l1) + 2 * len(cand_tokens & l2) + len(cand_tokens & l3)
    cand_grams, slot_grams = _grams(cand_tokens), _grams(l1 | l2 | l3)
    denominator = len(cand_grams) + len(slot_grams)
    return (a, 2 * len(cand_grams & slot_grams), denominator) if denominator else (a, 0, 0)


def _choice_hash(choice_set: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(choice_set).encode("utf-8")).hexdigest()


def assemble_blocking(
    request: ResolveRequest,
    registry: SlotRegistry,
    risk_evidence: RiskEvidence,
    guarded_target_id: str | None = None,
    *,
    scoring_mode: Literal["v1", "v2_instrument"] = "v1",
    choice_set_k: int | None = None,
    theta: tuple[int, int] | None = None,
) -> BlockingResult:
    """Build the forced-first K1 choice set and receipt without side effects."""
    if scoring_mode == "v1":
        if choice_set_k is not None or theta is not None:
            raise ValueError("v1 scoring does not accept instrument parameters")
    elif scoring_mode == "v2_instrument":
        if choice_set_k is None or theta is None:
            raise ValueError("v2_instrument scoring requires choice_set_k and theta")
    else:
        raise ValueError(f"unsupported blocking scoring mode: {scoring_mode}")

    forced = tuple(sorted(set(risk_evidence.matched_hr_slot_ids) | ({guarded_target_id} if guarded_target_id else set())))
    if len(forced) > MAX_FORCED:
        receipt = BlockingReceipt(forced, _choice_hash(forced), "hr", forced)
        return BlockingResult(receipt, (), True)

    if scoring_mode == "v1":
        candidate_tokens = _tokens(request.raw_domain) | _tokens(request.value) | _tokens(request.evidence_text)
        ranked = sorted(
            (
                (score, slot.slot_id)
                for slot in registry.slots
                if slot.slot_id not in forced
                if (score := _slot_score(slot, candidate_tokens)) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        choice_ids = forced + tuple(slot_id for _, slot_id in ranked[: V1_CHOICE_SET_K - len(forced)])
    elif scoring_mode == "v2_instrument":
        candidate_tokens = request_candidate_tokens(request)
        ranked_v2 = []
        for slot in registry.slots:
            if slot.slot_id in forced:
                continue
            a, b_num, b_den = score_components(slot, candidate_tokens)
            if a > 0 or b_num * theta[1] >= b_den * theta[0]:
                ranked_v2.append((a, b_num, b_den, slot.slot_id))
        ranked_v2.sort(key=lambda item: (-item[0], -Fraction(item[1], item[2] or 1), item[3]))
        choice_ids = forced + tuple(slot_id for _, _, _, slot_id in ranked_v2[: choice_set_k - len(forced)])
    choice_slots = tuple(registry.by_id[slot_id] for slot_id in choice_ids)
    slice_name = "hr" if risk_evidence.any or any(slot.high_risk for slot in choice_slots) else "benign"
    receipt = BlockingReceipt(choice_ids, _choice_hash(choice_ids), slice_name, forced)
    views = tuple(SlotView(slot.slot_id, slot.description, slot.high_risk, slot.deny_neighbor_note) for slot in choice_slots)
    return BlockingResult(receipt, views, False)
