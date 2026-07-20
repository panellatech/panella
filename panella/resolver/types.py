"""Frozen public contracts for the K1 deterministic resolver."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, is_dataclass
from types import MappingProxyType
from typing import Literal, Mapping, Protocol

_UID_RE = re.compile(r"^[a-z0-9][a-z0-9/_\-.]{0,127}$")
_KINDS = frozenset({"preference", "fact", "constraint"})


def _require_quantized_float(value: object, field_name: str) -> float:
    """Reject floats the six-decimal fitting contract cannot represent exactly."""

    # The fitting algorithm emits 6dp values by construction, so legitimate manifests are always quantized.
    if not isinstance(value, float) or not math.isfinite(value) or round(value, 6) != value:
        raise ValueError(f"{field_name} must be a finite float quantized to six decimal places")
    return value


@dataclass(frozen=True)
class ResolveRequest:
    request_uid: str
    kind: str
    raw_domain: str
    value: str
    evidence_text: str
    effective_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_uid, str) or not _UID_RE.fullmatch(self.request_uid):
            raise ValueError("request_uid must match the resolver UID grammar")
        if self.kind not in _KINDS:
            raise ValueError("kind must be preference, fact, or constraint")
        if not all(isinstance(value, str) for value in (self.raw_domain, self.value, self.evidence_text)):
            raise ValueError("raw_domain, value, and evidence_text must be strings")
        if self.effective_at is not None and not isinstance(self.effective_at, str):
            raise ValueError("effective_at must be a string or None")


@dataclass(frozen=True)
class ExistingSlot:
    slot_id: str
    last_seen: str | None

    def __post_init__(self) -> None:
        split_slot_id(self.slot_id)
        if self.last_seen is not None and not isinstance(self.last_seen, str):
            raise ValueError("last_seen must be a string or None")


@dataclass(frozen=True)
class ResolverContext:
    existing_slots: tuple[ExistingSlot, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.existing_slots, tuple) or not all(
            isinstance(slot, ExistingSlot) for slot in self.existing_slots
        ):
            raise ValueError("existing_slots must be a tuple of ExistingSlot values")


@dataclass(frozen=True)
class SlotView:
    slot_id: str
    description: str
    high_risk: bool
    deny_neighbor_note: str | None


@dataclass(frozen=True)
class CalibrationSlice:
    n_samples: int
    per_bin: tuple[int, ...]
    mapping: tuple[tuple[float, float, float], ...]
    tau: float

    def __post_init__(self) -> None:
        if not isinstance(self.n_samples, int) or isinstance(self.n_samples, bool):
            raise ValueError("n_samples must be an integer")
        if not isinstance(self.per_bin, (list, tuple)):
            raise ValueError("per_bin must be a tuple or list")
        if not isinstance(self.mapping, (list, tuple)):
            raise ValueError("mapping must be a tuple or list")

        per_bin = tuple(self.per_bin)
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in per_bin):
            raise ValueError("per_bin must contain integers")
        mapping: list[tuple[float, float, float]] = []
        for index, row in enumerate(self.mapping):
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                raise ValueError("each calibration mapping row must contain exactly three floats")
            low = _require_quantized_float(row[0], f"mapping[{index}].low")
            high = _require_quantized_float(row[1], f"mapping[{index}].high")
            calibrated = _require_quantized_float(row[2], f"mapping[{index}].calibrated")
            mapping.append((low, high, calibrated))
        _require_quantized_float(self.tau, "tau")

        object.__setattr__(self, "per_bin", per_bin)
        object.__setattr__(self, "mapping", tuple(mapping))


@dataclass(frozen=True)
class CalibrationManifest:
    calibration_version: str
    model_id: str
    prompt_template_hash: str
    registry_hash: str
    normalizer_rules_hash: str
    resolver_code_version: str
    fitted_on_goldset_hashes: tuple[str, ...]
    fitted_on_evidence_hash: str
    fitted_on_git_commit: str
    slices: Mapping[Literal["benign", "hr"], CalibrationSlice]

    def __post_init__(self) -> None:
        if not isinstance(self.slices, Mapping):
            raise ValueError("slices must be a mapping of calibration slices")

        frozen_slices: dict[str, CalibrationSlice] = {}
        for name, calibration in self.slices.items():
            if not isinstance(calibration, CalibrationSlice):
                raise ValueError("slices must contain CalibrationSlice values")
            frozen_slices[name] = CalibrationSlice(
                calibration.n_samples,
                calibration.per_bin,
                calibration.mapping,
                calibration.tau,
            )
        object.__setattr__(self, "slices", MappingProxyType(frozen_slices))


def canonical_manifest_hash(manifest: CalibrationManifest) -> str:
    """Return the canonical SHA-256 binding for a calibration manifest."""

    def normalize(value: object) -> object:
        if isinstance(value, float):
            # json.dumps emits finite float tokens via repr(); validation already guaranteed their 6dp form.
            return value
        if is_dataclass(value):
            return {item.name: normalize(getattr(value, item.name)) for item in fields(value)}
        if isinstance(value, Mapping):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [normalize(item) for item in value]
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    canonical_json = json.dumps(normalize(manifest), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolverConfig:
    llm_enabled: bool
    timeout_ms: int
    manifest: CalibrationManifest | None
    manifest_hash: str | None
    evidence_hash: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.llm_enabled, bool) or not isinstance(self.timeout_ms, int) or self.timeout_ms <= 0:
            raise ValueError("llm_enabled must be bool and timeout_ms must be positive")
        if self.llm_enabled and self.manifest is None:
            raise ValueError("llm_enabled requires a calibration manifest")
        if self.manifest is None and self.manifest_hash is not None:
            raise ValueError("manifest_hash requires a calibration manifest")


@dataclass
class RunBudget:
    max_calls: int
    calls_made: int = 0
    seen_uids: set[str] = field(default_factory=set)
    seen_unresolved: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_calls < 0 or self.calls_made < 0 or self.calls_made > self.max_calls:
            raise ValueError("invalid run budget state")


@dataclass(frozen=True)
class RiskEvidence:
    matched_hr_slot_ids: tuple[str, ...]
    domain_hr_hit: bool
    lexicon_hit: bool

    @property
    def any(self) -> bool:
        return bool(self.matched_hr_slot_ids)


FallbackOutcome = Literal[
    "not_attempted_deterministic_hit",
    "not_attempted_disabled",
    "not_attempted_empty_choice_set",
    "not_attempted_budget_exhausted",
    "forced_set_overflow",
    "selected",
    "low_confidence",
    "abstained",
    "invalid_output",
    "transport_failed",
    "timeout",
]


@dataclass(frozen=True)
class TransportAttempt:
    outcome: Literal["ok", "transport_error", "timeout", "invalid_output"]
    latency_ms: int
    raw_excerpt: str | None = None


@dataclass(frozen=True)
class DataTruncation:
    value_bytes_orig: int
    value_bytes_used: int
    evidence_bytes_orig: int
    evidence_bytes_used: int
    truncated: bool


@dataclass(frozen=True)
class BlockingReceipt:
    choice_set: tuple[str, ...]
    choice_set_hash: str
    slice: Literal["benign", "hr"]


@dataclass(frozen=True)
class FallbackSuggestion:
    raw_choice: str | None
    raw_confidence: float | None
    attempts: tuple[TransportAttempt, ...]


class FallbackProvider(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def prompt_template_hash(self) -> str: ...

    def suggest(
        self,
        request: ResolveRequest,
        choices: tuple[SlotView, ...],
        prompt_slice: Literal["benign", "hr"],
        truncated_value: str,
        truncated_evidence: str,
        timeout_ms: int,
    ) -> FallbackSuggestion: ...


@dataclass(frozen=True)
class LlmReceipt:
    model_id: str
    prompt_template_hash: str
    blocking: BlockingReceipt
    attempts: tuple[TransportAttempt, ...]
    data_truncation: DataTruncation
    raw_choice: str | None
    raw_confidence: float | None
    calibrated_confidence: float | None
    provider_contract_violation: str | None


@dataclass(frozen=True)
class VersionStamp:
    resolver_code_version: str
    registry_hash: str
    normalizer_rules_hash: str
    normalizer_version: str
    calibration_hash: str | None


@dataclass(frozen=True)
class ResolveDecision:
    action: Literal["BIND", "ADD", "ABSTAIN_ADD"]
    slot_id: str | None
    unresolved_domain: str | None
    unresolved: bool
    high_risk: bool
    risk_evidence: RiskEvidence
    method: Literal["exact", "alias", "llm_choice", "none"]
    confidence: float
    fallback_outcome: FallbackOutcome
    disabled_reason: str | None
    blocking_receipt: BlockingReceipt | None
    llm_receipt: LlmReceipt | None
    guard_fired: bool
    versions: VersionStamp

    def __post_init__(self) -> None:
        if self.unresolved != (self.action == "ABSTAIN_ADD"):
            raise ValueError("unresolved must exactly match ABSTAIN_ADD")
        if self.action == "ABSTAIN_ADD":
            if self.slot_id is not None or self.unresolved_domain is None:
                raise ValueError("abstentions require only unresolved_domain")
        elif self.slot_id is None or self.unresolved_domain is not None:
            raise ValueError("resolved decisions require only slot_id")
        if (self.disabled_reason is not None) != (self.fallback_outcome == "not_attempted_disabled"):
            raise ValueError("disabled_reason is only present for disabled fallback outcomes")


def split_slot_id(slot_id: str) -> tuple[str, str]:
    if not isinstance(slot_id, str) or slot_id.count(":") != 1:
        raise ValueError("slot_id must have exactly one kind separator")
    kind, domain = slot_id.split(":", 1)
    if kind not in _KINDS or not domain or domain.startswith("xunres_"):
        raise ValueError("slot_id has an invalid kind or reserved domain")
    return kind, domain
