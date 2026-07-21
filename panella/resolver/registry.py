"""Fail-closed loader for the pinned K1 v2 slot registry and taxonomy."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from .blocking_constants import BLOCKING_STOPWORDS
from .normalize import extractor_normalize_domain, resolver_normalize

MIN_REGISTRY_SLOTS = 90
# The v2 pin is sha256(slot-registry canonical hash + ':' + taxonomy canonical hash).
PINNED_REGISTRY_HASH = "fb9775bad41e9990aebbb1415153ded3ce5f53f97922f008f73ccaa6b985c2d7"
_KINDS = frozenset({"preference", "fact", "constraint"})
_REQUIRED_SLOT_FIELDS = frozenset({
    "id", "kind", "domain", "description", "high_risk", "aliases", "taxonomy_domain", "blocking_terms",
})
_ALLOWED_SLOT_FIELDS = _REQUIRED_SLOT_FIELDS | frozenset({"deny_neighbors", "hr_lexicon"})
_GOVERNANCE_OPS = frozenset({"add_alias", "remove_alias", "add_domain", "remove_domain"})
_GOVERNANCE_REASONS = frozenset({"ambiguity", "retirement", "superseded_by_pair"})


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
    taxonomy_domain: str
    blocking_terms: tuple[str, ...]


@dataclass(frozen=True)
class SlotRegistry:
    version: str
    slots: tuple[RegistrySlot, ...]
    content_hash: str
    slot_registry_hash: str
    taxonomy_hash: str
    by_id: Mapping[str, RegistrySlot]
    alias_raw: Mapping[str, RegistrySlot]
    alias_folded: Mapping[str, RegistrySlot]


def _canonical_hash(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_registry_content_hash(content: Mapping[str, Any]) -> str:
    """Return the v1-compatible canonical hash of slot_registry.yaml content."""
    return _canonical_hash(content)


def canonical_taxonomy_content_hash(content: Mapping[str, Any]) -> str:
    """Return the canonical hash of taxonomy.yaml content."""
    return _canonical_hash(content)


def composite_registry_hash(slot_registry_hash: str, taxonomy_hash: str) -> str:
    if not all(isinstance(value, str) and len(value) == 64 for value in (slot_registry_hash, taxonomy_hash)):
        raise ValueError("registry component hashes must be 64-character strings")
    return hashlib.sha256(f"{slot_registry_hash}:{taxonomy_hash}".encode()).hexdigest()


def canonical_blocking_terms_hash(terms: list[str]) -> str:
    """Hash sorted canonical blocking terms using the frozen field encoding."""
    canonical = sorted(terms)
    return hashlib.sha256(json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def default_registry_path() -> Path:
    return Path(__file__).parent / "slot_registry.yaml"


def default_taxonomy_path() -> Path:
    return Path(__file__).parent / "taxonomy.yaml"


def default_governance_path() -> Path:
    return Path(__file__).parent / "alias_governance.yaml"


def _fail(message: str) -> None:
    raise ValueError(f"invalid slot registry: {message}")


def _load_yaml(path: Path, *, label: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid {label}: cannot load {path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"invalid {label}: root must be a mapping")
    return loaded


def _str_list(value: object, field_name: str, slot_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        _fail(f"{slot_id}.{field_name} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        _fail(f"{slot_id}.{field_name} contains duplicates")
    return tuple(value)


def _tokens(value: str) -> set[str]:
    return set(filter(None, resolver_normalize(value).split("_")))


def _validate_taxonomy(document: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    if set(document) != {"version", "domains"} or not isinstance(document["version"], str) or not document["version"]:
        raise ValueError("invalid taxonomy: root must contain exactly non-empty version and domains")
    domains = document["domains"]
    if not isinstance(domains, dict) or not domains:
        raise ValueError("invalid taxonomy: domains must be a non-empty mapping")
    total = 0
    for name, descriptor in domains.items():
        if not isinstance(name, str) or not name or not isinstance(descriptor, dict) or set(descriptor) != {"min_slots", "provenance"}:
            raise ValueError("invalid taxonomy: malformed domain descriptor")
        minimum, provenance = descriptor["min_slots"], descriptor["provenance"]
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
            raise ValueError(f"invalid taxonomy: {name}.min_slots must be a positive int")
        if not isinstance(provenance, str) or not provenance or not (provenance.startswith("generic:") or provenance.startswith("findings:")):
            raise ValueError(f"invalid taxonomy: {name}.provenance is invalid")
        total += minimum
    if total < MIN_REGISTRY_SLOTS:
        raise ValueError(f"invalid taxonomy: minima must sum to at least {MIN_REGISTRY_SLOTS}")
    return domains


def canonical_governance_hash(document: Mapping[str, Any]) -> str:
    """Hash a governance document after the frozen op ordering is normalised."""
    if not isinstance(document, Mapping) or set(document) != {"baseline_registry_hash", "ops"}:
        raise ValueError("invalid alias governance: root must contain baseline_registry_hash and ops")
    ops = document["ops"]
    if not isinstance(ops, list):
        raise ValueError("invalid alias governance: ops must be a list")
    ordered = sorted(ops, key=lambda op: (op.get("op", ""), op.get("surface", ""), op.get("to_slot") or op.get("from_slot") or "")) if all(isinstance(op, dict) for op in ops) else ops
    return _canonical_hash({"baseline_registry_hash": document["baseline_registry_hash"], "ops": ordered})


def validate_alias_governance(
    document: Mapping[str, Any],
    *,
    repository_root: Path | None = None,
    baseline_document: Mapping[str, Any] | None = None,
    current_document: Mapping[str, Any] | None = None,
) -> None:
    """Validate governance schema and, when supplied, reconcile its alias/domain diff."""
    if set(document) != {"baseline_registry_hash", "ops"}:
        raise ValueError("invalid alias governance: root fields")
    baseline, ops = document["baseline_registry_hash"], document["ops"]
    if not isinstance(baseline, str) or len(baseline) != 64 or any(ch not in "0123456789abcdef" for ch in baseline):
        raise ValueError("invalid alias governance: baseline_registry_hash")
    if not isinstance(ops, list):
        raise ValueError("invalid alias governance: ops")
    seen: set[tuple[str, str, str]] = set()
    pairs: dict[str, list[dict[str, Any]]] = {}
    root = repository_root or Path(__file__).resolve().parents[2]
    for entry in ops:
        if not isinstance(entry, dict):
            raise ValueError("invalid alias governance: op must be a mapping")
        op, surface = entry.get("op"), entry.get("surface")
        if op not in _GOVERNANCE_OPS or not isinstance(surface, str) or not surface:
            raise ValueError("invalid alias governance: op or surface")
        is_add = op.startswith("add_")
        required = {"op", "surface", "to_slot", "rationale"} if is_add else {"op", "surface", "from_slot", "rationale", "reason"}
        allowed = required | {"pair_id"} | ({"fixture_id"} if not is_add else set())
        if set(entry) - allowed or not required <= set(entry):
            raise ValueError("invalid alias governance: fields for op")
        prohibited = "from_slot" if is_add else "to_slot"
        if prohibited in entry or not isinstance(entry["rationale"], str) or not entry["rationale"]:
            raise ValueError("invalid alias governance: slot direction or rationale")
        slot = entry.get("to_slot") if is_add else entry.get("from_slot")
        if not isinstance(slot, str) or not slot:
            raise ValueError("invalid alias governance: slot id")
        key = (op, surface, slot)
        if key in seen:
            raise ValueError("invalid alias governance: duplicate delta key")
        seen.add(key)
        pair_id = entry.get("pair_id")
        if pair_id is not None:
            if not isinstance(pair_id, str) or not pair_id:
                raise ValueError("invalid alias governance: pair_id")
            pairs.setdefault(pair_id, []).append(entry)
        if not is_add:
            reason = entry["reason"]
            if reason not in _GOVERNANCE_REASONS:
                raise ValueError("invalid alias governance: reason")
            fixture = entry.get("fixture_id")
            if reason == "ambiguity":
                if not isinstance(fixture, str) or not fixture or not (root / fixture).is_file():
                    raise ValueError("invalid alias governance: ambiguity requires an existing fixture_id")
            elif fixture is not None:
                raise ValueError("invalid alias governance: fixture_id is limited to ambiguity")
            if (pair_id is not None) != (reason == "superseded_by_pair"):
                raise ValueError("invalid alias governance: paired removes require superseded_by_pair")
    for pair_id, entries in pairs.items():
        if len(entries) != 2 or sum(entry["op"].startswith("remove_") for entry in entries) != 1:
            raise ValueError(f"invalid alias governance: pair {pair_id} must have one add and one remove")
        kinds = {entry["op"].split("_", 1)[1] for entry in entries}
        if len(kinds) != 1:
            raise ValueError(f"invalid alias governance: pair {pair_id} changes different families")
        if len({entry["surface"] for entry in entries}) != 1 and len({entry.get("to_slot") or entry.get("from_slot") for entry in entries}) != 1:
            raise ValueError(f"invalid alias governance: pair {pair_id} has no shared surface or slot")
    if (baseline_document is None) != (current_document is None):
        raise ValueError("invalid alias governance: baseline and current documents are required together")
    if baseline_document is not None and current_document is not None:
        def projection(source: Mapping[str, Any]) -> set[tuple[str, str, str]]:
            slots = source.get("slots")
            if not isinstance(slots, list):
                raise ValueError("invalid alias governance: diff document slots")
            result: set[tuple[str, str, str]] = set()
            for slot in slots:
                if not isinstance(slot, Mapping) or not isinstance(slot.get("id"), str) or not isinstance(slot.get("domain"), str) or not isinstance(slot.get("aliases"), list):
                    raise ValueError("invalid alias governance: diff document slot")
                result.add(("domain", slot["domain"], slot["id"]))
                result.update(("alias", alias, slot["id"]) for alias in slot["aliases"] if isinstance(alias, str))
            return result
        baseline_projection, current_projection = projection(baseline_document), projection(current_document)
        expected = {(f"remove_{family}", surface, slot) for family, surface, slot in baseline_projection - current_projection}
        expected.update((f"add_{family}", surface, slot) for family, surface, slot in current_projection - baseline_projection)
        actual = {(entry["op"], entry["surface"], entry.get("to_slot") or entry.get("from_slot")) for entry in ops}
        if actual != expected:
            raise ValueError("invalid alias governance: operations are not a bijection to alias/domain diff")


def load_registry(
    path: Path | str | None = None,
    *,
    expected_hash: str | None = PINNED_REGISTRY_HASH,
    taxonomy_path: Path | str | None = None,
    governance_path: Path | str | None = None,
) -> SlotRegistry:
    """Load, validate, and pin-check the v2 composite registry identity."""
    registry_path = default_registry_path() if path is None else Path(path)
    taxonomy_file = default_taxonomy_path() if taxonomy_path is None else Path(taxonomy_path)
    loaded = _load_yaml(registry_path, label="slot registry")
    taxonomy = _load_yaml(taxonomy_file, label="taxonomy")
    if set(loaded) != {"version", "slots"} or not isinstance(loaded["version"], str) or not loaded["version"]:
        _fail("root must contain exactly non-empty version and slots")
    if not isinstance(loaded["slots"], list):
        _fail("slots must be a list")
    domains = _validate_taxonomy(taxonomy)
    slot_hash = canonical_registry_content_hash(loaded)
    taxonomy_hash = canonical_taxonomy_content_hash(taxonomy)
    content_hash = composite_registry_hash(slot_hash, taxonomy_hash)
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
    counts: Counter[str] = Counter()
    for index, raw_slot in enumerate(loaded["slots"]):
        if not isinstance(raw_slot, dict):
            _fail(f"slot {index} must be a mapping")
        fields = set(raw_slot)
        if not fields >= _REQUIRED_SLOT_FIELDS or not fields <= _ALLOWED_SLOT_FIELDS:
            _fail(f"slot {index} has missing or unsupported fields")
        slot_id, kind, domain, description = (raw_slot[name] for name in ("id", "kind", "domain", "description"))
        high_risk, taxonomy_domain = raw_slot["high_risk"], raw_slot["taxonomy_domain"]
        if not all(isinstance(value, str) and value for value in (slot_id, kind, domain, description)):
            _fail(f"slot {index} has invalid string fields")
        if kind not in _KINDS or slot_id != f"{kind}:{domain}":
            _fail(f"{slot_id} must equal kind:domain")
        if not isinstance(taxonomy_domain, str) or taxonomy_domain not in domains:
            _fail(f"{slot_id}.taxonomy_domain is unknown")
        if domain.startswith("xunres_") or extractor_normalize_domain(domain) != domain or resolver_normalize(domain) != domain:
            _fail(f"{slot_id} has a non-canonical or reserved domain")
        if _tokens(domain) & BLOCKING_STOPWORDS:
            _fail(f"{slot_id} domain contains a blocking stopword")
        if not isinstance(high_risk, bool):
            _fail(f"{slot_id}.high_risk must be bool")
        aliases = _str_list(raw_slot["aliases"], "aliases", slot_id)
        if any(alias.startswith("xunres_") or extractor_normalize_domain(alias) != alias for alias in aliases):
            _fail(f"{slot_id} has a non-canonical or reserved alias")
        if any(_tokens(alias) & BLOCKING_STOPWORDS for alias in aliases):
            _fail(f"{slot_id} alias contains a blocking stopword")
        blocking_terms = _str_list(raw_slot["blocking_terms"], "blocking_terms", slot_id)
        if len(blocking_terms) > 24 or tuple(sorted(blocking_terms)) != blocking_terms:
            _fail(f"{slot_id}.blocking_terms must be sorted and capped")
        surfaces = _tokens(domain) | set().union(*(_tokens(alias) for alias in aliases)) if aliases else _tokens(domain)
        for term in blocking_terms:
            if term != resolver_normalize(term) or "_" in term or term in BLOCKING_STOPWORDS or term in surfaces:
                _fail(f"{slot_id} has an invalid blocking term {term}")
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
        slot = RegistrySlot(slot_id, kind, domain, description, high_risk, aliases, deny_neighbors, hr_lexicon, note, taxonomy_domain, blocking_terms)
        by_id[slot_id] = slot
        slots.append(slot)
        counts[taxonomy_domain] += 1
        for surface in (domain, *aliases):
            folded = resolver_normalize(surface)
            prior_raw = all_raw.setdefault(surface, slot_id)
            prior_folded = all_folded.setdefault(folded, slot_id)
            if prior_raw != slot_id or prior_folded != slot_id:
                _fail(f"alias two-form collision for {surface}")
        raw_alias_values.extend((alias, slot_id) for alias in aliases)
        folded_alias_values.extend((resolver_normalize(alias), slot_id) for alias in aliases)
    for name, descriptor in domains.items():
        if counts[name] < descriptor["min_slots"]:
            _fail(f"taxonomy domain {name} has fewer than {descriptor['min_slots']} slots")
    domain_to_slot = {slot.domain: slot for slot in slots}
    for slot in slots:
        for neighbor in slot.deny_neighbors:
            target = domain_to_slot.get(neighbor)
            if target is None or target.high_risk:
                _fail(f"{slot.slot_id} has dangling or high-risk deny_neighbor {neighbor}")
    governance_file = default_governance_path() if governance_path is None else Path(governance_path)
    if governance_file.is_file():
        validate_alias_governance(_load_yaml(governance_file, label="alias governance"))
    alias_raw = {surface: by_id[slot_id] for surface, slot_id in raw_alias_values}
    alias_folded = {surface: by_id[slot_id] for surface, slot_id in folded_alias_values}
    return SlotRegistry(loaded["version"], tuple(slots), content_hash, slot_hash, taxonomy_hash, MappingProxyType(by_id), MappingProxyType(alias_raw), MappingProxyType(alias_folded))
