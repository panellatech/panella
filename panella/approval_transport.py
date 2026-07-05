"""Pluggable approval transports for the memory approval queue (Slice-S P2).

The provenance gate in ``approval_finalizer.py`` trusts ONLY rows stamped by the deployment's
CONFIGURED transport (``governance approval.transport.kind``): ``approved_via`` must equal the
configured transport's name AND ``approved_by`` must be in the configured approver set. This module
owns the transport vocabulary — what names exist, how a presser is verified, and what provenance
string an authorized approval stamps — so the core carries no hardcoded ``'telegram'`` identity
coupling. Owner's deployment keeps ``daemon/telegram_approval_bot.py`` + ``config/memory.yaml``
and pins ``kind: telegram`` in the out-of-repo overlay; a self-host box ships ``local_cli``.

Fail-closed vocabulary: ``KNOWN_TRANSPORT_KINDS`` is the closed set the governance loader accepts —
an empty or unknown ``transport.kind`` is a load-time ``GovernanceConfigError``, never a silently
inert gate. This module imports nothing outside ``panella`` and the standard library
(governance-layer extractability — fence target).
"""

from __future__ import annotations

import hmac
import logging
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

TELEGRAM_TRANSPORT = "telegram"
LOCAL_CLI_TRANSPORT = "local_cli"
KNOWN_TRANSPORT_KINDS = frozenset({TELEGRAM_TRANSPORT, LOCAL_CLI_TRANSPORT})


class ApprovalTransportError(RuntimeError):
    """Raised when a transport cannot be constructed from its configured ``transport.config``."""


@runtime_checkable
class ApprovalTransport(Protocol):
    """The seam an approval channel implements.

    ``verify_presser`` maps a RAW presser credential (a Telegram ``from.id``, a presented local
    token, …) to the canonical ``approved_by`` identity string — or ``None`` when the presser is
    not authorized (fail-closed). ``stamp_provenance`` is the ``approved_via`` value an authorized
    approval stamps; the finalizer compares it against the CONFIGURED transport's name.
    """

    name: str

    def verify_presser(self, raw_presser: str) -> str | None: ...

    def stamp_provenance(self) -> str: ...


@dataclass(frozen=True)
class TelegramApprovalTransport:
    """Owner-style Telegram approval: an authenticated callback presser (``from.id``) must equal
    the single configured author id. The canonical approver identity is the PREFIXED form
    ``telegram:{chat_id}`` (``telegram_approval_bot._authorized_approvers``) — a bare chat id is
    never a valid ``approved_by``."""

    allowed_author_id: str
    name: str = field(default=TELEGRAM_TRANSPORT, init=False)

    def verify_presser(self, raw_presser: str) -> str | None:
        presser = str(raw_presser or "")
        if not self.allowed_author_id or presser != self.allowed_author_id:
            return None
        return f"{TELEGRAM_TRANSPORT}:{presser}"

    def stamp_provenance(self) -> str:
        return TELEGRAM_TRANSPORT


@dataclass(frozen=True)
class LocalCliApprovalTransport:
    """Self-host local approval: the presser presents the contents of an owner-held token file
    (created at provisioning, mode 0600). A missing/loose-permission/empty token file fails closed
    — every verification returns None until the box is provisioned correctly."""

    token_file: str
    token_mode: int = 0o600
    name: str = field(default=LOCAL_CLI_TRANSPORT, init=False)

    def _expected_token(self) -> str | None:
        path = Path(self.token_file).expanduser()
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & ~self.token_mode:
                logger.warning(
                    "local_cli approval token %s has loose permissions %o (want %o); refusing",
                    path, mode, self.token_mode,
                )
                return None
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("local_cli approval token unreadable (%s); refusing: %s", path, exc)
            return None
        return token or None

    def verify_presser(self, raw_presser: str) -> str | None:
        expected = self._expected_token()
        if expected is None or not raw_presser:
            return None
        if not hmac.compare_digest(str(raw_presser), expected):
            return None
        return f"{LOCAL_CLI_TRANSPORT}:owner"

    def stamp_provenance(self) -> str:
        return LOCAL_CLI_TRANSPORT


def build_transport(kind: str, config: Mapping[str, Any] | None = None) -> ApprovalTransport:
    """Construct the configured transport. Unknown/empty kind is an ``ApprovalTransportError``
    (the governance loader normally rejects those earlier — this is the belt to that suspender)."""
    cfg = dict(config or {})
    if kind == TELEGRAM_TRANSPORT:
        return TelegramApprovalTransport(allowed_author_id=str(cfg.get("allowed_author_id") or ""))
    if kind == LOCAL_CLI_TRANSPORT:
        token_file = str(cfg.get("token_file") or "")
        if not token_file:
            raise ApprovalTransportError("local_cli transport requires config.token_file")
        raw_mode = cfg.get("token_mode", "0600")
        try:
            token_mode = int(str(raw_mode), 8)
        except ValueError as exc:
            raise ApprovalTransportError(f"invalid local_cli token_mode: {raw_mode!r}") from exc
        return LocalCliApprovalTransport(token_file=token_file, token_mode=token_mode)
    raise ApprovalTransportError(f"unknown approval transport kind: {kind!r}")
