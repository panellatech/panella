"""Deterministic resolver-domain normalization and its content address."""

from __future__ import annotations

import hashlib
import re

STOPWORDS = frozenset(
    {
        "the", "a", "an", "my", "of", "in", "for", "current", "currently", "primary", "main", "new",
        "favorite", "favourite", "preferred", "usual", "regular", "personal",
    }
)
PLURAL_KEEP = frozenset({"status", "address", "gps", "analysis", "glasses", "fitness", "wellness", "business"})
PLURAL_Y_SUFFIX = "ies"
NORMALIZER_VERSION = "1.0.0"


def extractor_normalize_domain(value: str) -> str:
    """The extractor-compatible, syntax-only normalization step."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def _canonical_rules_repr() -> str:
    return repr((tuple(sorted(STOPWORDS)), tuple(sorted(PLURAL_KEEP)), PLURAL_Y_SUFFIX))


def compute_normalizer_rules_hash() -> str:
    return hashlib.sha256(_canonical_rules_repr().encode("utf-8")).hexdigest()


normalizer_rules_hash = compute_normalizer_rules_hash()


def resolver_normalize(value: str) -> str:
    """Apply the frozen K1 semantic folding rules to an extractor-domain surface."""
    if not isinstance(value, str):
        raise ValueError("resolver normalization requires a string")
    tokens: list[str] = []
    for token in extractor_normalize_domain(value).split("_"):
        if not token or token in STOPWORDS:
            continue
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss") and token not in PLURAL_KEEP:
            token = f"{token[:-3]}y" if token.endswith(PLURAL_Y_SUFFIX) else token[:-1]
        tokens.append(token)
    return "_".join(tokens)
