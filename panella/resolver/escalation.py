"""Pure implementation of the K1 high-risk deterministic-hit gate."""

from __future__ import annotations

from .registry import RegistrySlot
from .types import RiskEvidence


def requires_hr_escalation(target: RegistrySlot | None, risk_evidence: RiskEvidence) -> bool:
    return target is not None and risk_evidence.any and target.slot_id not in risk_evidence.matched_hr_slot_ids
