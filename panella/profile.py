"""Agent profile loader and validator for the memory boundary."""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from panella.governance import current_governance, resolved_config_dir
from panella.principal import default_tenant_id

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENTS_DIR = ROOT / "config" / "agents"
WINGS_PATH = ROOT / "config" / "wings.yaml"
VALID_RETRIEVAL_MODES = {"legacy", "hybrid"}
VALID_OBSERVABILITY_LEVELS = {"minimal", "standard", "verbose"}
RENDER_CONFIG_COMMAND = "panella-render-config --out"


class AgentProfileConfigError(ValueError):
    """Raised when rendered agent profile config is missing or invalid."""


def _resolved_agents_dir() -> Path:
    """The agents-config dir: the ``PANELLA_CONFIG_DIR`` env / governance ``paths.config_dir`` when
    set (packaged distribution points at its rendered dist-config), else the in-repo
    ``config/agents`` (a repo checkout runs on its committed config)."""
    config_dir = resolved_config_dir()
    if config_dir:
        return config_dir / "agents"
    return DEFAULT_AGENTS_DIR


def _resolved_wings_path() -> Path:
    config_dir = resolved_config_dir()
    if config_dir:
        return config_dir / "wings.yaml"
    return WINGS_PATH


def _render_target_for_agents_dir(agents_dir: Path) -> Path:
    return agents_dir.parent if agents_dir.name == "agents" else agents_dir


def _render_command_for_agents_dir(agents_dir: Path) -> str:
    out_dir = _render_target_for_agents_dir(agents_dir)
    return f"`{RENDER_CONFIG_COMMAND} {out_dir}` and set PANELLA_CONFIG_DIR={out_dir}"


def available_profile_names(*, agents_dir: Path | None = None) -> tuple[str, ...]:
    resolved = agents_dir if agents_dir is not None else _resolved_agents_dir()
    if not resolved.exists() or not resolved.is_dir():
        return ()
    return tuple(sorted(path.stem for path in resolved.glob("*.yaml") if path.is_file()))


def ensure_rendered_profiles(*, agents_dir: Path | None = None) -> None:
    resolved = agents_dir if agents_dir is not None else _resolved_agents_dir()
    if available_profile_names(agents_dir=resolved):
        return
    raise AgentProfileConfigError(
        "config not rendered: run "
        f"{_render_command_for_agents_dir(resolved)} "
        f"(expected rendered profiles in {resolved})"
    )


@dataclass(frozen=True)
class WriteDefaults:
    wing: str
    room: str


@dataclass(frozen=True)
class WriteQuota:
    daily_max_drawers: int
    burst_max_per_minute: int


@dataclass(frozen=True)
class RetentionPolicy:
    default_ttl_days: int | None = None
    ephemeral_rooms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CostBudget:
    daily_embed_calls_max: int


