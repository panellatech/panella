"""Deterministic, bounded candidate choice-set assembly."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .normalize import resolver_normalize
from .registry import RegistrySlot, SlotRegistry
from .types import BlockingReceipt, ResolveRequest, RiskEvidence, SlotView

CHOICE_SET_K = 8


@dataclass(frozen=True)
class BlockingResult:
    receipt: BlockingReceipt
    choices: tuple[SlotView, ...]
    forced_overflow: bool


def _tokens(value: str) -> set[str]:
    return set(filter(None, resolver_normalize(value).split("_")))


def _slot_score(slot: RegistrySlot, candidate_tokens: set[str]) -> int:
    domain_tokens = _tokens(slot.domain)
    alias_tokens = set().union(*(_tokens(alias) for alias in slot.aliases)) if slot.aliases else set()
    description_tokens = _tokens(slot.description)
    return (
        3 * len(candidate_tokens & domain_tokens)
        + 2 * len(candidate_tokens & alias_tokens)
        + len(candidate_tokens & description_tokens)
    )


def _choice_hash(choice_set: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(choice_set).encode("utf-8")).hexdigest()


def assemble_blocking(
    request: ResolveRequest,
    registry: SlotRegistry,
    risk_evidence: RiskEvidence,
    guarded_target_id: str | None = None,
) -> BlockingResult:
    """Build the forced-first K1 choice set and receipt without side effects."""
    forced = tuple(sorted(set(risk_evidence.matched_hr_slot_ids) | ({guarded_target_id} if guarded_target_id else set())))
    if len(forced) > CHOICE_SET_K:
        receipt = BlockingReceipt(forced, _choice_hash(forced), "hr")
        return BlockingResult(receipt, (), True)
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
    choice_ids = forced + tuple(slot_id for _, slot_id in ranked[: CHOICE_SET_K - len(forced)])
    choice_slots = tuple(registry.by_id[slot_id] for slot_id in choice_ids)
    slice_name = "hr" if risk_evidence.any or any(slot.high_risk for slot in choice_slots) else "benign"
    receipt = BlockingReceipt(choice_ids, _choice_hash(choice_ids), slice_name)
    views = tuple(SlotView(slot.slot_id, slot.description, slot.high_risk, slot.deny_neighbor_note) for slot in choice_slots)
    return BlockingResult(receipt, views, False)
