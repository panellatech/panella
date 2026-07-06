"""Unit tests for eval/longmemeval/visibility.py — the facade-visibility stamping logic, tested
against `config_render.py`'s ACTUAL rendered `serving` profile (not a hand-copied allowlist). This
is the make-or-break check the brief calls out: a wrong wing/room stamp makes every facade-lane
recall silently 0."""
from __future__ import annotations

import pytest

from eval.longmemeval.visibility import assert_serving_profile_reads, eval_wing_room
from panella.governance import load_governance


def test_eval_wing_room_derives_from_generic_governance() -> None:
    """With the shipped generic governance (no overlay), owner_wing defaults to 'owner' — the
    SAME value panella/config_render.py's render_serving_profile uses for its read_allowlist."""
    gov = load_governance()
    wing, room = eval_wing_room(gov)
    assert wing == gov.identity.owner_wing
    assert room == "preferences"


def test_derived_stamp_passes_the_real_serving_profile_gate() -> None:
    """The derived (wing, room) MUST be admitted by the rendered serving profile's read_allowlist —
    proves the derivation is correct against the real config_render.py logic, not just internally
    consistent with itself."""
    gov = load_governance()
    wing, room = eval_wing_room(gov)
    assert_serving_profile_reads(wing, room, governance=gov)  # must not raise


def test_wrong_wing_fails_the_gate_loudly() -> None:
    """A wing that does NOT match governance.identity.owner_wing must be rejected LOUDLY (never a
    silent pass) — this is the exact failure mode the brief's make-or-break constraint targets: a
    naive port ingesting into the wrong wing gets recall@k=0 on the facade lane forever, silently,
    unless something asserts visibility before any recall number is computed."""
    gov = load_governance()
    with pytest.raises(RuntimeError, match="NOT covered by the rendered"):
        assert_serving_profile_reads("definitely-not-the-owner-wing", "preferences", governance=gov)


def test_legacy_fallback_wing_room_would_fail_the_gate() -> None:
    """panella/panella_adapter.py's LEGACY_FALLBACK_WING/ROOM ('knowledge'/'legacy') is EXACTLY the
    silent-zero trap this module exists to prevent — assert it does NOT pass the serving gate,
    proving the derived stamp is not accidentally equivalent to the legacy fallback."""
    gov = load_governance()
    with pytest.raises(RuntimeError):
        assert_serving_profile_reads("knowledge", "legacy", governance=gov)
