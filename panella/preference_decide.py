"""Stage 2 DECIDE — bounded-room same-slot judge for preference SUPERSEDE (NO durable writes).

The "Decide" half of the Extract→Decide preference store (Panella store north-star path). Given a NEW
extracted attribute (a ``PreferenceDescriptor`` candidate) and the actor's CURRENTLY-ACTIVE prefs in a
BOUNDED room (``owner/preferences`` — tens to low-hundreds), decide whether the candidate is:

  ADD                    — a new slot (no existing pref is the same slot)
  NOOP                   — the same slot AND the same value (nothing changes)
  UPDATE                 — the same slot, a new value → supersede the prior row (by its content_hash)
  UPDATE_NEEDS_APPROVAL  — same slot + new value BUT high-risk → never silent; route to Owner-visible approval

The converged key-stability design (``migration-log/briefs/panella-stage2-keystab-design-2026-06-23.md``,
R3 refinement → 95 for the bounded room): the free-form canonical_key is NOT a join key (P1a proved an
LLM mints a different key per mention — key_stability 0.154). Supersede targets are found by a
**bounded-room exhaustive LLM same-slot JUDGE** (the candidate vs ALL active prefs, no embedding
shortlist for the bounded room) and demoted by the prior row's stable ``content_hash``.

SAFETY LAW (asymmetric containment): a false-NEGATIVE (missed update → stale+current coexist, the
continuity gate catches it, recoverable) is acceptable; a false-MERGE (demote a DIFFERENT slot's valid
fact) is NEVER acceptable. So every uncertainty resolves toward ADD (never merge): empty/ambiguous
(>1) match, an unparseable judge reply, a kind/risk-class mismatch, or an out-of-range index all yield
ADD. The LLM judge answers ONLY the semantic same-slot question; DETERMINISTIC code owns the final
action (kind/risk guards, value equality, high-risk routing) — the judge can never directly cause a
merge.

PURE + transport-agnostic: the LLM is an INJECTED ``judge_fn`` (same seam as the extractor's
``chat_fn``) so the SAME logic runs in the shadow eval (subscription codex judge) and the future gated
cron (model-router) and hermetic tests (a fake) — no eval-only path, so a measured result is credible.
This module performs NO durable writes, NO store calls, and NO conflict search beyond the in-memory
active list it is handed; the caller (the shadow eval now; the gated finalizer later) acts on the
returned proposal.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

# A transport-agnostic judge call: (system_prompt, user_prompt) -> raw model text (a JSON object). The
# eval binds a subscription codex judge; the future cron binds the model-router; tests inject a fake.
JudgeFn = Callable[[str, str], str]

# Risk classes. "high" = identity/medical/constraints — never a silent supersede (UPDATE_NEEDS_APPROVAL).
RISK_NORMAL = "normal"
RISK_HIGH = "high"


class DecideAction(StrEnum):
    """The proposed action for a candidate (str-valued so reports/JSON serialize cleanly)."""

    ADD = "ADD"
    NOOP = "NOOP"
    UPDATE = "UPDATE"
    UPDATE_NEEDS_APPROVAL = "UPDATE_NEEDS_APPROVAL"


class DecideReason(StrEnum):
    """The DETERMINISTIC reason a given action was chosen — the machine-readable form of ``rationale``, so a
    shadow-eval run (or production observability) can tabulate WHY each ADD happened without parsing prose.
    The six ADD reasons distinguish a safe ADD (genuinely new / no-match) from a recall-tax ADD (a guard
    blocked an otherwise-same-slot update — the key Phase-0 signal: kind/risk drift vs judge miss)."""

    NO_ACTIVE = "no_active"            # ADD: empty room, judge not called
    JUDGE_NO_MATCH = "judge_no_match"  # ADD: judge returned no same-slot index
    JUDGE_AMBIGUOUS = "judge_ambiguous"  # ADD: judge returned >1 index → fail-safe ADD
    PARSE_ERROR = "parse_error"        # ADD: unparseable judge reply → fail-safe ADD
    # Guard PRECEDENCE (honest metric contract): kind is checked BEFORE risk, so a candidate that mismatches
    # BOTH kind and risk is reported as KIND_GUARD (the risk mismatch is then moot — it's already not the same
    # slot). Telemetry reads kind_guard as "kind drift (risk possibly too)", not "kind drift, risk matched".
    KIND_GUARD = "kind_guard"          # ADD: single match but candidate.kind != match.kind (checked first)
    RISK_GUARD = "risk_guard"          # ADD: single match, kind MATCHES, but candidate.risk_class != match.risk_class
    SAME_VALUE = "same_value"          # NOOP: same slot, value normalizes equal
    SAME_SLOT_UPDATE = "same_slot_update"      # UPDATE: same slot, new value, normal-risk
    HIGH_RISK_UPDATE = "high_risk_update"      # UPDATE_NEEDS_APPROVAL: same slot, new value, high-risk


@dataclass(frozen=True)
class PreferenceDescriptor:
    """One typed attribute as the Decide layer sees it — the candidate (new fact) OR an active pref.

    This is a SEPARATE input contract from ``preference_extraction.PreferenceCandidate`` (which carries
    only kind/domain/value/confidence/evidence and whose canonical_key is now a label, not a join key).
    The Decide guards need ``risk_class`` and a slot DESCRIPTOR; the eval builds this from the goldset and
    the future prod path builds it from extraction + a risk classifier — neither mutates the P1a type.

    ``content_hash`` is the stable durable id of an ACTIVE pref (the supersede target); it is ``None`` for
    a not-yet-durable candidate. ``slot_question`` (+ optional ``slot_scope``) is what the judge sees;
    ``slot_label`` is a human/provenance label. ``kind`` and ``risk_class`` drive the deterministic guards.
    """

    kind: str
    risk_class: str
    value: str
    slot_question: str = ""
    slot_label: str = ""
    slot_scope: str = ""
    source_sid: str = ""
    content_hash: str | None = None

    @property
    def judge_descriptor(self) -> str:
        """The slot text shown to the judge: slot_question (+ scope), falling back to slot_label."""
        base = self.slot_question.strip() or self.slot_label.strip()
        scope = self.slot_scope.strip()
        return f"{base} ({scope})" if scope else base


@dataclass(frozen=True)
class DecideResult:
    """The Decide proposal (NOT a durable write). ``target_content_hash`` is the active row to supersede
    (UPDATE/UPDATE_NEEDS_APPROVAL) or the duplicate row (NOOP); ``None`` for ADD. ``matched_index`` is the
    index into the active list the judge selected. ``ambiguous``/``parse_error`` flag fail-safe ADDs."""

    action: DecideAction
    target_content_hash: str | None = None
    matched_index: int | None = None
    confidence: float = 0.0
    ambiguous: bool = False
    parse_error: bool = False
    rationale: str = ""
    reason: DecideReason | None = None  # machine-readable WHY (Phase-0 trace); decide() always sets it
    # the RAW judge verdict (NOT the deterministic action) — so a Phase-0 trace can show WHICH active rows the
    # judge returned + its own reasoning, esp. for judge_no_match / judge_ambiguous misses (GH-bot P2).
    judge_indices: tuple[int, ...] = ()
    judge_rationale: str = ""


# ── the judge (semantic same-slot ONLY; deterministic code owns the action) ──

DECIDE_JUDGE_SYS = (
    "You compare a NEW attribute about a user against a numbered list of EXISTING stored attributes, and "
    "identify which existing entries describe the SAME underlying attribute/slot as the new one — i.e. the "
    "new statement is an UPDATE to (or a restatement of) that same thing.\n\n"
    "Match on the SLOT (what the attribute is ABOUT), NOT on the value. Two entries are the SAME slot even "
    "when their values DIFFER — e.g. 'favorite programming language: Python' and 'favorite programming "
    "language: Rust' are the SAME slot (an update). Two entries are DIFFERENT slots when they describe "
    "different attributes even if related — e.g. 'home city' vs 'work city', or 'food allergy' vs "
    "'favorite food'.\n\n"
    "Return ONLY a JSON object (no prose, no markdown fences) with:\n"
    '  "same_slot_indices": array of 0-based indices of EXISTING entries that are the SAME slot as the new '
    "attribute (normally 0 or 1; more than 1 ONLY if the existing list genuinely repeats the same slot)\n"
    '  "confidence": 0.0-1.0\n'
    '  "rationale": one short sentence\n\n'
    "If none match, return an empty array. When uncertain, PREFER an empty array — do NOT guess a match "
    "(a wrong match destroys a distinct fact; a missed match is harmless and recoverable)."
)


def build_judge_user(candidate: PreferenceDescriptor, active: Sequence[PreferenceDescriptor]) -> str:
    """The user prompt: the new attribute's slot+value, then the numbered active prefs (slot+value)."""
    lines = [
        "NEW attribute:",
        f"  slot: {candidate.judge_descriptor}",
        f"  value: {candidate.value}",
        "",
        "EXISTING stored attributes:",
    ]
    for i, p in enumerate(active):
        lines.append(f"  [{i}] slot: {p.judge_descriptor} | value: {p.value}")
    lines += ["", "Which EXISTING indices are the SAME slot as the NEW attribute? Return the JSON object."]
    return "\n".join(lines)


