"""Governance config loader for the memory boundary (Slice-S open-core product).

Loads the shipped generic ``config/governance.yaml`` and, when ``PANELLA_GOVERNANCE_OVERLAY`` points at
an out-of-repo overlay file, deep-merges it (overlay wins per key). The overlay is how a deployment
(e.g. owner's box) pins its own identity / approval / model-router values WITHOUT editing the
FF-deployed in-repo config — the base stays generic and FF-safe, the overlay survives ``git clean`` /
fresh worktrees (precedent: ``systemd/default-daemon.service`` ``EnvironmentFile=-%h/.config/panella/env``).

P1 exposes the ``model_router`` section (the LLM seam config). P2 layers identity / approval / paths
accessors on the SAME merged mapping. This module imports nothing outside ``panella`` and the
standard library (governance-layer extractability — it is a fence target).

Fail-loud by construction: a malformed config, or an overlay POINTER that is set but points at a
missing file, raises ``GovernanceConfigError`` rather than silently degrading to the generic base —
so a wrong deploy ordering (code before overlay) is a loud error, never a silent generic-identity serve.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from panella.approval_transport import KNOWN_TRANSPORT_KINDS

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOVERNANCE_PATH = Path(__file__).resolve().with_name("governance.yaml")
OVERLAY_ENV = "PANELLA_GOVERNANCE_OVERLAY"

# P2 fallbacks for an identity/approval/paths block a hand-rolled config omits entirely — these
# mirror the shipped generic config/governance.yaml, so "section absent" degrades to the same
# owner-neutral posture the base ships (NOT to any deployment's identity). A section that is
# PRESENT but malformed is a load-time GovernanceConfigError, never a silent fallback.
_GENERIC_ROOT_PRINCIPAL_ID = "human:owner"
_GENERIC_SUBJECT_ID = "u_owner"
_GENERIC_TENANT_ID = "t_owner_personal"
_GENERIC_TENANT_PREFIX = "t_"
_GENERIC_OWNER_LABEL = "Owner"
_GENERIC_OWNER_SLUG = "owner"
_GENERIC_OWNER_WING = "owner"
_GENERIC_TRANSPORT_KIND = "local_cli"
_GENERIC_TRANSPORT_CONFIG: Mapping[str, Any] = {
    "token_file": "~/.panella/approval.token",
    "token_mode": "0600",
}
_GENERIC_STORE_PATH = "~/.panella/sqlite_vec.db"
_GENERIC_EMBEDDINGS_ENV = "~/.panella/openai.env"


class GovernanceConfigError(RuntimeError):
    """Raised on a malformed governance config, or a set-but-missing overlay pointer (fail-loud)."""


@dataclass(frozen=True)
class RootPrincipalConfig:
    """The deployment's root operator identity (``identity.root_principal``)."""

    id: str
    subject_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class IdentityConfig:
    """The deployment's owner identity (``identity``) — the P2 de-Owner seam.

    ``content_owner_label`` / ``owner_slug`` / ``owner_wing`` template the DURABLE identity of new
    approval writes (content prefix, memory_type/event_type/source_system prefix, wing); a
    deployment overlay that reproduces the exact historical bytes keeps its corpus fork-free."""

    root_principal: RootPrincipalConfig
    default_tenant_id: str
    default_subject_id: str
    tenant_id_prefix: str
    content_owner_label: str
    owner_slug: str
    owner_wing: str


@dataclass(frozen=True)
class ApprovalConfig:
    """The approval channel config (``approval``). ``authorized_approvers`` empty = INERT-CLOSED
    (the finalizer keystone: nothing durable ever finalizes)."""

    authorized_approvers: tuple[str, ...]
    transport_kind: str
    transport_config: Mapping[str, Any]


@dataclass(frozen=True)
class PathsConfig:
    """Deployment path bindings (``paths``). ``config_dir`` empty/None = the in-repo ``config/``
    tree (a repo checkout); a packaged distribution points it at its rendered config dir."""

    store_path: str
    embeddings_env: str
    config_dir: str | None


@dataclass(frozen=True)
class Governance:
    """The merged (base + optional overlay) governance config.

    ``raw`` is the full merged mapping; typed section accessors read from it so P2 can add
    identity / approval / paths without changing the loader contract."""

    schema_version: int
    raw: Mapping[str, Any]

    @property
    def model_router(self) -> Mapping[str, Any]:
        """The ``model_router`` block (per-role provider chains). Empty mapping when absent."""
        block = self.raw.get("model_router")
        return block if isinstance(block, dict) else {}

    @property
    def identity(self) -> IdentityConfig:
        return _parse_identity(_section(self.raw, "identity"))

    @property
    def approval(self) -> ApprovalConfig:
        return _parse_approval(_section(self.raw, "approval"))

    @property
    def paths(self) -> PathsConfig:
        return _parse_paths(_section(self.raw, "paths"))


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    block = raw.get(key)
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise GovernanceConfigError(f"governance {key} must be a mapping, got {type(block).__name__}")
    return block


