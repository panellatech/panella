"""Fail-closed loader for the pinned K1 slot registry."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from .normalize import extractor_normalize_domain, resolver_normalize

MIN_REGISTRY_SLOTS = 50
PINNED_REGISTRY_HASH = "f6d44f272dd092a48c9078d8a7f442fa7b11fe350fb4506684bbbfabd2009a84"
_KINDS = frozenset({"preference", "fact", "constraint"})
_REQUIRED_SLOT_FIELDS = frozenset({"id", "kind", "domain", "description", "high_risk", "aliases"})
_ALLOWED_SLOT_FIELDS = _REQUIRED_SLOT_FIELDS | frozenset({"deny_neighbors", "hr_lexicon"})


@dataclass(frozen=True)
class RegistrySlot:
    slot_id: str
    kind: str
    domain: str
    description: str
    high_risk: bool
    aliases: tuple[str, ...]
    deny_neighbors: tuple[str, ...]
    hr_lexicon: tuple[str, ...]
    deny_neighbor_note: str | None


@dataclass(frozen=True)
class SlotRegistry:
    version: str
    slots: tuple[RegistrySlot, ...]
    content_hash: str
    by_id: Mapping[str, RegistrySlot]
    alias_raw: Mapping[str, RegistrySlot]
    alias_folded: Mapping[str, RegistrySlot]


def canonical_registry_content_hash(content: Mapping[str, Any]) -> str:
    """Hash parsed registry content, not source formatting, using canonical JSON."""
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def default_registry_path() -> Path:
    return Path(__file__).parent / "slot_registry.yaml"


def _fail(message: str) -> None:
    raise ValueError(f"invalid slot registry: {message}")


def _str_list(value: object, field_name: str, slot_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        _fail(f"{slot_id}.{field_name} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        _fail(f"{slot_id}.{field_name} contains duplicates")
    return tuple(value)


def load_registry(path: Path | str | None = None, *, expected_hash: str | None = PINNED_REGISTRY_HASH) -> SlotRegistry:
    """Load, validate, and optionally pin-check a registry YAML document."""
    registry_path = default_registry_path() if path is None else Path(path)
    try:
        loaded = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid slot registry: cannot load {registry_path}") from exc
    if not isinstance(loaded, dict) or set(loaded) != {"version", "slots"}:
        _fail("root must contain exactly version and slots")
    if not isinstance(loaded["version"], str) or not loaded["version"]:
        _fail("version must be a non-empty string")
    if not isinstance(loaded["slots"], list):
        _fail("slots must be a list")
    content_hash = canonical_registry_content_hash(loaded)
    if expected_hash is not None and content_hash != expected_hash:
        _fail("content hash does not match the pin")
    if len(loaded["slots"]) < MIN_REGISTRY_SLOTS:
        _fail(f"registry must contain at least {MIN_REGISTRY_SLOTS} slots")

    slots: list[RegistrySlot] = []
    by_id: dict[str, RegistrySlot] = {}
    all_raw: dict[str, str] = {}
    all_folded: dict[str, str] = {}
    raw_alias_values: list[tuple[str, str]] = []
    folded_alias_values: list[tuple[str, str]] = []

    for index, raw_slot in enumerate(loaded["slots"]):
        if not isinstance(raw_slot, dict):
            _fail(f"slot {index} must be a mapping")
        fields = set(raw_slot)
        if not fields >= _REQUIRED_SLOT_FIELDS or not fields <= _ALLOWED_SLOT_FIELDS:
            _fail(f"slot {index} has missing or unsupported fields")
        slot_id = raw_slot["id"]
        kind = raw_slot["kind"]
        domain = raw_slot["domain"]
        description = raw_slot["description"]
        high_risk = raw_slot["high_risk"]
        if not all(isinstance(value, str) and value for value in (slot_id, kind, domain, description)):
            _fail(f"slot {index} has invalid string fields")
        if kind not in _KINDS or slot_id != f"{kind}:{domain}":
            _fail(f"{slot_id} must equal kind:domain")
        if domain.startswith("xunres_") or extractor_normalize_domain(domain) != domain or resolver_normalize(domain) != domain:
            _fail(f"{slot_id} has a non-canonical or reserved domain")
        if not isinstance(high_risk, bool):
            _fail(f"{slot_id}.high_risk must be bool")
        aliases = _str_list(raw_slot["aliases"], "aliases", slot_id)
        if any(alias.startswith("xunres_") or extractor_normalize_domain(alias) != alias for alias in aliases):
            _fail(f"{slot_id} has a non-canonical or reserved alias")
        deny_neighbors = _str_list(raw_slot.get("deny_neighbors", []), "deny_neighbors", slot_id)
        hr_lexicon = _str_list(raw_slot.get("hr_lexicon", []), "hr_lexicon", slot_id)
        if len(deny_neighbors) > 4 or len(hr_lexicon) > 8:
            _fail(f"{slot_id} exceeds a registry list limit")
        if high_risk and not hr_lexicon:
            _fail(f"{slot_id} high-risk slots require hr_lexicon")
        if not high_risk and hr_lexicon:
            _fail(f"{slot_id} non-high-risk slot cannot define hr_lexicon")
        if slot_id in by_id:
            _fail(f"duplicate id {slot_id}")
        note = f"Do not confuse with: {', '.join(deny_neighbors)}." if deny_neighbors else None
        slot = RegistrySlot(slot_id, kind, domain, description, high_risk, aliases, deny_neighbors, hr_lexicon, note)
        by_id[slot_id] = slot
        slots.append(slot)
        for surface in (domain, *aliases):
            folded = resolver_normalize(surface)
            prior_raw = all_raw.setdefault(surface, slot_id)
            prior_folded = all_folded.setdefault(folded, slot_id)
            if prior_raw != slot_id or prior_folded != slot_id:
                _fail(f"alias two-form collision for {surface}")
        raw_alias_values.extend((alias, slot_id) for alias in aliases)
        folded_alias_values.extend((resolver_normalize(alias), slot_id) for alias in aliases)

    domain_to_slot = {slot.domain: slot for slot in slots}
    for slot in slots:
        for neighbor in slot.deny_neighbors:
            target = domain_to_slot.get(neighbor)
            if target is None or target.high_risk:
                _fail(f"{slot.slot_id} has dangling or high-risk deny_neighbor {neighbor}")

    alias_raw = {surface: by_id[slot_id] for surface, slot_id in raw_alias_values}
    alias_folded = {surface: by_id[slot_id] for surface, slot_id in folded_alias_values}
    return SlotRegistry(
        version=loaded["version"],
        slots=tuple(slots),
        content_hash=content_hash,
        by_id=MappingProxyType(by_id),
        alias_raw=MappingProxyType(alias_raw),
        alias_folded=MappingProxyType(alias_folded),
    )
