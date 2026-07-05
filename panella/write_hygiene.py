"""Ingress-hygiene constants for network memory writes (Slice-S P3b).

A network caller (HTTP `/v1/memory/write`, or an MCP write tool) must never inject server-
authoritative identity / provenance or internal-control flags via metadata passthrough. The
constants + helpers that enforce that live HERE — a neutral, stdlib-only module — so BOTH the HTTP
route and the MCP tool surface import ONE source of truth instead of one importing FastAPI route
code from the other (which would invert the import firewall and drag HTTP deps into the stdio MCP
path). This module is a governance fence target: it imports nothing outside the standard library.

Two strip-sets:

- ``HTTP_BLOCKED_WRITE_METADATA`` — the historical HTTP set (moved verbatim from the write route,
  behavior-preserving). ``author_agent_id`` / ``source_bridge`` / ``session_id`` are deliberately
  NOT blocked: they are caller-asserted provenance BY DESIGN (the log distinguishes caller-asserted
  author from the inferred principal).
- ``NETWORK_WRITE_BLOCKED_METADATA`` — the STRICTER superset the MCP write tool uses: also blocks
  ``source_system``. Unlike the HTTP route (whose callers are owner's own authenticated bridges),
  the MCP surface is a net-new externally-reachable write path where ``source_system`` MUST be
  server-derived — it feeds the DURABLE identity of an approval write (``client_raw`` builds the
  owner-templated ``f"{owner_slug}-manual"`` default, and ``MemoryClient.write`` otherwise honors a
  caller-supplied value), so a forged ``source_system`` would corrupt durable provenance /
  attribution even on a candidate that still requires approval.
"""

from __future__ import annotations

# Server-authoritative / internal-control keys an HTTP caller must not inject via metadata
# passthrough. principal_id/actor_id/subject_id are derived from the authenticated principal in
# client.write — a caller override forges provenance/attribution in the audit trail.
# raise_dedup_skipped is a private client.write kwarg. conversation_id is the oversize-floor
# semantic-dedup-skip control arg (locked to the in-process cc-sync profile). source_artifact_key
# is the cc-sync source-identity stamp layer ② keys its destructive source-version supersede on —
# a forged value could later target/mask another source's versions.
HTTP_BLOCKED_WRITE_METADATA: frozenset[str] = frozenset(
    {"raise_dedup_skipped", "principal_id", "actor_id", "subject_id", "conversation_id", "source_artifact_key"}
)

# The MCP write tool's stricter set: everything the HTTP route blocks, PLUS source_system (durable
# identity — must be server-derived on the network write surface, never caller-supplied).
NETWORK_WRITE_BLOCKED_METADATA: frozenset[str] = HTTP_BLOCKED_WRITE_METADATA | {"source_system"}

# cc-sync's source-version REPLACE hard-DELETEs every active row carrying a `ccsk:<key>` tag — a
# cc-sync-internal control tag. A network caller must never plant one (a forged ccsk: tag on a
# network-written row would make cc-sync's replace delete it = a wrong-row deletion). tags is a
# LIST, so it can't go in a key strip-set; filter it explicitly. Only ccsk: is reserved; all other
# caller tags pass through.
RESERVED_TAG_PREFIXES: tuple[str, ...] = ("ccsk:",)


def strip_reserved_tags(tags: object) -> object:
    """Drop reserved control tags (e.g. ``ccsk:``) from a caller-supplied ``tags`` list; pass
    non-list values through unchanged (the caller's own validation handles a wrong type)."""
    if not isinstance(tags, list):
        return tags
    return [
        t for t in tags
        if not (isinstance(t, str) and t.startswith(RESERVED_TAG_PREFIXES))
    ]


def sanitize_network_write_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Strip the strict network-write blocklist keys + reserved control tags from caller metadata.
    Used by the MCP write tool; returns a new dict (does not mutate the input)."""
    clean = {k: v for k, v in metadata.items() if k not in NETWORK_WRITE_BLOCKED_METADATA}
    if "tags" in clean:
        clean["tags"] = strip_reserved_tags(clean["tags"])
    return clean
