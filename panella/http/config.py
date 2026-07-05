"""Runtime configuration for the memory HTTP facade."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from panella.audit import AUDIT_DB_PATH
from panella.client_raw import OUTBOX_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOKEN_DB_PATH = ROOT / "data" / "memory_tokens.db"


@dataclass(frozen=True)
class MemoryHttpConfig:
    token_db_path: Path = DEFAULT_TOKEN_DB_PATH
    audit_db_path: Path = AUDIT_DB_PATH
    outbox_db_path: Path = OUTBOX_DB_PATH
    profile_name: str = "default"
    host: str = "127.0.0.1"
    port: int = 8001
    rate_limit_per_minute: int = 100
    build_sha: str = "unknown"
    log_level: str = "INFO"
    # Panella store store probed by the startup coherence self-check (Slice-S P2 §1.5.3).
    # None → resolved at probe time: PANELLA_STORE_PATH env, else governance paths.store_path.
    store_path: Path | None = None
    # Slice-S P3b — network MCP mount (/mcp Streamable HTTP). Opt-in (default OFF) so owner's live
    # panella-http unit — whose env sets none of these — is byte-for-byte unchanged.
    mcp_enabled: bool = False
    mcp_profile: str = "mcp-read"
    # DNS-rebinding protection (SDK TransportSecuritySettings): the allowed Host/Origin sets for
    # the /mcp mount. Empty → loopback defaults resolved at mount time (see app.py). A deployment
    # exposing /mcp beyond loopback lists its host in PANELLA_MCP_ALLOWED_HOSTS.
    mcp_allowed_hosts: tuple[str, ...] = ()
    mcp_allowed_origins: tuple[str, ...] = ()


def load_config(config: MemoryHttpConfig | dict[str, Any] | None = None) -> MemoryHttpConfig:
    if isinstance(config, MemoryHttpConfig):
        return config
    values = dict(config or {})
    env_token_db = os.environ.get("PANELLA_HTTP_TOKEN_DB")
    env_audit_db = os.environ.get("PANELLA_HTTP_AUDIT_DB")
    env_outbox_db = os.environ.get("PANELLA_HTTP_OUTBOX_DB")
    env_log_level = os.environ.get("PANELLA_HTTP_LOG_LEVEL")
    env_rate_limit = os.environ.get("PANELLA_HTTP_RATE_LIMIT_PER_MINUTE")
    env_build_sha = os.environ.get("PANELLA_HTTP_BUILD_SHA") or os.environ.get("GIT_COMMIT")
    raw_store_path = values.get("store_path") or os.environ.get("PANELLA_STORE_PATH")

    return MemoryHttpConfig(
        store_path=Path(raw_store_path).expanduser() if raw_store_path else None,
        token_db_path=Path(values.get("token_db_path") or env_token_db or DEFAULT_TOKEN_DB_PATH),
        audit_db_path=Path(values.get("audit_db_path") or env_audit_db or AUDIT_DB_PATH),
        outbox_db_path=Path(values.get("outbox_db_path") or env_outbox_db or OUTBOX_DB_PATH),
        profile_name=str(values.get("profile_name") or os.environ.get("PANELLA_HTTP_PROFILE") or "default"),
        host=str(values.get("host") or "127.0.0.1"),
        port=int(values.get("port") or 8001),
        rate_limit_per_minute=int(values.get("rate_limit_per_minute") or env_rate_limit or 100),
        build_sha=str(values.get("build_sha") or env_build_sha or _git_sha()),
        log_level=str(values.get("log_level") or env_log_level or "INFO"),
        mcp_enabled=bool(values.get("mcp_enabled") if values.get("mcp_enabled") is not None
                         else _env_flag("PANELLA_MCP_ENABLED")),
        mcp_profile=str(values.get("mcp_profile") or os.environ.get("PANELLA_MCP_PROFILE") or "mcp-read"),
        mcp_allowed_hosts=_csv_tuple(values.get("mcp_allowed_hosts") or os.environ.get("PANELLA_MCP_ALLOWED_HOSTS")),
        mcp_allowed_origins=_csv_tuple(values.get("mcp_allowed_origins") or os.environ.get("PANELLA_MCP_ALLOWED_ORIGINS")),
    )


def _env_flag(name: str) -> bool:
    """A truthy env flag: '1'/'true'/'yes'/'on' (case-insensitive). Unset/empty/anything else = False."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _csv_tuple(value: Any) -> tuple[str, ...]:
    """Parse a comma-separated string (or pass a list/tuple through) into a tuple of trimmed
    non-empty strings. None/empty → ()."""
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _git_sha() -> str:
    head = ROOT / ".git" / "HEAD"
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_path = ROOT / ".git" / ref.removeprefix("ref: ").strip()
            return ref_path.read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"
