"""TTL-scoped root elevation for memory tenant operations."""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from panella.audit import AUDIT_DB_PATH, audit_tail_hash, audit_write
from panella.principal import BreakGlassToken, Principal, root_principal

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG_CONFIG_PATH = ROOT / "doctor" / "watchdog.conf"

logger = logging.getLogger(__name__)


@contextmanager
def break_glass(
    reason: str,
    ttl_seconds: int = 600,
    requested_tenants: list[str] | None = None,
    *,
    caller: Principal | None = None,
    audit_db_path: str | Path = AUDIT_DB_PATH,
    watchdog_config_path: str | Path = WATCHDOG_CONFIG_PATH,
) -> Iterator[Principal]:
    """Root operator only (governance ``identity.root_principal``). Yields an elevated Principal
    for a bounded TTL."""

    if not str(reason or "").strip():
        raise ValueError("break-glass reason is required")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    tenants = list(requested_tenants or ["*"])
    principal = caller or root_principal()
    _validate_root_caller(principal)

    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    prev_hash = audit_tail_hash(audit_db_path)
    token = BreakGlassToken(
        reason=str(reason),
        issued_at=issued_at,
        expires_at=expires_at,
        audit_chain_prev_hash=prev_hash,
    )
    root = root_principal()
    elevated = Principal(
        id=root.id,
        tenant_id="*",
        subject_id=principal.subject_id,
        actor_kind="human",
        scopes=frozenset({"*"}),
        roles=root.roles,
        root_flag=True,
        break_glass_token=token,
    )
    audit_write(
        principal=elevated,
        tenant_accessed="*",
        op="break_glass_open",
        reason_code=str(reason),
        details={"ttl_seconds": ttl_seconds, "requested_tenants": tenants, "expires_at": expires_at.isoformat()},
        db_path=audit_db_path,
    )
    _notify_open_best_effort(elevated, tenants, watchdog_config_path=Path(watchdog_config_path))
    try:
        yield elevated
    finally:
        audit_write(
            principal=elevated,
            tenant_accessed="*",
            op="break_glass_close",
            reason_code=str(reason),
            details={"requested_tenants": tenants, "issued_at": issued_at.isoformat()},
            db_path=audit_db_path,
        )


def _validate_root_caller(principal: Principal) -> None:
    root_id = root_principal().id
    if principal.id != root_id or principal.actor_kind != "human":
        raise PermissionError(f"break-glass is restricted to {root_id}")
    if not principal.root_flag or "root_operator" not in principal.roles:
        raise PermissionError("break-glass requires root_operator role")
    if not principal.has_scope("*"):
        raise PermissionError("break-glass requires '*' scope")


def _notify_open_best_effort(
    principal: Principal,
    requested_tenants: list[str],
    *,
    watchdog_config_path: Path = WATCHDOG_CONFIG_PATH,
) -> None:
    token, chat_id = _load_watchdog_telegram_config(watchdog_config_path)
    if not token or not chat_id:
        return
    message = (
        "Memory break-glass opened\n"
        f"principal={principal.id}\n"
        f"tenants={','.join(requested_tenants)}\n"
        f"reason={principal.break_glass_token.reason if principal.break_glass_token else ''}\n"
        f"expires_at={principal.break_glass_token.expires_at.isoformat() if principal.break_glass_token else ''}"
    )
    thread = threading.Thread(target=_send_telegram, args=(token, chat_id, message), daemon=True)
    thread.start()


def _load_watchdog_telegram_config(path: Path = WATCHDOG_CONFIG_PATH) -> tuple[str, str]:
    token = ""
    chat_id = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.strip().startswith("#"):
                continue
            key, raw = line.split("=", 1)
            value = raw.strip().strip('"').strip("'")
            if key.strip() == "TELEGRAM_BOT_TOKEN":
                token = value
            elif key.strip() == "TELEGRAM_CHAT_ID":
                chat_id = value
    except OSError:
        return "", ""
    return token, chat_id


def _sanitize_telegram_exc(exc: BaseException) -> str:
    """Class name + HTTP status only — never str(exc)/exc.url, which embed the
    bot token (it lives in the URL path; the Telegram Bot API mandates that)."""
    cls = type(exc).__name__
    code = getattr(exc, "code", None)  # urllib.error.HTTPError
    if not isinstance(code, int):
        code = getattr(getattr(exc, "response", None), "status_code", None)  # httpx
    return f"{cls}(status={code})" if isinstance(code, int) else cls


def _send_telegram(token: str, chat_id: str, message: str) -> None:
    try:
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
        # Token stays in the URL path (Telegram Bot API mandate); kept out of
        # logs via _sanitize_telegram_exc in the failure branch below.
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            response.read()
    except Exception as exc:
        logger.debug("break-glass telegram notification failed: %s", _sanitize_telegram_exc(exc))
