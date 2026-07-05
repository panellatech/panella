"""Stage 2 EXTRACT — derive typed preference/fact candidates from user turns (NO durable writes).

The "Extract" half of the Extract→Decide preference store (Panella store north-star path). PURE and
transport-agnostic: the LLM is an INJECTED ``chat_fn`` so the SAME extraction logic runs in the P1a
shadow eval (injecting the OpenAI ``qa.chat`` transport) and the future P1 cron (injecting the
model-router) — no eval-only code path, so a measured lift is credible, not a benchmark artifact
(the converged roadmap's integrity guard). This module performs NO durable writes and NO conflict
search; it only turns text into typed candidates. The caller (eval now; gated cron later) decides /
queues / finalizes.

Design (converged #323): a ``canonical_key = "{kind}:{domain}"`` identifies a durable SLOT — two
candidates sharing a canonical_key are about the same slot (an UPDATE/supersede pair when the value
differs). ``kind ∈ {preference, fact, constraint}``; ``domain`` is a stable snake_case slot name. Key
STABILITY (same slot → same key across paraphrases) and the ABSENCE of harmful cross-slot collisions
are the riskiest assumptions of the whole store — measured by the P1a extraction eval before any
red-line durable write.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

# A transport-agnostic chat call: (system_prompt, user_prompt) -> raw model text. The eval binds the
# OpenAI key+model onto qa.chat; the future cron binds the model-router; tests inject a fake.
ChatFn = Callable[[str, str], str]

# The kinds a candidate may carry. Anything else the model emits is coerced to "fact" (conservative:
# an unrecognized typed attribute is still a factual statement, never silently dropped).
ALLOWED_KINDS = ("preference", "fact", "constraint")
_DEFAULT_KIND = "fact"

EXTRACT_SYS = (
    "You extract DURABLE facts and preferences ABOUT THE USER from a single user message, for a "
    "long-term personal memory. Extract ONLY stable, self-describing attributes of the user: their "
    "preferences/tastes (kind=preference), their factual states/attributes (kind=fact), and explicit "
    "rules/permissions they set (kind=constraint). Do NOT extract: questions, requests, transient "
    "one-off events, chit-chat, or anything not a durable attribute of the user.\n\n"
    "For each attribute output an object with:\n"
    '  "kind": one of "preference" | "fact" | "constraint"\n'
    '  "domain": a short, STABLE snake_case slot name for the attribute (the SAME slot must get the '
    "SAME domain every time it is mentioned, however it is phrased). Use the most general natural slot "
    "for the attribute (for example timezone, preferred_news_source, or data_sharing_rule show the "
    "shape). Different attributes MUST get different domains.\n"
    '  "value": the current value, concise\n'
    '  "confidence": 0.0-1.0\n'
    '  "evidence": a short quote from the message\n\n'
    "Return ONLY a JSON array (no prose, no markdown fences). Return [] if the message contains no "
    "durable user attribute."
)


@dataclass(frozen=True)
class PreferenceCandidate:
    """One extracted typed attribute (NOT yet durable). ``canonical_key`` is the slot identity."""

    source_sid: str
    kind: str
    domain: str
    value: str
    confidence: float
    evidence: str
    # True when the model emitted a kind OUTSIDE the allowed taxonomy and it was coerced to the default.
    # The coercion keeps a usable canonical_key for the store (safe), but the eval counts a coerced
    # candidate as a SCHEMA failure so a model that ignores the kind contract is not masked (GH-bot P2).
    coerced_kind: bool = False

    @property
    def canonical_key(self) -> str:
        return f"{self.kind}:{self.domain}"


def normalize_domain(domain: str) -> str:
    """Stable snake_case normalization of a slot name: lowercase, non-alphanumerics → ``_``, collapse
    and trim underscores. Deterministic so the SAME slot label always maps to the SAME key."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(domain).strip().lower())
    return slug.strip("_")


def _coerce_confidence(value: object) -> float:
    try:
        c = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, c))


