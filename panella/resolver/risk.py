"""Pre-short-circuit high-risk evidence computation."""

from __future__ import annotations

import re

from .normalize import resolver_normalize
from .registry import SlotRegistry
from .types import ResolveRequest, RiskEvidence


def compute_risk_evidence(request: ResolveRequest, registry: SlotRegistry) -> RiskEvidence:
    raw_domain = request.raw_domain
    folded_domain = resolver_normalize(raw_domain)
    lexical_text = f"{request.value} {request.evidence_text}".lower()
    domain_matches: set[str] = set()
    lexicon_matches: set[str] = set()
    for slot in registry.slots:
        if not slot.high_risk:
            continue
        surfaces = (slot.domain, *slot.aliases)
        if raw_domain in surfaces or folded_domain in {resolver_normalize(surface) for surface in surfaces}:
            domain_matches.add(slot.slot_id)
        if any(re.search(rf"\b{re.escape(stem.lower())}", lexical_text) for stem in slot.hr_lexicon):
            lexicon_matches.add(slot.slot_id)
    return RiskEvidence(
        matched_hr_slot_ids=tuple(sorted(domain_matches | lexicon_matches)),
        domain_hr_hit=bool(domain_matches),
        lexicon_hit=bool(lexicon_matches),
    )
