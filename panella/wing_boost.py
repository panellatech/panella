"""Wing-bias soft-boost for memory ranking.

Extracted into its own module so the live Panella retrieval path
(``panella.panella_adapter``) can apply the wing soft-boost WITHOUT pulling in
the heavy embedding/numpy dependencies of the former in-process hybrid retriever
(removed in the 2026-06-30 legacy prune). This module has zero legacy/panella-infra
coupling — stdlib only, with ``yaml`` loaded lazily inside ``_query_patterns``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
QUERY_PATTERNS_PATH = ROOT / "config" / "memory_query_patterns.yaml"
HINT_WING_BOOST_FACTOR = 1.5
OUT_OF_HINT_WING_FACTOR = 0.7
DEFAULT_WING_FACTOR = 1.0


@dataclass(frozen=True)
class WingBias:
    target_wings: tuple[str, ...]
    downweight_wings: tuple[str, ...]
    source: str
    factors: dict[str, float]
    downweight_all_others: bool = False


def resolve_wing_bias(query: str, wings_hint: list[str] | tuple[str, ...] | None = None) -> WingBias:
    """Resolve caller-provided or inferred wing intent for ranking."""

    hinted = _normalize_wings(wings_hint or [])
    if hinted:
        return _wing_bias(hinted, (), "goldset", downweight_all_others=True)

    inferred, downweighted = infer_wings(query)
    if inferred:
        return _wing_bias(inferred, downweighted, "inferred", downweight_all_others=False)

    return WingBias(target_wings=(), downweight_wings=(), source="none", factors={})


def infer_wings(query: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Infer likely target wings from configured keyword patterns."""

    matched_boosts: list[str] = []
    matched_downweights: list[str] = []
    for rule in _query_patterns():
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        try:
            matched = re.search(pattern, query or "") is not None
        except re.error:
            continue
        if matched:
            matched_boosts.extend(str(wing) for wing in rule.get("boost_wings", []))
            matched_downweights.extend(str(wing) for wing in rule.get("downweight_wings", []))
    return _normalize_wings(matched_boosts), _normalize_wings(matched_downweights)


def wing_boost_factor(hit_wing: str, wing_bias: WingBias) -> float:
    """Return the multiplicative ranking factor for a hit wing."""

    hit = str(hit_wing or "").strip()
    if not hit or wing_bias.source == "none":
        return DEFAULT_WING_FACTOR
    if hit in wing_bias.factors:
        return wing_bias.factors[hit]
    return OUT_OF_HINT_WING_FACTOR if wing_bias.downweight_all_others else DEFAULT_WING_FACTOR


def wing_boost(hit_wing: str, wings_hint: list[str] | tuple[str, ...] | str | None) -> float:
    """Backward-compatible helper returning the boost factor for tests/callers."""

    if isinstance(wings_hint, str):
        hint_list: list[str] | tuple[str, ...] | None = [wings_hint]
    else:
        hint_list = wings_hint
    return wing_boost_factor(hit_wing, resolve_wing_bias("", hint_list))


def _wing_bias(
    target_wings: tuple[str, ...],
    downweight_wings: tuple[str, ...],
    source: str,
    *,
    downweight_all_others: bool,
) -> WingBias:
    factors: dict[str, float] = {wing: HINT_WING_BOOST_FACTOR for wing in target_wings}
    for wing in downweight_wings:
        factors.setdefault(wing, OUT_OF_HINT_WING_FACTOR)
    return WingBias(
        target_wings=target_wings,
        downweight_wings=downweight_wings,
        source=source,
        factors=factors,
        downweight_all_others=downweight_all_others,
    )


def _normalize_wings(wings: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for wing in wings:
        value = str(wing or "").strip()
        if value and value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


@lru_cache(maxsize=1)
def _query_patterns(path: Path = QUERY_PATTERNS_PATH) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ()
    rules = raw.get("wing_inference", [])
    if not isinstance(rules, list):
        return ()
    return tuple(rule for rule in rules if isinstance(rule, dict))