def _req_str(block: Mapping[str, Any], key: str, default: str, *, section: str) -> str:
    value = block.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise GovernanceConfigError(f"governance {section}.{key} must be a non-empty string, got {value!r}")
    return value.strip()


def _parse_identity(block: Mapping[str, Any]) -> IdentityConfig:
    rp_raw = block.get("root_principal")
    if rp_raw is None:
        rp_raw = {}
    if not isinstance(rp_raw, Mapping):
        raise GovernanceConfigError("governance identity.root_principal must be a mapping")
    roles_raw = rp_raw.get("roles", ["root_operator"])
    if not isinstance(roles_raw, list) or not all(isinstance(r, str) and r.strip() for r in roles_raw):
        raise GovernanceConfigError("governance identity.root_principal.roles must be non-empty strings")
    root = RootPrincipalConfig(
        id=_req_str(rp_raw, "id", _GENERIC_ROOT_PRINCIPAL_ID, section="identity.root_principal"),
        subject_id=_req_str(rp_raw, "subject_id", _GENERIC_SUBJECT_ID, section="identity.root_principal"),
        roles=tuple(r.strip() for r in roles_raw),
    )
    return IdentityConfig(
        root_principal=root,
        default_tenant_id=_req_str(block, "default_tenant_id", _GENERIC_TENANT_ID, section="identity"),
        default_subject_id=_req_str(block, "default_subject_id", _GENERIC_SUBJECT_ID, section="identity"),
        tenant_id_prefix=_req_str(block, "tenant_id_prefix", _GENERIC_TENANT_PREFIX, section="identity"),
        content_owner_label=_req_str(block, "content_owner_label", _GENERIC_OWNER_LABEL, section="identity"),
        owner_slug=_req_str(block, "owner_slug", _GENERIC_OWNER_SLUG, section="identity"),
        owner_wing=_req_str(block, "owner_wing", _GENERIC_OWNER_WING, section="identity"),
    )


def _parse_approval(block: Mapping[str, Any]) -> ApprovalConfig:
    approvers_raw = block.get("authorized_approvers", [])
    if approvers_raw is None:
        approvers_raw = []
    if not isinstance(approvers_raw, list) or not all(
        isinstance(a, str) and a.strip() for a in approvers_raw
    ):
        raise GovernanceConfigError(
            "governance approval.authorized_approvers must be a list of non-empty strings"
        )
    transport_raw = block.get("transport")
    if transport_raw is None:
        transport_raw = {}
    if not isinstance(transport_raw, Mapping):
        raise GovernanceConfigError("governance approval.transport must be a mapping")
    kind = transport_raw.get("kind", _GENERIC_TRANSPORT_KIND)
    # Fail-closed at LOAD: an empty or unknown transport name must never reach the finalizer gate
    # (where a typo'd kind would refuse every approval with no hint at the cause).
    if not isinstance(kind, str) or not kind.strip():
        raise GovernanceConfigError("governance approval.transport.kind must be a non-empty string")
    kind = kind.strip()
    if kind not in KNOWN_TRANSPORT_KINDS:
        raise GovernanceConfigError(
            f"governance approval.transport.kind {kind!r} is not a known transport "
            f"(known: {sorted(KNOWN_TRANSPORT_KINDS)})"
        )
    config_raw = transport_raw.get("config", _GENERIC_TRANSPORT_CONFIG if kind == _GENERIC_TRANSPORT_KIND else {})
    if config_raw is None:
        config_raw = {}
    if not isinstance(config_raw, Mapping):
        raise GovernanceConfigError("governance approval.transport.config must be a mapping")
    return ApprovalConfig(
        authorized_approvers=tuple(a.strip() for a in approvers_raw),
        transport_kind=kind,
        transport_config=dict(config_raw),
    )


def _parse_paths(block: Mapping[str, Any]) -> PathsConfig:
    config_dir_raw = block.get("config_dir")
    if config_dir_raw is not None and not isinstance(config_dir_raw, str):
        raise GovernanceConfigError("governance paths.config_dir must be a string or null")
    config_dir = config_dir_raw.strip() if isinstance(config_dir_raw, str) and config_dir_raw.strip() else None
    return PathsConfig(
        store_path=_req_str(block, "store_path", _GENERIC_STORE_PATH, section="paths"),
        embeddings_env=_req_str(block, "embeddings_env", _GENERIC_EMBEDDINGS_ENV, section="paths"),
        config_dir=config_dir,
    )


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive per-key merge — overlay wins. Nested mappings merge; any non-mapping (scalar/list)
    is replaced wholesale by the overlay's value."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise GovernanceConfigError(f"cannot read governance config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GovernanceConfigError(f"governance config must be a mapping: {path}")
    return data