def _extract_json_object(raw: str) -> dict | None:
    """Best-effort parse of a JSON OBJECT from a judge reply (tolerates ```json fences / stray prose).
    Returns the dict, or ``None`` when the reply was not a JSON object at all (the caller fail-safes to
    ADD). Never raises. (Mirrors preference_extraction._extract_json_array for the object case.)"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


@dataclass
class _JudgeVerdict:
    indices: list[int]
    confidence: float
    rationale: str
    parse_error: bool = False


def parse_judge(raw: str, n_active: int) -> _JudgeVerdict:
    """Parse the judge reply into validated active-list indices. FAIL-SAFE: an unparseable reply, a
    non-list ``same_slot_indices``, OR a list containing ANY structurally-invalid entry (a bool, a
    non-int, or an out-of-range int) yields ``parse_error=True`` with NO indices (→ caller ADDs, never
    merges). We do NOT salvage the valid subset of a partly-malformed reply — acting on a verdict that
    contains garbage could itself drive a false merge (the never-false-merge law covers malformed
    indices). ``bool`` is excluded explicitly because it is an ``int`` subclass, so ``[true]`` would
    otherwise be read as index 1 (GH-bot). An empty list is valid (the judge found no same-slot match);
    duplicate valid ints are benign (deduped)."""
    obj = _extract_json_object(raw)
    raw_idxs = obj.get("same_slot_indices") if obj is not None else None
    if obj is None or not isinstance(raw_idxs, list):
        return _JudgeVerdict(indices=[], confidence=0.0, rationale="", parse_error=True)
    valid: list[int] = []
    for i in raw_idxs:
        if isinstance(i, bool) or not isinstance(i, int) or not (0 <= i < n_active):
            return _JudgeVerdict(indices=[], confidence=0.0, rationale="", parse_error=True)
        valid.append(i)
    try:
        conf = max(0.0, min(1.0, float(obj.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    return _JudgeVerdict(indices=sorted(set(valid)), confidence=conf, rationale=str(obj.get("rationale", "")).strip())


# ── value equality (NOOP vs UPDATE) ──

def _norm_value(s: str) -> str:
    """Normalize a value for NOOP detection: lowercase, collapse whitespace, strip surrounding
    punctuation. Strict-ish equality — a near-restatement that normalizes equal is a NOOP; anything else
    is an UPDATE (a needless supersede with an equivalent value is harmless; a missed NOOP is not)."""
    return re.sub(r"\s+", " ", str(s).strip().lower()).strip(" .,;:!?\"'")


# ── decide (deterministic; the judge only proposes same-slot candidates) ──

def decide(
    candidate: PreferenceDescriptor,
    active: Sequence[PreferenceDescriptor],
    *,
    judge_fn: JudgeFn,
) -> DecideResult:
    """Propose ADD/NOOP/UPDATE/UPDATE_NEEDS_APPROVAL for ``candidate`` against the bounded ``active`` set.

    No active prefs → ADD without calling the judge. Otherwise the judge proposes same-slot indices and
    DETERMINISTIC code owns the action: 0 matches → ADD; >1 → ambiguous ADD; an unparseable reply →
    fail-safe ADD; exactly 1 → guard on kind + risk_class (a mismatch is NOT the same slot → ADD), then
    NOOP (same value) / UPDATE (new value) / UPDATE_NEEDS_APPROVAL (new value, high-risk)."""
    if not active:
        return DecideResult(action=DecideAction.ADD, reason=DecideReason.NO_ACTIVE,
                            rationale="no active prefs in room")

    verdict = parse_judge(judge_fn(DECIDE_JUDGE_SYS, build_judge_user(candidate, active)), len(active))
    if verdict.parse_error:
        return DecideResult(
            action=DecideAction.ADD, ambiguous=True, parse_error=True, reason=DecideReason.PARSE_ERROR,
            rationale="judge reply unparseable — fail-safe ADD (never merge on an untrusted verdict)",
        )
    if not verdict.indices:
        return DecideResult(action=DecideAction.ADD, confidence=verdict.confidence,
                            reason=DecideReason.JUDGE_NO_MATCH, rationale="no same-slot match",
                            judge_rationale=verdict.rationale)
    if len(verdict.indices) > 1:
        return DecideResult(
            action=DecideAction.ADD, ambiguous=True, confidence=verdict.confidence,
            reason=DecideReason.JUDGE_AMBIGUOUS, judge_indices=tuple(verdict.indices),
            judge_rationale=verdict.rationale,
            rationale=f"ambiguous — {len(verdict.indices)} same-slot matches; fail-safe ADD (never merge)",
        )

    idx = verdict.indices[0]
    match = active[idx]
    # Deterministic guards: a different kind or risk class is NOT the same slot (the judge answers
    # semantics; these structured-field invariants are non-negotiable and code-owned). PRECEDENCE: kind is
    # checked before risk → a both-mismatch reports KIND_GUARD (see DecideReason; the reason is a metric
    # label, the action — ADD, never merge — is identical either way).
    if candidate.kind != match.kind:
        # ADD (target stays None → never merges), but record matched_index: the judge DID find a same-slot
        # row; the guard rejected it on kind drift. The trace needs this to tell "judge found nothing" apart
        # from "judge found it but kind/risk drifted" (Phase-0 diagnostic fidelity).
        return DecideResult(
            action=DecideAction.ADD, matched_index=idx, confidence=verdict.confidence,
            reason=DecideReason.KIND_GUARD, judge_indices=(idx,), judge_rationale=verdict.rationale,
            rationale=f"kind mismatch ({candidate.kind} vs {match.kind}) — not the same slot",
        )
    if candidate.risk_class != match.risk_class:
        return DecideResult(
            action=DecideAction.ADD, matched_index=idx, confidence=verdict.confidence,
            reason=DecideReason.RISK_GUARD, judge_indices=(idx,), judge_rationale=verdict.rationale,
            rationale=f"risk-class mismatch ({candidate.risk_class} vs {match.risk_class}) — not the same slot",
        )

    common = dict(target_content_hash=match.content_hash, matched_index=idx, confidence=verdict.confidence,
                  judge_indices=(idx,), judge_rationale=verdict.rationale)
    if _norm_value(candidate.value) == _norm_value(match.value):
        return DecideResult(action=DecideAction.NOOP, reason=DecideReason.SAME_VALUE,
                            rationale="same slot, same value", **common)
    if candidate.risk_class == RISK_HIGH:
        return DecideResult(
            action=DecideAction.UPDATE_NEEDS_APPROVAL, reason=DecideReason.HIGH_RISK_UPDATE,
            rationale="high-risk slot update — never silent; route to Owner-visible approval", **common,
        )
    return DecideResult(action=DecideAction.UPDATE, reason=DecideReason.SAME_SLOT_UPDATE,
                        rationale="same slot, new value", **common)
