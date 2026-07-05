"""Default memory adapter selection."""

from __future__ import annotations

import logging
import os
from typing import Any

from panella.panella_adapter import (
    DEFAULT_BASE_URL,
    PanellaAdapter,
    PanellaAuthMissing,
    resolve_panella_api_key,
)

logger = logging.getLogger(__name__)

ENV_BACKEND = "PANELLA_MEMORY_BACKEND"
ENV_BASE_URL = "PANELLA_BASE_URL"
DEFAULT_BACKEND = "panella"
VALID_BACKENDS = {"auto", "panella"}


def default_adapter(*, retrieval_mode: str | None = None, source: str = "panella") -> Any:
    raw_selector = os.environ.get(ENV_BACKEND, DEFAULT_BACKEND).strip().lower() or DEFAULT_BACKEND
    if raw_selector not in VALID_BACKENDS:
        raise ValueError(f"{ENV_BACKEND} must be one of {sorted(VALID_BACKENDS)}, got {raw_selector!r}")

    if raw_selector == "auto":
        logger.warning(
            "memory_default_adapter deprecated_selector=auto treated_as=panella source=%s "
            "note=auto-fallback removed; set PANELLA_MEMORY_BACKEND=panella (or unset) to silence",
            source,
        )

    try:
        api_key = resolve_panella_api_key()
    except PanellaAuthMissing:
        logger.error("memory_default_adapter failed selector=%s reason=missing_panella_auth source=%s", raw_selector, source)
        raise

    base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    logger.info("memory_default_adapter active=panella selector=%s base_url=%s source=%s", raw_selector, base_url, source)
    return PanellaAdapter(base_url=base_url, api_key=api_key)
