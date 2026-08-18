"""Deterministic, bounded candidate choice-set assembly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction

from .blocking_constants import BLOCKING_STOPWORDS, SCORING_DROP
from .normalize import resolver_normalize
from .registry import RegistrySlot, SlotRegistry
from .types import BlockingReceipt, ResolveRequest, RiskEvidence, SlotView

CHOICE_SET_K = 8  # sweep seed — NOT frozen; chief freezes via follow-up commit after the K1-c §4.4 sweep (selection evidence required)
THETA = (3, 20)  # sweep seed — NOT frozen; chief freezes via follow-up commit after the K1-c §4.4 sweep (selection evidence required)
MAX_FORCED = 8  # frozen; sole overflow anchor (E2); decoupled from CHOICE_SET_K


def blocking_rules_canonical(k: int, theta: tuple[int, int]) -> str:
    """Return the frozen content-addressed blocking-rules representation."""
    return json.dumps({"choice_set_k": k, "max_forced": MAX_FORCED, "theta": list(theta), "weights": [3, 2, 1], "blocking_stopwords": sorted(BLOCKING_STOPWORDS), "trigram": {"n": 3, "framing": "^$"}}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def blocking_rules_hash_for(k: int, theta: tuple[int, int]) -> str:
    return hashlib.sha256(blocking_rules_canonical(k, theta).encode("utf-8")).hexdigest()


BLOCKING_RULES_HASH = blocking_rules_hash_for(CHOICE_SET_K, THETA)


@dataclass(frozen=True)
class BlockingResult:
    receipt: BlockingReceipt
    choices: tuple[SlotView, ...]
    forced_overflow: bool


def _tokens(value: str) -> set[str]:
    return set(filter(None, resolver_normalize(value).split("_")))


def request_candidate_tokens(request: ResolveRequest) -> set[str]:
    """Return normalized three-surface candidate tokens with blocking words removed."""
    return (_tokens(request.raw_domain) | _tokens(request.value) | _tokens(request.evidence_text)) - BLOCKING_STOPWORDS


def _grams(tokens: set[str]) -> set[str]:
    return {framed[index : index + 3] for token in tokens for framed in (f"^{token}$",) for index in range(len(framed) - 2)}


def score_components(slot: RegistrySlot, cand_tokens: set[str]) -> tuple[int, int, int]:
    """Return ``(A, b_num, b_den)`` for already filtering-free candidate tokens."""
    l1 = _tokens(slot.domain) - SCORING_DROP
    l2 = set().union(*(_tokens(alias) for alias in slot.aliases)) - SCORING_DROP if slot.aliases else set()
    l3 = (_tokens(slot.description) - SCORING_DROP) | set(slot.blocking_terms)
    a = 3 * len(cand_tokens & l1) + 2 * len(cand_tokens & l2) + len(cand_tokens & l3)
    cand_grams, slot_grams = _grams(cand_tokens), _grams(l1 | l2 | l3)
    denominator = len(cand_grams) + len(slot_grams)
    return (a, 2 * len(cand_grams & slot_grams), denominator) if denominator else (a, 0, 0)


def _choice_hash(choice_set: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(choice_set).encode("utf-8")).hexdigest()


def assemble_blocking(request: ResolveRequest, registry: SlotRegistry, risk_evidence: RiskEvidence, guarded_target_id: str | None = None, *, choice_set_k: int = CHOICE_SET_K, theta: tuple[int, int] = THETA) -> BlockingResult:
    """Build the forced-first K1 choice set and receipt without side effects."""
    forced = tuple(sorted(set(risk_evidence.matched_hr_slot_ids) | ({guarded_target_id} if guarded_target_id else set())))
    if len(forced) > MAX_FORCED:
        receipt = BlockingReceipt(forced, _choice_hash(forced), "hr", forced)
        return BlockingResult(receipt, (), True)
    candidate_tokens = request_candidate_tokens(request)
    ranked = []
    for slot in registry.slots:
        if slot.slot_id in forced:
            continue
        a, b_num, b_den = score_components(slot, candidate_tokens)
        if a > 0 or b_num * theta[1] >= b_den * theta[0]:
            ranked.append((a, b_num, b_den, slot.slot_id))
    ranked.sort(key=lambda item: (-item[0], -Fraction(item[1], item[2] or 1), item[3]))
    choice_ids = forced + tuple(slot_id for _, _, _, slot_id in ranked[: choice_set_k - len(forced)])
    choice_slots = tuple(registry.by_id[slot_id] for slot_id in choice_ids)
    slice_name = "hr" if risk_evidence.any or any(slot.high_risk for slot in choice_slots) else "benign"
    receipt = BlockingReceipt(choice_ids, _choice_hash(choice_ids), slice_name, forced)
    views = tuple(SlotView(slot.slot_id, slot.description, slot.high_risk, slot.deny_neighbor_note) for slot in choice_slots)
    return BlockingResult(receipt, views, False)