def _extract_json_array(raw: str) -> list | None:
    """Best-effort parse of a JSON array from a model response (tolerates ```json fences / stray prose).
    Returns the array (possibly EMPTY — a valid "no attributes" reply) when the model returned a JSON
    array; returns ``None`` when the reply was NOT a JSON array at all (non-array JSON / unparseable /
    prose) — a contract failure the caller counts. Never raises."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, list):
        return None
    return parsed  # ALL elements (incl. non-dict); the caller counts non-dict entries as schema failures


def parse_with_stats(raw: str, source_sid: str) -> tuple[list[PreferenceCandidate], dict[str, int]]:
    """Parse + report raw schema compliance:
    ``(candidates, {raw_objects, dropped, coerced, malformed})``. ``malformed`` = non-object array
    entries (null/string); ``dropped`` = objects missing/null domain or value; ``coerced`` = emitted
    candidates whose kind was omitted/invalid. All are model-contract failures the eval must SEE —
    counting only emitted candidates would mask a model that returns mostly-malformed output (GH-bot P1).
    ``raw_objects`` is the FULL array length (every element), so nothing is invisible to schema_validity."""
    arr = _extract_json_array(raw)
    if arr is None:  # the model did not return a JSON array at all → one schema-contract failure
        return [], {"raw_objects": 1, "dropped": 0, "coerced": 0, "malformed": 1}
    objs = arr
    out: list[PreferenceCandidate] = []
    dropped = coerced = malformed = 0
    for item in objs:
        if not isinstance(item, dict):
            malformed += 1  # a non-object array entry (null/string/number) — a schema-contract failure
            continue
        # Coalesce NULL explicitly: item.get("domain", "") returns None when the key is present with a
        # null value (the default applies only to ABSENT keys), and normalize_domain(None) would yield a
        # bogus "none" slot / str(None) a bogus "None" value — a schema failure masquerading as a valid
        # candidate. Treat null (or missing) required fields as a drop. (GH-bot P1.)
        raw_domain = item.get("domain")
        raw_value = item.get("value")
        domain = normalize_domain(raw_domain) if raw_domain is not None else ""
        value = str(raw_value).strip() if raw_value is not None else ""
        if not domain or not value:
            dropped += 1  # an attribute with no slot or no value is not actionable
            continue
        raw_kind = item.get("kind")
        kind_str = str(raw_kind).strip().lower() if raw_kind is not None else ""
        kind_valid = kind_str in ALLOWED_KINDS  # a present + in-taxonomy kind; OMITTED or invalid → flagged
        if not kind_valid:
            coerced += 1
        out.append(
            PreferenceCandidate(
                source_sid=source_sid,
                kind=kind_str if kind_valid else _DEFAULT_KIND,
                domain=domain,
                value=value,
                confidence=_coerce_confidence(item.get("confidence")),
                evidence=str(item.get("evidence", "")).strip(),
                coerced_kind=not kind_valid,
            )
        )
    return out, {"raw_objects": len(objs), "dropped": dropped, "coerced": coerced, "malformed": malformed}


def parse_candidates(raw: str, source_sid: str) -> list[PreferenceCandidate]:
    """Typed candidates from a raw model response (drops entries missing a domain or value)."""
    return parse_with_stats(raw, source_sid)[0]


def extract_preferences(
    text: str, source_sid: str, *, chat_fn: ChatFn, stats: dict[str, int] | None = None
) -> list[PreferenceCandidate]:
    """Extract typed preference/fact/constraint candidates from one user message. Pure: no durable
    writes, no conflict search. ``chat_fn(system, user)`` supplies the LLM (injected). An empty/blank
    message short-circuits to ``[]`` without an LLM call. If ``stats`` is given it is populated with the
    raw schema-compliance counts (for the eval's schema_validity); prod callers omit it."""
    if not (text or "").strip():
        if stats is not None:
            stats.update({"raw_objects": 0, "dropped": 0, "coerced": 0, "malformed": 0})
        return []
    raw = chat_fn(EXTRACT_SYS, text)
    cands, s = parse_with_stats(raw, source_sid)
    if stats is not None:
        stats.update(s)
    return cands
