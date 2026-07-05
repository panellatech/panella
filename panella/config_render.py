"""Pure per-distribution config rendering (Slice-S §1.6 part 2 / P3a seam).

The finalizer profile + wings allowlists must MATCH the deployment's owner identity, but
``AgentProfile.load`` reads hardcoded in-repo paths and a coord FF-deploy would overwrite any
in-repo templating — so origin/main keeps ``config/agents/panella-finalizer.yaml`` +
``config/wings.yaml`` OWNER-PINNED (owner's deployment, FF-safe), and the PACKAGE build renders
generic versions from the generic governance instead. These pure helpers are that render step,
pulled into P2 so the §1.5(g) generic finalize-to-active proof tests the RENDERED config without
depending on a P3a package build.

Everything identity-shaped derives from governance ``identity`` (owner_wing / owner_slug /
default_tenant_id); the structural vocabulary (rooms ``preferences``/``feedback``, suffixes
``_preference``/``_feedback``) is fixed product structure. Imports nothing outside
``panella`` + stdlib (fence target).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from panella.governance import Governance

FINALIZER_PROFILE_NAME = "panella-finalizer"
# The generic serving + MCP profiles P3b renders alongside the finalizer profile. Names are stable
# generic strings (never an owner slug) so a distribution's env defaults never embed an identity.
SERVING_PROFILE_NAME = "serving"
MCP_READ_PROFILE_NAME = "mcp-read"
MCP_WRITE_PROFILE_NAME = "mcp-write"
# The stdio MCP server (tools/panella_mcp_server.py) defaults to this profile name
# (PANELLA_MCP_PROFILE). On a packaged box that sets PANELLA_CONFIG_DIR to the rendered dir, the
# stdio path resolves profiles from there — so the rendered artifact must also carry a profile under
# this name, else `AgentProfile.load("panella-mcp")` fails before the stdio server serves any tool. It
# is rendered as the SAME generic read profile as mcp-read (its role: read-only retrieval).
STDIO_DEFAULT_PROFILE_NAME = "panella-mcp"


def render_serving_profile(governance: Governance) -> str:
    """The HTTP facade's generic serving profile (P3b) — closes the P3a 403 product gap: an
    owner-token routine ``/v1/memory/*`` op resolves this profile (``PANELLA_HTTP_PROFILE=serving``)
    whose ``tenant_scope`` is the box's OWN default tenant, so the op is admitted (200 on a fresh
    box), not 403 against a foreign deployment's tenant pin. Writes are candidates-only
    (``approval_required_for: ['*']`` + enforced owner allowlists)."""
    identity = governance.identity
    wing = identity.owner_wing
    profile = {
        "name": SERVING_PROFILE_NAME,
        "description": (
            "Generic HTTP serving profile (package-rendered). Routine reads over the owner wing; "
            "all writes route to the approval queue (candidates-only)."
        ),
        "tenant_scope": [identity.default_tenant_id],
        "write_default": {"wing": wing, "room": "preferences"},
        "write_quota": {"daily_max_drawers": 200, "burst_max_per_minute": 60},
        "memory_type_allowlist": [f"{identity.owner_slug}_preference", f"{identity.owner_slug}_feedback"],
        "retention_policy": {"default_ttl_days": None, "ephemeral_rooms": []},
        "approval_required_for": ["*"],
        "read_allowlist": [f"{wing}/*"],
        "deny": [],
        "max_query_k": 20,
        "retrieval_mode": "hybrid",
        "wing_boost": {"default": 1.0},
        "observability_level": "standard",
        "cost_budget": {"daily_embed_calls_max": 1000},
        "enforce_write_allowlist": True,
        "finalizer_only": False,
        "write_wing_allowlist": [wing],
        "write_room_allowlist": [f"{wing}/preferences", f"{wing}/feedback"],
    }
    return yaml.safe_dump(profile, sort_keys=False)


def render_mcp_read_profile(governance: Governance, *, name: str = MCP_READ_PROFILE_NAME) -> str:
    """The generic MCP read profile (plan v7 R6 P3) — read-only over the owner wing. Empty
    ``memory_type_allowlist`` = no write capability (the MCP write tool is not registered for it).
    ``name`` lets the SAME read profile be rendered under the stdio default name (panella-mcp) too."""
    identity = governance.identity
    wing = identity.owner_wing
    profile = {
        "name": name,
        "description": "Generic MCP read profile (package-rendered): read-only retrieval over the owner wing.",
        "tenant_scope": [identity.default_tenant_id],
        "write_default": {"wing": wing, "room": "preferences"},
        "write_quota": {"daily_max_drawers": 1, "burst_max_per_minute": 1},
        "memory_type_allowlist": [],
        "retention_policy": {"default_ttl_days": None, "ephemeral_rooms": []},
        "approval_required_for": [],
        "read_allowlist": [f"{wing}/*"],
        "deny": [],
        "max_query_k": 20,
        "retrieval_mode": "hybrid",
        "wing_boost": {"default": 1.0},
        "observability_level": "standard",
        "cost_budget": {"daily_embed_calls_max": 1000},
        "enforce_write_allowlist": False,
        "finalizer_only": False,
        "write_wing_allowlist": [],
        "write_room_allowlist": [],
    }
    return yaml.safe_dump(profile, sort_keys=False)


def render_mcp_write_profile(governance: Governance) -> str:
    """The generic MCP write profile (P3b): candidates-only durable writes over the owner wing —
    ``approval_required_for: ['*']`` + enforced owner allowlists, so every ``submit_candidate``
    routes to the approval queue (never a direct durable write)."""
    identity = governance.identity
    wing, slug = identity.owner_wing, identity.owner_slug
    profile = {
        "name": MCP_WRITE_PROFILE_NAME,
        "description": (
            "Generic MCP write profile (package-rendered): submits durable-write CANDIDATES to the "
            "approval queue over the owner wing. Reads the owner wing."
        ),
        "tenant_scope": [identity.default_tenant_id],
        "write_default": {"wing": wing, "room": "preferences"},
        "write_quota": {"daily_max_drawers": 100, "burst_max_per_minute": 20},
        "memory_type_allowlist": [f"{slug}_preference", f"{slug}_feedback"],
        "retention_policy": {"default_ttl_days": None, "ephemeral_rooms": []},
        "approval_required_for": ["*"],
        "read_allowlist": [f"{wing}/*"],
        "deny": [],
        "max_query_k": 20,
        "retrieval_mode": "hybrid",
        "wing_boost": {"default": 1.0},
        "observability_level": "standard",
        "cost_budget": {"daily_embed_calls_max": 1000},
        "enforce_write_allowlist": True,
        "finalizer_only": False,
        "write_wing_allowlist": [wing],
        "write_room_allowlist": [f"{wing}/preferences", f"{wing}/feedback"],
    }
    return yaml.safe_dump(profile, sort_keys=False)


def render_finalizer_profile(governance: Governance) -> str:
    """The generic post-approval finalizer profile, allowlists derived from governance identity.
    Mirrors the owner-pinned in-repo profile's shape (quota, finalizer_only, enforce flags)."""
    identity = governance.identity
    wing, slug = identity.owner_wing, identity.owner_slug
    profile = {
        "name": FINALIZER_PROFILE_NAME,
        "description": (
            "Post-approval durable writer (package-rendered). finalizer_only: usable ONLY by the "
            "approval finalizer after the provenance gate — never a direct write profile."
        ),
        "tenant_scope": [identity.default_tenant_id],
        "write_default": {"wing": wing, "room": "preferences"},
        "write_quota": {"daily_max_drawers": 200, "burst_max_per_minute": 60},
        "memory_type_allowlist": [f"{slug}_preference", f"{slug}_feedback"],
        "retention_policy": {"default_ttl_days": None, "ephemeral_rooms": []},
        "approval_required_for": [],
        "read_allowlist": [f"{wing}/preferences", f"{wing}/feedback"],
        "deny": [],
        "max_query_k": 5,
        "retrieval_mode": "hybrid",
        "wing_boost": {"default": 1.0},
        "observability_level": "standard",
        "cost_budget": {"daily_embed_calls_max": 1000},
        "enforce_write_allowlist": True,
        "finalizer_only": True,
        "write_wing_allowlist": [wing],
        "write_room_allowlist": [f"{wing}/preferences", f"{wing}/feedback"],
    }
    return yaml.safe_dump(profile, sort_keys=False)


def render_wings(governance: Governance) -> str:
    """The generic wings config: the owner wing with the structural approval rooms."""
    identity = governance.identity
    wings = {
        "wings": {
            identity.owner_wing: {
                "description": "The box owner's personal wing (package-rendered).",
                "rooms": ["preferences", "feedback"],
            },
        },
    }
    return yaml.safe_dump(wings, sort_keys=False)


def render_distribution_config(governance: Governance, out_dir: Path) -> dict[str, Path]:
    """Render the per-distribution config files into ``out_dir`` (``agents/`` + ``wings.yaml``),
    the same layout ``AgentProfile.load`` resolves through governance ``paths.config_dir``.
    Returns the written paths keyed by logical name."""
    agents_dir = out_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    renders = {
        "finalizer_profile": (FINALIZER_PROFILE_NAME, render_finalizer_profile),
        "serving_profile": (SERVING_PROFILE_NAME, render_serving_profile),
        "mcp_read_profile": (MCP_READ_PROFILE_NAME, render_mcp_read_profile),
        "mcp_write_profile": (MCP_WRITE_PROFILE_NAME, render_mcp_write_profile),
        # The stdio MCP server's default profile name, rendered as the generic read profile so the
        # stdio-over-SSH path resolves on a packaged box (PANELLA_CONFIG_DIR) — GH Codex bot P2.
        "stdio_default_profile": (
            STDIO_DEFAULT_PROFILE_NAME,
            lambda gov: render_mcp_read_profile(gov, name=STDIO_DEFAULT_PROFILE_NAME),
        ),
    }
    for logical_name, (profile_name, render_fn) in renders.items():
        path = agents_dir / f"{profile_name}.yaml"
        path.write_text(render_fn(governance), encoding="utf-8")
        written[logical_name] = path
    wings_path = out_dir / "wings.yaml"
    wings_path.write_text(render_wings(governance), encoding="utf-8")
    written["wings"] = wings_path
    return written
