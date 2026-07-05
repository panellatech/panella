"""Principal and tenant identity primitives for the memory boundary.

Identity DEFAULTS (default tenant/subject, the root operator) are read from the deployment's
governance config (``panella/governance.py``) — the Slice-S de-Owner seam. The module-level
``DEFAULT_*`` constants below are retained ONLY as legacy test fallbacks / historical sentinels;
production call sites use ``default_tenant_id()`` / ``default_subject_id()`` / ``root_principal()``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from panella.governance import current_governance

TENANT_ID_RE = re.compile(r"^(?:t*[a-z0-9_]+|\*)$")


def default_tenant_id() -> str:
    """The deployment's default tenant id (``identity.default_tenant_id``)."""
    return current_governance().identity.default_tenant_id


def default_subject_id() -> str:
    """The deployment's default subject id (``identity.default_subject_id``)."""
    return current_governance().identity.default_subject_id


class PrincipalValidationError(ValueError):
    """Raised when a principal or tenant scope is malformed."""


@dataclass(frozen=True)
class BreakGlassToken:
    reason: str
    issued_at: datetime
    expires_at: datetime
    audit_chain_prev_hash: str

    def is_active(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return self.expires_at > current


@dataclass(frozen=True)
class TenantScope:
    tenant_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.tenant_ids:
            raise PrincipalValidationError("tenant scope must include at least one tenant")
        for tenant_id in self.tenant_ids:
            _validate_identity_id(tenant_id, field="tenant_id", allow_wildcard=True, root_flag=tenant_id == "*")

    @classmethod
    def from_principal(cls, principal: Principal) -> TenantScope:
        if principal.tenant_id == "*":
            return cls(("*",))
        return cls((principal.tenant_id,))

    def concrete_ids(self) -> tuple[str, ...] | None:
        if "*" in self.tenant_ids:
            return None
        return self.tenant_ids


@dataclass(frozen=True)
class Principal:
    id: str
    tenant_id: str
    subject_id: str
    actor_kind: Literal["agent", "human", "system"]
    scopes: frozenset[str]
    roles: frozenset[str]
    root_flag: bool = False
    break_glass_token: BreakGlassToken | None = None

    def __post_init__(self) -> None:
        _validate_identity_id(self.tenant_id, field="tenant_id", allow_wildcard=True, root_flag=self.root_flag)
        _validate_identity_id(self.subject_id, field="subject_id", allow_wildcard=False, root_flag=False)
        if self.actor_kind not in {"agent", "human", "system"}:
            raise PrincipalValidationError(f"invalid actor_kind: {self.actor_kind}")
        object.__setattr__(self, "scopes", frozenset(str(scope) for scope in self.scopes))
        object.__setattr__(self, "roles", frozenset(str(role) for role in self.roles))
        if self.tenant_id == "*" and not self.root_flag:
            raise PrincipalValidationError("tenant_id='*' is only valid for root principals")
        if self.break_glass_token is not None and not self.root_flag:
            raise PrincipalValidationError("break-glass token requires root_flag=True")

    @property
    def tenant_scope(self) -> TenantScope:
        return TenantScope.from_principal(self)

    @property
    def actor_id(self) -> str:
        return self.id.split("@", 1)[0]

    def has_scope(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise PermissionError(f"principal {self.id} lacks required scope: {scope}")

    def require_active_break_glass(self, now: datetime | None = None) -> None:
        if self.break_glass_token is None:
            return
        if not self.break_glass_token.is_active(now):
            raise PermissionError("break-glass token expired")

    def is_root_with_break_glass(self, now: datetime | None = None) -> bool:
        return bool(
            self.root_flag
            and self.tenant_id == "*"
            and self.break_glass_token is not None
            and self.break_glass_token.is_active(now)
        )


def principal_default_for_profile(profile: object) -> Principal:
    name = str(getattr(profile, "name", "unknown"))
    fallback_tenant = default_tenant_id()
    tenant_scope = tuple(str(item) for item in getattr(profile, "tenant_scope", [fallback_tenant]))
    tenant_id = tenant_scope[0] if tenant_scope else fallback_tenant
    if tenant_id == "*":
        raise PrincipalValidationError("agent default principal cannot use tenant_id='*'")
    return Principal(
        id=f"agent:{name}@{tenant_id}",
        tenant_id=tenant_id,
        subject_id=default_subject_id(),
        actor_kind="agent",
        scopes=frozenset({"memory.read", "memory.write"}),
        roles=frozenset({"agent_default"}),
    )


def root_principal() -> Principal:
    """The deployment's root operator principal, from governance ``identity.root_principal``."""
    identity = current_governance().identity
    return Principal(
        id=identity.root_principal.id,
        tenant_id="*",
        subject_id=identity.root_principal.subject_id,
        actor_kind="human",
        scopes=frozenset({"*"}),
        roles=frozenset(identity.root_principal.roles),
        root_flag=True,
    )



def _validate_identity_id(value: str, *, field: str, allow_wildcard: bool, root_flag: bool) -> None:
    text = str(value or "")
    if text == "*":
        if allow_wildcard and root_flag:
            return
        raise PrincipalValidationError(f"{field}='*' is only valid for root principals")
    if not TENANT_ID_RE.fullmatch(text):
        raise PrincipalValidationError(f"invalid {field}: {value!r}")
