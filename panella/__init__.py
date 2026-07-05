"""Profile-driven memory boundary for Panella."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from panella.client import ApprovalRequired, MemoryClient, QuotaExceeded, TenantIsolationError, WriteResult
    from panella.principal import BreakGlassToken, Principal, TenantScope, principal_default_for_profile
    from panella.profile import AgentProfile

__all__ = [
    "AgentProfile",
    "ApprovalRequired",
    "BreakGlassToken",
    "MemoryClient",
    "Principal",
    "QuotaExceeded",
    "TenantScope",
    "TenantIsolationError",
    "WriteResult",
    "principal_default_for_profile",
]

_EXPORTS = {
    "AgentProfile": ("panella.profile", "AgentProfile"),
    "ApprovalRequired": ("panella.client", "ApprovalRequired"),
    "BreakGlassToken": ("panella.principal", "BreakGlassToken"),
    "MemoryClient": ("panella.client", "MemoryClient"),
    "Principal": ("panella.principal", "Principal"),
    "QuotaExceeded": ("panella.client", "QuotaExceeded"),
    "TenantScope": ("panella.principal", "TenantScope"),
    "TenantIsolationError": ("panella.client", "TenantIsolationError"),
    "WriteResult": ("panella.client", "WriteResult"),
    "principal_default_for_profile": ("panella.principal", "principal_default_for_profile"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
