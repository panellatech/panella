"""Pure implementation of the K1 high-risk deterministic-hit gate."""

from __future__ import annotations

from .registry import RegistrySlot
from .types import RiskEvidence


def requires_hr_escalation(target: RegistrySlot | None, risk_evidence: RiskEvidence) -> bool:
    # A high-risk deterministic target may bypass escalation only when it is the sole
    # high-risk evidence. Any other matched high-risk slot is competing evidence.
    return target is not None and risk_evidence.any and risk_evidence.matched_hr_slot_ids != (target.slot_id,)