@dataclass(frozen=True)
class AgentProfile:
    name: str
    description: str
    write_default: WriteDefaults
    write_quota: WriteQuota
    memory_type_allowlist: list[str]
    retention_policy: RetentionPolicy
    approval_required_for: list[str]
    read_allowlist: list[str]
    deny: list[str]
    max_query_k: int
    retrieval_mode: str
    wing_boost: dict[str, float]
    observability_level: str
    cost_budget: CostBudget
    tenant_scope: list[str] = field(default_factory=lambda: [default_tenant_id()])
    # ⚙️v4 write allowlist gate (default off → legacy 7 profiles unchanged)
    write_wing_allowlist: tuple[str, ...] = ()
    write_room_allowlist: tuple[str, ...] = ()
    enforce_write_allowlist: bool = False
    # When True (default) a dedup_skipped write still consumes the write quota —
    # the PR#132 anti-spam posture, kept for ALL existing profiles. Set False on a
    # trusted batch bridge (cc-sync) whose daily re-scan legitimately re-submits
    # many already-stored files: counting those no-op dedups exhausts the per-minute
    # burst budget and starves genuinely-new writes (cc-sync 2026-05-29 incident).
    quota_counts_dedup: bool = True
    # Stage 2 P0 — a finalizer-only profile (e.g. panella-finalizer) has
    # approval_required_for:[] so its writes would NOT queue. That would make it a
    # back door around the whole approval gate (any caller loading it could write()
    # ungated). MemoryClient.write() REFUSES a finalizer_only profile: it is used
    # ONLY by the post-approval finalizer (panella/approval_finalizer.py), which
    # gates the durable write behind the approval-provenance check. Default False keeps
    # all existing profiles unchanged.
    finalizer_only: bool = False

    @classmethod
    def load(cls, name: str, *, agents_dir: Path | None = None, wings_path: Path | None = None) -> AgentProfile:
        # None → resolve through governance paths.config_dir (packaged distribution) or the
        # in-repo config tree — NOT a hardcoded repo-relative path (Slice-S P2 packaging seam).
        agents_dir = agents_dir if agents_dir is not None else _resolved_agents_dir()
        wings_path = wings_path if wings_path is not None else _resolved_wings_path()
        ensure_rendered_profiles(agents_dir=agents_dir)
        path = agents_dir / f"{name}.yaml"
        if not path.exists():
            valid = ", ".join(available_profile_names(agents_dir=agents_dir)) or "<none>"
            raise AgentProfileConfigError(
                f"agent profile {name!r} not found in {agents_dir} "
                f"(valid: {valid}); run {_render_command_for_agents_dir(agents_dir)}"
            )
        # Memoize parse+validate keyed on BOTH the profile YAML mtime AND the
        # wings.yaml mtime: load() is called per HTTP request (route + token
        # resolution) and profiles are static at runtime, so re-reading+re-parsing
        # every call is pure waste. validate() reads wings_path (write allowlists
        # are checked against it), so a wings edit must also invalidate the cache —
        # else an enforce-allowlist profile could keep passing/failing validation
        # against stale wings/rooms config. The cheap stat()s replace the read+parse
        # on the hot path; an edit to either file invalidates the entry (no restart).
        wings_mtime_ns = wings_path.stat().st_mtime_ns if wings_path.exists() else -1
        return _load_profile_cached(name, agents_dir, wings_path, path.stat().st_mtime_ns, wings_mtime_ns)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgentProfile:
        return cls(
            name=str(raw["name"]),
            description=str(raw.get("description") or ""),
            write_default=WriteDefaults(**_mapping(raw, "write_default")),
            write_quota=WriteQuota(**_mapping(raw, "write_quota")),
            memory_type_allowlist=[str(item) for item in raw.get("memory_type_allowlist", [])],
            retention_policy=RetentionPolicy(**_mapping(raw, "retention_policy")),
            approval_required_for=[str(item) for item in raw.get("approval_required_for", [])],
            read_allowlist=[str(item) for item in raw.get("read_allowlist", [])],
            deny=[str(item) for item in raw.get("deny", [])],
            max_query_k=int(raw["max_query_k"]),
            retrieval_mode=str(raw["retrieval_mode"]).strip().lower(),
            wing_boost={str(key): float(value) for key, value in _mapping(raw, "wing_boost").items()},
            observability_level=str(raw.get("observability_level", "standard")).strip().lower(),
            cost_budget=CostBudget(**_mapping(raw, "cost_budget")),
            tenant_scope=[str(item) for item in raw.get("tenant_scope", [default_tenant_id()])],
            # ⚙️v4 explicit parse — YAML omission keeps enforce=False, allowlist=()
            write_wing_allowlist=tuple(str(item) for item in raw.get("write_wing_allowlist", [])),
            write_room_allowlist=tuple(str(item) for item in raw.get("write_room_allowlist", [])),
            enforce_write_allowlist=bool(raw.get("enforce_write_allowlist", False)),
            # YAML omission keeps True → existing profiles unchanged (dedup counts).
            quota_counts_dedup=bool(raw.get("quota_counts_dedup", True)),
            # Stage 2 P0 — YAML omission keeps False (existing profiles unchanged).
            finalizer_only=bool(raw.get("finalizer_only", False)),
        )

    def validate(self, *, wings_path: Path = WINGS_PATH) -> None:
        wing_rooms = load_known_wing_rooms(wings_path)
        known_wings = set(wing_rooms.keys())
        if self.write_default.wing not in known_wings:
            raise ValueError(f"invalid write_default.wing: {self.write_default.wing}")
        if self.write_quota.daily_max_drawers <= 0:
            raise ValueError("write_quota.daily_max_drawers must be positive")
        if self.write_quota.burst_max_per_minute <= 0:
            raise ValueError("write_quota.burst_max_per_minute must be positive")
        if self.max_query_k <= 0:
            raise ValueError("max_query_k must be positive")
        if self.retrieval_mode not in VALID_RETRIEVAL_MODES:
            raise ValueError(f"invalid retrieval_mode: {self.retrieval_mode}")
        if self.observability_level not in VALID_OBSERVABILITY_LEVELS:
            raise ValueError(f"invalid observability_level: {self.observability_level}")
        if "default" not in self.wing_boost:
            raise ValueError("wing_boost.default is required")
        if self.cost_budget.daily_embed_calls_max <= 0:
            raise ValueError("cost_budget.daily_embed_calls_max must be positive")
        if not self.tenant_scope:
            raise ValueError("tenant_scope must include at least one tenant")
        # A tenant id must carry the governance-configured prefix; the deployment's own default
        # tenant is admitted verbatim (it may predate the prefix convention, e.g. a legacy corpus).
        identity = current_governance().identity
        for tenant_id in self.tenant_scope:
            if tenant_id != identity.default_tenant_id and not tenant_id.startswith(identity.tenant_id_prefix):
                raise ValueError(f"invalid tenant_scope tenant_id: {tenant_id}")
        # ⚙️v6 Phase B — write allowlist must reference real wings/rooms.
        # Empty allowlists (legacy default with enforce=False) trivially pass.
        # We validate even when enforce=False so YAML drift is caught early,
        # before the flip lands and starts denying real writes.
        for wing in self.write_wing_allowlist:
            if wing not in known_wings:
                raise ValueError(
                    f"write_wing_allowlist references unknown wing: {wing}"
                )
        for entry in self.write_room_allowlist:
            if entry.count("/") != 1:
                raise ValueError(
                    f"write_room_allowlist entry must be 'wing/room': {entry}"
                )
            wing, _, room = entry.partition("/")
            if not wing or not room:
                raise ValueError(
                    f"write_room_allowlist entry must be 'wing/room': {entry}"
                )
            if wing not in known_wings:
                raise ValueError(
                    f"write_room_allowlist references unknown wing: {entry}"
                )
            if room not in wing_rooms[wing]:
                raise ValueError(
                    f"write_room_allowlist references unknown room: {entry}"
                )

    def allows_tenant(self, tid: str) -> bool:
        return "*" in self.tenant_scope or str(tid) in self.tenant_scope


