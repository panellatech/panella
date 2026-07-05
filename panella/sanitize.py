"""Small redaction helpers for bridge memory writers (redact-and-keep v8).

Scope: comprehensive on CREDENTIAL shapes (mirrors the canonical credential
patterns in config/secret_patterns.yaml) but deliberately CONSERVATIVE on broad
numeric PII. ``_has_pii_signal()`` is a hard drain gate, so a false positive
silently DROPS a valid memory; bare-number matching is intentionally minimal
(phone requires separators; SSN/credit-card are left to the LLM-gated Phase-1
classifier, not duplicated in this hard gate).

redact-and-keep contract: the drain SANITIZES first, then gates on the SANITIZED
text — so the common case is REDACT-AND-KEEP (persist the memory with the secret
masked) and DROP is reserved for residue the bounded redactors cannot fully mask.
The design converged over 4 Codex adversarial rounds (confidence 90 -> 97); the
exact behavior is pinned by tests/panella/test_sanitize.py (ported from the
runnable closure probe). Per "leak-proof by architecture, not enumeration", the
auth path is closed by a FAIL-SAFE invariant (``_auth_residual``): an unanticipated
auth scheme degrades to DROP, never LEAK.

Honest-scope residuals (do NOT claim "all input" is covered). R-A is IRREDUCIBLE
at the regex/length/entropy layer — independently adversarially cross-checked
2026-06-01 (conf 93); see workbench redact-keep-entropy-detector-ADVERSARIAL-
FINDING. A genuine close needs a fundamentally different mechanism (deferred
sibling), NOT a wider regex/entropy gate — every threshold that drops R-A also
over-drops benign auth prose:
  R-A (leak): a credential under an UNKNOWN auth scheme, comma-separated (so the
    bounded redactor stops at the comma), with the value segmented by ARBITRARY
    separators into sub-16 chunks (``;`` ``|`` ``%`` space ``][`` ``}{`` ``.``
    ``:`` ``&`` ...), OR an all-alpha / low-entropy value. These collide with
    benign auth prose (``owner=platform-infrastructure-team`` strips to an
    all-alpha 24-char ~3.5-entropy run) in every length/charset/entropy dimension,
    so no fixed threshold separates them. Known param names (signature/proof/hmac +
    the keyword set) and known schemes (Bearer/Digest/Basic/SigV4/...) ARE handled.
  R-B (over-drop, safe-side): a benign long value in an auth header is dropped to a
    recoverable dead-letter (the fail-safe keys on length, not entropy). Does NOT
    occur in the real spool corpus (all observed auth-context tails are genuine
    high-entropy credentials).
  R-C (pre-existing): a keyword-less, scheme-less high-entropy bare blob.

The ``codex_desktop_drain`` real-event path imports this module (lazily), so its
absence ships a drain that ImportErrors on the first captured turn while
``--dry-run`` (empty spool) still passes. ``test_sanitize`` + the drain test
guard against that file going missing again.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_REDACTED = "[redacted]"

# Unicode normalization, run before BOTH sanitize() and _has_pii_signal(). NFKC
# folds fullwidth lookalikes (``＝`` -> ``=``); stripping Unicode ``Cf``
# (format) chars plus a curated invisible/filler set closes zero-width /
# combining-joiner / filler codepoints that splice a keyword from its ``=``
# (e.g. ``pass͏word=...``) or a Digest field from the next, defeating the
# credential regexes. Includes the SUPPLEMENTARY variation selectors
# U+E0100-E01EF (Codex round-4 P0-3).
_INVISIBLE_CODEPOINTS = (
    {0x00AD, 0x034F, 0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0}
    | set(range(0x180B, 0x1810))  # Mongolian free variation selectors
    | set(range(0xFE00, 0xFE10))  # BMP variation selectors
    | set(range(0xE0100, 0xE01F0))  # supplementary variation selectors
)


def _normalize(text: str) -> str:
    """NFKC-fold then strip format + invisible/filler codepoints."""

    normalized = unicodedata.normalize("NFKC", str(text))
    return "".join(
        ch
        for ch in normalized
        if unicodedata.category(ch) != "Cf" and ord(ch) not in _INVISIBLE_CODEPOINTS
    )


_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
# Phone: require an explicit separator (or parenthesized area code) so a bare
# 10-digit run — epoch timestamps, build/request ids like 1717075200 — is NOT
# treated as PII. _has_pii_signal is a hard dead-letter gate in the drain, so a
# false positive here silently drops a valid memory (Codex #175 round-5 P2).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)[\s.\-]?|\d{3}[\s.\-])\d{3}[\s.\-]?\d{4}(?!\d)"
)
# Secret keywords as a `_`/`-`-delimited *component* of the key identifier, so we
# catch all of: `api_key=…`, `token: …`, prefixed names (`PANELLA_API_KEY=…`,
# `CF_ACCESS_CLIENT_SECRET=…`) AND keyword-in-the-middle names
# (`AWS_SECRET_ACCESS_KEY=…`, `GITHUB_ACCESS_TOKEN_V2=…`). Component boundaries
# (`_`/`-`) keep benign words like `tokenizer=` / `passwordless=` from matching.
# v8: `authorization` REMOVED — it is handled by _AUTH_LINE_RE / _auth_residual;
# leaving it here let the widened value class swallow the Digest comma that the
# fail-safe keys on. `signature`/`proof`/`hmac` ADDED — redact-and-KEEP their
# values (incl. dot/colon/&-segmented bodies: `X-Amz-Signature`, bare `signature=`,
# known-param `proof=`).
_SECRET_KEYWORDS = (
    r"(?:api[_-]?key|access[_-]?key|bearer|cookie|passphrase|password|passwd|pwd"
    r"|secret|token|webhook|signature|proof|hmac)"
)
# Group 1 (key + separator + optional opening quote) is preserved; group 2 (the
# value) is redacted. v8: value class widened `[^\s,;'"]+` -> `[^\s'"]+` so
# punctuation/comma-containing secret values are fully redacted.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)"
    r"("
    r"\b(?:[A-Za-z0-9]+[_-])*"  # optional WORD_ / WORD- prefixes
    + _SECRET_KEYWORDS
    + r"(?:[_-][A-Za-z0-9]+)*"  # optional _WORD / -WORD suffixes
    r"\s*[:=]\s*['\"]?"  # separator + optional opening quote
    r")"
    r"([^\s'\"]+)"  # the value (redacted)
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
# Token shapes mirror the canonical CREDENTIAL set in config/secret_patterns.yaml
# (GitHub classic `gh[poshru]_`, fine-grained `github_pat_`, OpenAI/Anthropic
# `sk-`, Slack `xox[abeprs]-`, AWS access-key id `AKIA…`). Broad PII in that YAML
# (SSN / credit-card / email:password) is intentionally NOT duplicated in this
# lightweight write-path helper.
_COMMON_TOKEN_RE = re.compile(
    r"\b(?:"
    r"github_pat_[A-Za-z0-9_]{22,}"  # GitHub fine-grained PAT
    r"|gh[poshru]_[A-Za-z0-9_]{20,}"  # GitHub classic (ghp_/gho_/ghs_/ghh_/ghr_/ghu_)
    r"|sk-[A-Za-z0-9_-]{20,}"  # OpenAI/Anthropic (sk-, sk-ant-, sk-proj-)
    r"|xox[abeprs]-[A-Za-z0-9-]{20,}"  # Slack (all canonical prefixes incl xoxe-)
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r")\b"
)
# v8: provider-specific token families that carry no `key=` prefix (so
# _SECRET_ASSIGNMENT_RE misses them): Stripe `sk_live_/sk_test_/rk_live_/whsec_`,
# Google `AIza`, GitLab `glpat-`, HuggingFace `hf_`, npm `npm_`, DigitalOcean
# `dop_v1_/doo_v1_`. The leading `\b` + trailing `(?![A-Za-z0-9])` avoid a false
# positive on a bare prose mention of the prefix (`use the sk_live_ prefix`).
_PROVIDER_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk_live_[A-Za-z0-9]{16,}|sk_test_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}"
    r"|whsec_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{20,}|glpat-[0-9A-Za-z_\-]{16,}"
    r"|hf_[A-Za-z0-9]{16,}|npm_[A-Za-z0-9]{16,}|dop_v1_[A-Za-z0-9]{16,}|doo_v1_[A-Za-z0-9]{16,}"
    r")(?![A-Za-z0-9])"
)
# Bearer auth headers + bare JWTs (`eyJ….….…`): the _SECRET_ASSIGNMENT_RE value
# group stops at the space after `Bearer`, leaving the real token; redact the
# whole `Bearer <token>` and standalone JWTs. Both shapes per config/secret_patterns.yaml.
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{16,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
# v8 auth path: bounded redaction + a scheme-agnostic fail-safe.
# _AUTH_QUOTED_RE is escape-aware so a quoted Authorization JSON value is redacted
# whole even with embedded `\"`. _AUTH_LINE_RE stays BOUNDED (`[^\r\n",]+`) because
# _batch_text emits one-line JSON (json.dumps, scripts/codex_desktop_drain.py) — a
# full-line redactor would eat a whole batch; under-redaction residue is caught by
# _auth_residual instead.
_AUTH_QUOTED_RE = re.compile(
    r'(?i)(["\'](?:proxy-)?authorization["\']\s*[:=]\s*)("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\')'
)
_AUTH_LINE_RE = re.compile(r'(?i)((?:proxy-)?authorization\s*[:=]\s*)(?:"[^"]*"|[^\r\n",]+)')
# Fail-safe context + residual high-entropy credential token (>=16 of the
# base64url + `=`/`-`/`_` alphabet), with a left boundary so it does not match a
# longer surrounding run.
_AUTH_CONTEXT_RE = re.compile(r'(?i)(?:proxy-)?(?:authoriz|authentic)[\w-]*["\']?\s*[:=]')
_CRED_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{16,}")


def sanitize(text: str) -> str:
    """Return text with direct PII and credentials redacted (redact-and-keep)."""

    value = _normalize(text)
    value = _PRIVATE_KEY_RE.sub(_REDACTED, value)
    value = _AUTH_QUOTED_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    value = _AUTH_LINE_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    value = _BEARER_RE.sub(_REDACTED, value)
    value = _JWT_RE.sub(_REDACTED, value)
    value = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    value = _PROVIDER_TOKEN_RE.sub(_REDACTED, value)
    value = _COMMON_TOKEN_RE.sub(_REDACTED, value)
    value = _EMAIL_RE.sub(_REDACTED, value)
    value = _PHONE_RE.sub(_REDACTED, value)
    return value


def stored_content_hash(content: str) -> str:
    """The content_hash Panella store WILL store for ``content`` — the single source of truth.

    Mirrors Panella store's ``generate_content_hash`` (upstream ``utils/hashing.py``:
    ``sha256(content.strip().lower())``) applied to the SANITIZED content, since both
    ``MemoryClient.write`` and ``PanellaAdapter.add_memory`` sanitize before the POST
    (``sanitize`` is idempotent, so the double-pass collapses). It is also what
    ``find_active_hashes_by_tag`` returns for a stored row, so the cc-sync atom-set
    no-op/verify can compare the expected set ``E`` against the active set ``A`` by
    equality. Extracted here (next to ``sanitize``, the normalization it depends on) so
    the cc-sync splitter and ``MemoryClient.replace_source_atom_set`` cannot drift apart
    — a divergence between them is exactly the R2-class no-op/verify hazard.
    """
    return hashlib.sha256(sanitize(content).strip().lower().encode("utf-8")).hexdigest()


def _auth_residual(value: str) -> bool:
    """Fail-safe: a high-entropy credential token still present in an auth/authn
    header context AFTER the bounded redactor ran => the redactor under-redacted
    => signal DROP.

    Scheme-agnostic: Digest (comma/semicolon/quoted/first-param), SigV4, rspauth,
    and any unanticipated same-line scheme all degrade to DROP not LEAK. The tail
    spans RFC obs-fold continuation lines (start with SP/HT) (round-4 P0-1) and
    stops at a JSON sibling key or object/array close so it does not over-scan into
    unrelated batch fields (round-4 P0-5 over-drop reduction).
    """

    for match in _AUTH_CONTEXT_RE.finditer(value):
        segment = value[match.end() : match.end() + 800]
        lines = segment.split("\n")
        parts = [lines[0]]
        for line in lines[1:]:
            if line[:1] in (" ", "\t"):  # obsolete header line-folding continuation
                parts.append(line)
            else:
                break
        tail = " ".join(parts).replace(_REDACTED, " ")
        stop = re.search(r',\s*"[\w.-]+"\s*:|[}\]]', tail)
        if stop:
            tail = tail[: stop.start()]
        if _CRED_TOKEN_RE.search(tail):
            return True
    return False


def _has_pii_signal(text: str) -> bool:
    """Return True when text still carries data the bridge must not persist raw.

    The drain calls this on the SANITIZED text (sanitize-then-gate), so True means
    a residual the bounded redactors could not fully mask -> DROP. Marker immunity:
    a redacted assignment (`token=[redacted]`) leaves only the `[redacted]` marker
    in the value, which has no alphanumeric residue, so it does not re-trip.
    """

    value = _normalize(text)
    for match in _SECRET_ASSIGNMENT_RE.finditer(value):
        if any(ch.isalnum() for ch in match.group(2).replace(_REDACTED, "")):
            return True
    if _auth_residual(value):
        return True
    neutral = value.replace(_REDACTED, " ")
    return any(
        pattern.search(neutral)
        for pattern in (
            _PRIVATE_KEY_RE,
            _BEARER_RE,
            _JWT_RE,
            _PROVIDER_TOKEN_RE,
            _COMMON_TOKEN_RE,
            _EMAIL_RE,
            _PHONE_RE,
        )
    )
