"""The ingest-visibility contract — derives the wing/room stamp the shipped `serving` facade
profile actually reads, from `panella/config_render.py` itself (never a hardcoded guess).

The make-or-break detail this closes: `render_serving_profile` (panella/config_render.py) builds
`read_allowlist: [f"{wing}/*"]` where `wing = governance.identity.owner_wing`. Rows the store adapter
normalizes WITHOUT wing/room metadata fall back to `wing="knowledge", room="legacy"`
(panella/panella_adapter.py LEGACY_FALLBACK_WING/ROOM) — a wing the packaged `serving` profile does
NOT read. A naive port that ingests without stamping wing/room therefore gets recall@k=0 on the
FACADE lane forever, silently, because every hit is filtered out by `MemoryClient._filter_hits`'s
`read_allowlist` check before it ever reaches the caller.

This module reads the REAL governance (the same `panella.governance.current_governance()` the
facade process itself resolves at boot) and renders the actual serving profile via
`render_serving_profile`, so the eval harness's wing/room stamp is provably the same value the box
being measured will actually serve — not a copy that can drift from it.
"""
from __future__ import annotations

import fnmatch

import yaml

from panella.config_render import render_serving_profile
from panella.governance import Governance, current_governance


def eval_wing_room(governance: Governance | None = None) -> tuple[str, str]:
    """The (wing, room) pair the eval ingester must stamp on every row for the facade lane to see it.

    ``wing`` = the resolved governance's ``identity.owner_wing`` (the same value
    ``render_serving_profile`` uses for its ``read_allowlist``). ``room`` = ``"preferences"`` — one of
    the two structural rooms every rendered serving/finalizer profile allows
    (``write_room_allowlist``/``read_allowlist`` both key off ``{wing}/preferences`` and
    ``{wing}/feedback``; either works, "preferences" is arbitrary but fixed for determinism)."""
    gov = governance if governance is not None else current_governance()
    return gov.identity.owner_wing, "preferences"


def assert_serving_profile_reads(wing: str, room: str, *, governance: Governance | None = None) -> None:
    """Fail LOUD (not a silent guess) if the rendered `serving` profile's `read_allowlist` would NOT
    admit `{wing}/{room}` — the exact predicate `MemoryClient._filter_hits` applies
    (`fnmatch.fnmatchcase` against each allowlist pattern). Call this at eval-box startup
    (`eval-isolation-check` / `eval-selftest`) so a governance drift is caught before any recall
    number is computed, not discovered as a silent facade-lane 0."""
    gov = governance if governance is not None else current_governance()
    rendered = yaml.safe_load(render_serving_profile(gov))
    allowlist = rendered.get("read_allowlist") or []
    path = f"{wing}/{room}"
    if not any(fnmatch.fnmatchcase(path, pattern) for pattern in allowlist):
        raise RuntimeError(
            f"eval ingest stamp wing={wing!r} room={room!r} (path={path!r}) is NOT covered by the "
            f"rendered `serving` profile's read_allowlist={allowlist!r} — every facade-lane recall "
            "would silently be 0. This means governance.identity.owner_wing on this box does not "
            "match the wing this harness derived; re-run eval_wing_room() against the box's actual "
            "governance, do not hardcode a value."
        )