@functools.cache
def _load_profile_cached(
    name: str, agents_dir: Path, wings_path: Path, _profile_mtime_ns: int, _wings_mtime_ns: int
) -> AgentProfile:
    # _profile_mtime_ns / _wings_mtime_ns are part of the cache key only (they
    # invalidate the entry when the profile YAML or wings.yaml is edited on disk);
    # neither is read in the body. AgentProfile is frozen, so sharing one parsed
    # instance across callers is safe.
    path = agents_dir / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"agent profile must be a mapping: {path}")
    profile = AgentProfile.from_dict(raw)
    profile.validate(wings_path=wings_path)
    return profile


def load_known_wings(path: Path = WINGS_PATH) -> set[str]:
    return set(load_known_wing_rooms(path).keys())


def load_known_wing_rooms(path: Path = WINGS_PATH) -> dict[str, frozenset[str]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    wings = raw.get("wings") or {}
    if not isinstance(wings, dict):
        raise ValueError(f"wings config must contain a mapping: {path}")
    result: dict[str, frozenset[str]] = {}
    for wing_name, wing_def in wings.items():
        rooms_def = (wing_def or {}).get("rooms") if isinstance(wing_def, dict) else None
        rooms = rooms_def if isinstance(rooms_def, list) else []
        result[str(wing_name)] = frozenset(str(room) for room in rooms)
    return result


def _mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping")
    return dict(value)


Profile = AgentProfile
