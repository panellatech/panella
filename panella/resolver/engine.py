"""Single-entry deterministic K1 resolver engine."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .blocking import assemble_blocking
from .escalation import requires_hr_escalation
from .normalize import NORMALIZER_VERSION, normalizer_rules_hash, resolver_normalize
from .registry import MIN_REGISTRY_SLOTS, PINNED_REGISTRY_HASH, SlotRegistry, load_registry
from .risk import compute_risk_evidence
from .types import (
    BlockingReceipt,
    CalibrationSlice,
    DataTruncation,
    FallbackProvider,
    FallbackSuggestion,
    LlmReceipt,
    ResolveDecision,
    ResolveRequest,
    ResolverConfig,
    ResolverContext,
    RiskEvidence,
    RunBudget,
    TransportAttempt,
    VersionStamp,
)

RESOLVER_CODE_VERSION = "1.0.0"
MIN_CAL_SAMPLES = {"benign": 50, "hr": 30}


@dataclass(frozen=True)
class _AlwaysMissProvider:
    @property
    def model_id(self) -> str:
        return "always-miss"

    @property
    def prompt_template_hash(self) -> str:
        return "always-miss-v1"

    def suggest(
        self,
        request: ResolveRequest,
        choices: tuple[object, ...],
        prompt_slice: str,
        truncated_value: str,
        truncated_evidence: str,
        timeout_ms: int,
    ) -> FallbackSuggestion:
        return FallbackSuggestion("ABSTAIN", 0.0, (TransportAttempt("ok", 0),))


class ResolverEngine:
    """Resolve one request without writes, I/O, or any bypass of deterministic matching."""

    def __init__(
        self,
        config: ResolverConfig | None = None,
        *,
        registry: SlotRegistry | None = None,
        provider: FallbackProvider | None = None,
    ) -> None:
        self.config = config or ResolverConfig(False, 1000, None, None, None)
        self.registry = registry if registry is not None else load_registry()
        self.provider: FallbackProvider = provider or _AlwaysMissProvider()
        if len(self.registry.slots) < MIN_REGISTRY_SLOTS:
            raise ValueError(f"registry must contain at least {MIN_REGISTRY_SLOTS} slots")
        if self.registry.content_hash != PINNED_REGISTRY_HASH:
            raise ValueError("registry content hash does not match the pin")
        self._llm_disabled_reason: str | None = None
        if self.config.llm_enabled:
            self._llm_disabled_reason = self._manifest_binding_disabled_reason()

    def _manifest_binding_disabled_reason(self) -> str | None:
        manifest = self.config.manifest
        if manifest is None:
            return "manifest_component_mismatch:manifest"
        bindings = (
            ("manifest_hash", self.config.manifest_hash is not None),
            ("evidence_hash", self.config.evidence_hash is not None),
            ("registry_hash", manifest.registry_hash == self.registry.content_hash),
            ("normalizer_rules_hash", manifest.normalizer_rules_hash == normalizer_rules_hash),
            ("resolver_code_version", manifest.resolver_code_version == RESOLVER_CODE_VERSION),
            ("model_id", manifest.model_id == self.provider.model_id),
            ("prompt_template_hash", manifest.prompt_template_hash == self.provider.prompt_template_hash),
        )
        for field, matches in bindings:
            if not matches:
                return f"manifest_component_mismatch:{field}"
        return None

    def _versions(self) -> VersionStamp:
        return VersionStamp(
            resolver_code_version=RESOLVER_CODE_VERSION,
            registry_hash=self.registry.content_hash,
            normalizer_rules_hash=normalizer_rules_hash,
            normalizer_version=NORMALIZER_VERSION,
            calibration_hash=self.config.manifest_hash,
        )

    @staticmethod
    def _unresolved_domain(request_uid: str, budget: RunBudget) -> str:
        encoded = f"xunres_{hashlib.sha256(request_uid.encode('utf-8')).hexdigest()[:32]}"
        known_uid = budget.seen_unresolved.get(encoded)
        if known_uid is not None and known_uid != request_uid:
            raise RuntimeError("unresolved-domain encoding collision")
        budget.seen_unresolved[encoded] = request_uid
        return encoded

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= limit:
            return value
        return encoded[:limit].decode("utf-8", errors="ignore")

    def _truncation(self, request: ResolveRequest) -> tuple[str, str, DataTruncation]:
        value = self._truncate(request.value, 2048)
        evidence = self._truncate(request.evidence_text, 4096)
        value_original = len(request.value.encode("utf-8"))
        evidence_original = len(request.evidence_text.encode("utf-8"))
        receipt = DataTruncation(
            value_original,
            len(value.encode("utf-8")),
            evidence_original,
            len(evidence.encode("utf-8")),
            value != request.value or evidence != request.evidence_text,
        )
        return value, evidence, receipt

    def _abstain(
        self,
        request: ResolveRequest,
        budget: RunBudget,
        risk_evidence: RiskEvidence,
        fallback_outcome: str,
        *,
        guard_fired: bool,
        blocking_receipt: BlockingReceipt | None = None,
        llm_receipt: LlmReceipt | None = None,
        disabled_reason: str | None = None,
    ) -> ResolveDecision:
        high_risk = risk_evidence.any or guard_fired or (
            blocking_receipt is not None and blocking_receipt.slice == "hr"
        )
        return ResolveDecision(
            action="ABSTAIN_ADD",
            slot_id=None,
            unresolved_domain=self._unresolved_domain(request.request_uid, budget),
            unresolved=True,
            high_risk=high_risk,
            risk_evidence=risk_evidence,
            method="none",
            confidence=0.0,
            fallback_outcome=fallback_outcome,  # type: ignore[arg-type]
            disabled_reason=disabled_reason,
            blocking_receipt=blocking_receipt,
            llm_receipt=llm_receipt,
            guard_fired=guard_fired,
            versions=self._versions(),
        )

    def _resolved(
        self,
        slot_id: str,
        context: ResolverContext,
        risk_evidence: RiskEvidence,
        *,
        method: str,
        confidence: float,
        fallback_outcome: str,
        guard_fired: bool,
        blocking_receipt: BlockingReceipt | None = None,
        llm_receipt: LlmReceipt | None = None,
    ) -> ResolveDecision:
        action = "BIND" if any(slot.slot_id == slot_id for slot in context.existing_slots) else "ADD"
        slot = self.registry.by_id[slot_id]
        high_risk = (
            risk_evidence.any
            or guard_fired
            or slot.high_risk
            or (blocking_receipt is not None and blocking_receipt.slice == "hr")
        )
        return ResolveDecision(
            action=action,
            slot_id=slot_id,
            unresolved_domain=None,
            unresolved=False,
            high_risk=high_risk,
            risk_evidence=risk_evidence,
            method=method,  # type: ignore[arg-type]
            confidence=confidence,
            fallback_outcome=fallback_outcome,  # type: ignore[arg-type]
            disabled_reason=None,
            blocking_receipt=blocking_receipt,
            llm_receipt=llm_receipt,
            guard_fired=guard_fired,
            versions=self._versions(),
        )

    def _slice_is_valid(self, slice_name: str) -> bool:
        manifest = self.config.manifest
        if manifest is None:
            return False
        calibration = manifest.slices.get(slice_name)  # type: ignore[arg-type]
        if calibration is None or calibration.n_samples < MIN_CAL_SAMPLES[slice_name]:
            return False
        calibrated_values = {row[2] for row in calibration.mapping}
        return bool(calibration.mapping) and calibration.tau in calibrated_values

    @staticmethod
    def _calibrate(raw_confidence: float, calibration: CalibrationSlice) -> float | None:
        for low, high, calibrated in calibration.mapping:
            if low <= raw_confidence < high or (raw_confidence == 1.0 and high == 1.0):
                return calibrated
        return None

    @staticmethod
    def _provider_outcome(suggestion: FallbackSuggestion, timeout_ms: int) -> tuple[str, str | None]:
        attempts = suggestion.attempts
        if not attempts:
            return "invalid_output", "empty_attempts"
        if any(
            attempt.raw_excerpt is not None
            and (
                attempt.outcome != "invalid_output"
                or not isinstance(attempt.raw_excerpt, str)
                or len(attempt.raw_excerpt.encode("utf-8")) > 200
            )
            for attempt in attempts
        ):
            return "invalid_output", "excerpt_misuse"
        if len(attempts) > 2 or (
            len(attempts) == 2 and attempts[0].outcome not in {"transport_error", "timeout"}
        ):
            return "transport_failed", "attempt_sequence"
        if any(attempt.outcome == "ok" and attempt.latency_ms > timeout_ms for attempt in attempts):
            return "timeout", "timeout_exceeded_ok"
        terminal = attempts[-1]
        if terminal.outcome != "ok" and (suggestion.raw_choice is not None or suggestion.raw_confidence is not None):
            return "invalid_output", "payload_without_ok"
        if terminal.outcome == "transport_error":
            return "transport_failed", None
        if terminal.outcome == "timeout":
            return "timeout", None
        if terminal.outcome == "invalid_output":
            return "invalid_output", None
        return "ok", None

    def resolve(self, request: ResolveRequest, context: ResolverContext, budget: RunBudget) -> ResolveDecision:
        if request.request_uid in budget.seen_uids:
            raise ValueError("duplicate request_uid in run budget")
        budget.seen_uids.add(request.request_uid)
        risk_evidence = compute_risk_evidence(request, self.registry)
        normalized = resolver_normalize(request.raw_domain)
        target = self.registry.by_id.get(f"{request.kind}:{normalized}")
        method = "exact"
        if target is None:
            target = self.registry.alias_raw.get(request.raw_domain)
            method = "alias"
        if target is None:
            folded_target = self.registry.alias_folded.get(normalized)
            if folded_target is not None and not folded_target.high_risk:
                target = folded_target
                method = "alias"
        guard_fired = requires_hr_escalation(target, risk_evidence)
        if target is not None and not guard_fired:
            return self._resolved(
                target.slot_id,
                context,
                risk_evidence,
                method=method,
                confidence=1.0,
                fallback_outcome="not_attempted_deterministic_hit",
                guard_fired=False,
            )

        if self._llm_disabled_reason is not None:
            return self._abstain(
                request,
                budget,
                risk_evidence,
                "not_attempted_disabled",
                guard_fired=guard_fired,
                disabled_reason=self._llm_disabled_reason,
            )
        if not self.config.llm_enabled:
            return self._abstain(
                request, budget, risk_evidence, "not_attempted_disabled", guard_fired=guard_fired,
                disabled_reason="global_disabled",
            )
        if budget.calls_made >= budget.max_calls:
            return self._abstain(request, budget, risk_evidence, "not_attempted_budget_exhausted", guard_fired=guard_fired)

        blocking = assemble_blocking(
            request, self.registry, risk_evidence, target.slot_id if guard_fired and target is not None else None
        )
        if blocking.forced_overflow:
            return self._abstain(
                request, budget, risk_evidence, "forced_set_overflow", guard_fired=guard_fired,
                blocking_receipt=blocking.receipt,
            )
        if not blocking.receipt.choice_set:
            return self._abstain(
                request, budget, risk_evidence, "not_attempted_empty_choice_set", guard_fired=guard_fired,
                blocking_receipt=blocking.receipt,
            )
        if not self._slice_is_valid(blocking.receipt.slice):
            reason = "hr_slice_required_but_disabled" if blocking.receipt.slice == "hr" and risk_evidence.any else "slice_disabled"
            return self._abstain(
                request, budget, risk_evidence, "not_attempted_disabled", guard_fired=guard_fired,
                blocking_receipt=blocking.receipt, disabled_reason=reason,
            )

        budget.calls_made += 1
        truncated_value, truncated_evidence, truncation = self._truncation(request)
        suggestion = self.provider.suggest(
            request,
            blocking.choices,
            blocking.receipt.slice,
            truncated_value,
            truncated_evidence,
            self.config.timeout_ms,
        )
        provider_outcome, violation = self._provider_outcome(suggestion, self.config.timeout_ms)
        calibrated: float | None = None
        if provider_outcome == "ok":
            if (
                not isinstance(suggestion.raw_choice, str)
                or isinstance(suggestion.raw_confidence, bool)
                or not isinstance(suggestion.raw_confidence, (float, int))
                or not math.isfinite(float(suggestion.raw_confidence))
                or not 0.0 <= float(suggestion.raw_confidence) <= 1.0
                or (
                    suggestion.raw_choice != "ABSTAIN"
                    and suggestion.raw_choice not in blocking.receipt.choice_set
                )
            ):
                provider_outcome = "invalid_output"
            elif suggestion.raw_choice != "ABSTAIN":
                calibration = self.config.manifest.slices[blocking.receipt.slice]  # type: ignore[union-attr]
                calibrated = self._calibrate(float(suggestion.raw_confidence), calibration)
                if calibrated is None:
                    provider_outcome = "invalid_output"
        receipt = LlmReceipt(
            model_id=self.provider.model_id,
            prompt_template_hash=self.provider.prompt_template_hash,
            blocking=blocking.receipt,
            attempts=suggestion.attempts,
            data_truncation=truncation,
            raw_choice=suggestion.raw_choice,
            raw_confidence=suggestion.raw_confidence,
            calibrated_confidence=calibrated,
            provider_contract_violation=violation,
        )
        if provider_outcome != "ok":
            return self._abstain(
                request, budget, risk_evidence, provider_outcome, guard_fired=guard_fired,
                blocking_receipt=blocking.receipt, llm_receipt=receipt,
            )
        if suggestion.raw_choice == "ABSTAIN":
            return self._abstain(
                request, budget, risk_evidence, "abstained", guard_fired=guard_fired,
                blocking_receipt=blocking.receipt, llm_receipt=receipt,
            )
        if calibrated is None:
            raise RuntimeError("successful provider output must have calibrated confidence")
        calibration = self.config.manifest.slices[blocking.receipt.slice]  # type: ignore[union-attr]
        if calibrated < calibration.tau:
            return self._abstain(
                request, budget, risk_evidence, "low_confidence", guard_fired=guard_fired,
                blocking_receipt=blocking.receipt, llm_receipt=receipt,
            )
        return self._resolved(
            suggestion.raw_choice,
            context,
            risk_evidence,
            method="llm_choice",
            confidence=calibrated,
            fallback_outcome="selected",
            guard_fired=guard_fired,
            blocking_receipt=blocking.receipt,
            llm_receipt=receipt,
        )