def resolve_overlay_path(
    overlay_path: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """The overlay Path to merge, or None for pure-generic. Explicit ``overlay_path`` wins; otherwise
    read the ``PANELLA_GOVERNANCE_OVERLAY`` env pointer. An empty/unset pointer → None (generic base)."""
    env = env if env is not None else os.environ
    pointer = overlay_path if overlay_path is not None else env.get(OVERLAY_ENV)
    if not pointer:
        return None
    return Path(pointer).expanduser()


def load_governance(
    *,
    base_path: str | os.PathLike[str] = DEFAULT_GOVERNANCE_PATH,
    overlay_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> Governance:
    """Load the generic base config and deep-merge the resolved overlay (if any).

    - ``base_path`` default = the shipped ``config/governance.yaml``.
    - Overlay resolution: explicit ``overlay_path`` wins, else ``PANELLA_GOVERNANCE_OVERLAY``; unset → generic.
    - A set-but-missing overlay pointer raises ``GovernanceConfigError`` (fail-loud, not silent-generic).
    """
    base = _load_yaml_mapping(Path(base_path))
    overlay = resolve_overlay_path(overlay_path, env=env)
    merged: dict[str, Any] = base
    if overlay is not None:
        if not overlay.exists():
            raise GovernanceConfigError(
                f"{OVERLAY_ENV} points at a missing overlay: {overlay} — place the overlay BEFORE "
                "deploying code that reads it (fail-loud, not a silent generic-identity serve)"
            )
        merged = _deep_merge(base, _load_yaml_mapping(overlay))
    sv_raw = merged.get("schema_version", 1)
    # Type-strict: a str "1" or a bool True is NOT a valid schema_version (coercion would silently
    # accept a malformed config).
    if not isinstance(sv_raw, int) or isinstance(sv_raw, bool):
        raise GovernanceConfigError(f"schema_version must be an integer, got {sv_raw!r}")
    schema_version = sv_raw
    model_router = merged.get("model_router")
    if model_router is not None and not isinstance(model_router, dict):
        raise GovernanceConfigError(
            f"model_router must be a mapping, got {type(model_router).__name__}"
        )
    governance = Governance(schema_version=schema_version, raw=merged)
    # Force-parse the P2 sections at LOAD so a malformed identity/approval/paths block (or an
    # empty/unknown approval transport) fails loud at startup — never lazily inside a write path.
    _ = governance.identity, governance.approval, governance.paths
    return governance


# --------------------------------------------------------------------------- process-wide default
# The de-Ownered call sites (principal defaults, approval payload identity, finalizer gate, HTTP
# self-check) read the deployment's governance through this cached entry point rather than each
# threading a Governance instance through every signature. The cache is process-wide because the
# config is static at runtime (base = shipped file; overlay = one-time provisioning artifact);
# tests that swap PANELLA_GOVERNANCE_OVERLAY call reset_governance_cache().

_CURRENT_LOCK = threading.Lock()
_CURRENT: Governance | None = None


def current_governance() -> Governance:
    """The process-wide merged governance (loaded once, fail-loud). See module docstring for the
    overlay resolution contract."""
    global _CURRENT
    got = _CURRENT
    if got is not None:
        return got
    with _CURRENT_LOCK:
        if _CURRENT is None:
            _CURRENT = load_governance()
        return _CURRENT


def reset_governance_cache() -> None:
    """Drop the cached process-wide governance (tests / config reload)."""
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = None


CONFIG_DIR_ENV = "PANELLA_CONFIG_DIR"


def resolved_config_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """The config dir that holds agent profiles + wings.yaml, or None for the in-repo ``config/``.

    Precedence (mirrors the ``PANELLA_STORE_PATH`` env-over-governance precedent): the
    ``PANELLA_CONFIG_DIR`` env var wins (a packaged distribution points it at its rendered
    dist-config), else governance ``paths.config_dir``, else None (a repo checkout runs on its
    committed ``config/`` tree). Empty/unset at every layer → None."""
    env = env if env is not None else os.environ
    raw = env.get(CONFIG_DIR_ENV)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    config_dir = current_governance().paths.config_dir
    if config_dir:
        return Path(config_dir).expanduser()
    return None
