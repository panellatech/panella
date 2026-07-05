"""Per-bridge freshness / SLO tracking for missed-source streaks + corpus staleness.

Captures two signals each bridge run (claude-bridge, codex-bridge, cc-sync)
should emit:

1. **Missed-source streak counter** — increments when a run exits with
   ``source_errors_count > 0``; resets to 0 when a run completes with
   ``source_errors_count == 0`` AND ``new_sessions_written > 0``. A streak
   of 3 (~72h) raises a P1 alert candidate.

2. **Corpus-staleness gauge** — last successful write timestamp per wing.
   When ``now - last_written_at > 7 days`` for any active wing, raises a
   P2 alert candidate (informational; agent may just be idle).

This module is the *tracking* layer — pure SQLite + a single
``record_run()`` entrypoint that returns ``list[AlertEvent]`` describing
which thresholds were crossed on this run. Actual alert delivery (POST to
default-daemon Gateway, Discord webhook, etc.) is a separate concern and is
wired by callers via the ``deliver`` callable injected at construction.

Tested surfaces:
- T1-T2: streak increment + reset logic
- T3: per-wing latest-write query
- T4-T5: threshold-cross emits expected alert objects
- T6: dedup — same streak does not re-emit on subsequent runs
- T10: opt-out via PANELLA_MEMORY_DISABLE_FRESHNESS_ALERTS env var
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STREAK_ALERT_THRESHOLD = 3  # consecutive failed runs
STALENESS_ALERT_THRESHOLD_DAYS = 7
ENV_DISABLE = "PANELLA_MEMORY_DISABLE_FRESHNESS_ALERTS"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridge_health (
    bridge_name TEXT PRIMARY KEY,
    source_outage_streak INTEGER NOT NULL DEFAULT 0,
    last_alerted_streak INTEGER NOT NULL DEFAULT 0,
    last_run_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wing_freshness (
    bridge_name TEXT NOT NULL,
    wing TEXT NOT NULL,
    last_written_at REAL NOT NULL,
    last_alerted_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (bridge_name, wing)
);
"""


@dataclass(frozen=True)
class AlertEvent:
    """Single alert candidate emitted by record_run().

    Caller decides routing (Gateway / webhook / log) via the ``deliver`` hook.
    """

    bridge_name: str
    severity: str  # "P1" | "P2"
    kind: str  # "source_outage_streak" | "corpus_stale_wing"
    message: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunSummary:
    """Outcome of a single bridge run handed to record_run()."""

    bridge_name: str
    source_errors_count: int
    new_sessions_written: int
    wings_written_at: dict[str, float] = field(default_factory=dict)
    # Defaults to time.time() when None at record_run() entry.
    now: float | None = None


@dataclass
class HealthState:
    bridge_name: str
    streak: int
    last_alerted_streak: int
    last_run_at: float


def _ensure_schema(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def _load_state(conn: sqlite3.Connection, bridge_name: str) -> HealthState:
    row = conn.execute(
        "SELECT source_outage_streak, last_alerted_streak, last_run_at "
        "FROM bridge_health WHERE bridge_name = ?",
        (bridge_name,),
    ).fetchone()
    if row is None:
        return HealthState(bridge_name=bridge_name, streak=0, last_alerted_streak=0, last_run_at=0.0)
    return HealthState(bridge_name=bridge_name, streak=row[0], last_alerted_streak=row[1], last_run_at=row[2])


def _persist_state(conn: sqlite3.Connection, state: HealthState) -> None:
    conn.execute(
        "INSERT INTO bridge_health (bridge_name, source_outage_streak, last_alerted_streak, last_run_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(bridge_name) DO UPDATE SET "
        "  source_outage_streak = excluded.source_outage_streak, "
        "  last_alerted_streak = excluded.last_alerted_streak, "
        "  last_run_at = excluded.last_run_at",
        (state.bridge_name, state.streak, state.last_alerted_streak, state.last_run_at),
    )


def _persist_wing_freshness(
    conn: sqlite3.Connection, bridge_name: str, wings_written_at: dict[str, float]
) -> None:
    """Upsert per-wing freshness. When last_written_at advances past the prior
    last_alerted_at watermark, reset last_alerted_at to 0 so a future staleness
    period can re-alert. This makes the alert-dedup gate at _query_stale_wings
    track "alerted since the most recent fresh write" rather than "alerted
    ever," which would otherwise keep wings silently stale-but-muted after
    they recovered + re-aged.
    """
    for wing, ts in wings_written_at.items():
        conn.execute(
            "INSERT INTO wing_freshness (bridge_name, wing, last_written_at, last_alerted_at) "
            "VALUES (?, ?, ?, 0) "
            "ON CONFLICT(bridge_name, wing) DO UPDATE SET "
            "  last_written_at = MAX(wing_freshness.last_written_at, excluded.last_written_at), "
            "  last_alerted_at = CASE "
            "    WHEN excluded.last_written_at > wing_freshness.last_written_at THEN 0 "
            "    ELSE wing_freshness.last_alerted_at "
            "  END",
            (bridge_name, wing, ts),
        )


def _query_stale_wings(
    conn: sqlite3.Connection, bridge_name: str, now: float, threshold_seconds: float
) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT wing, last_written_at, last_alerted_at FROM wing_freshness WHERE bridge_name = ?",
        (bridge_name,),
    ).fetchall()
    out: list[tuple[str, float]] = []
    for wing, last_written, last_alerted in rows:
        if now - last_written < threshold_seconds:
            continue
        # Dedup: don't re-alert if already alerted since the most recent write.
        if last_alerted >= last_written:
            continue
        out.append((wing, last_written))
    return out


def _mark_wings_alerted(
    conn: sqlite3.Connection, bridge_name: str, wings: list[str], now: float
) -> None:
    for wing in wings:
        conn.execute(
            "UPDATE wing_freshness SET last_alerted_at = ? WHERE bridge_name = ? AND wing = ?",
            (now, bridge_name, wing),
        )


def record_run(
    db_path: Path,
    summary: RunSummary,
    *,
    deliver: Callable[[AlertEvent], None] | None = None,
    streak_threshold: int = STREAK_ALERT_THRESHOLD,
    staleness_threshold_days: int = STALENESS_ALERT_THRESHOLD_DAYS,
) -> list[AlertEvent]:
    """Update bridge-health counters and return any alert candidates.

    ``deliver`` is an optional callback invoked once per alert emitted on
    this run (e.g., post to Gateway, log a structured event, send a webhook).
    Alerts are also returned to the caller for visibility / testing.

    Delivery contract: the alert-dedup watermarks (``last_alerted_streak``
    on bridge state, ``last_alerted_at`` on wing freshness) are advanced
    ONLY for alerts whose ``deliver`` callback returned without raising.
    When ``deliver`` is ``None`` the caller is treated as the delivery
    sink — alerts are returned by value and the watermarks advance
    eagerly. This prevents a single Gateway outage from permanently
    suppressing future alerts on the same streak level or wing because a
    pre-committed watermark would otherwise mute them once the dedup gate
    sees ``last_alerted >= current``.

    Honors the ``PANELLA_MEMORY_DISABLE_FRESHNESS_ALERTS`` env var: when set
    to a truthy value, returns ``[]`` and skips alert generation and
    delivery, but STILL persists streak/last_run_at/wing_freshness so
    healthy runs during a maintenance window correctly reset the outage
    streak and refresh ``last_written_at``. Otherwise removing the env
    var after a long quiet period would replay stale pre-maintenance
    alerts.
    """

    disabled = _env_disabled()
    if disabled:
        logger.info(
            "bridge_health alerts disabled via %s; tracking state only", ENV_DISABLE
        )

    _ensure_schema(db_path)
    now = summary.now if summary.now is not None else time.time()
    threshold_seconds = staleness_threshold_days * 86400.0

    alerts: list[AlertEvent] = []
    streak_alert: AlertEvent | None = None
    wing_alerts_by_wing: dict[str, AlertEvent] = {}

    # Phase 1: read prior state, build alert candidates, persist streak
    # progression + wing-freshness watermark. We do NOT advance the
    # alert-dedup floors yet — those wait until delivery succeeds (Phase 3).
    with sqlite3.connect(db_path) as conn:
        state = _load_state(conn, summary.bridge_name)
        had_clean_run = summary.source_errors_count == 0 and summary.new_sessions_written > 0
        if had_clean_run:
            new_streak = 0
            # Clean run resets dedup floor regardless of delivery — the run
            # itself is the signal, not an emitted alert.
            persisted_last_alerted = 0
        elif summary.source_errors_count > 0:
            new_streak = state.streak + 1
            persisted_last_alerted = state.last_alerted_streak
        else:
            # Run had no source errors but also no new writes (e.g., dry-run);
            # leave streak unchanged.
            new_streak = state.streak
            persisted_last_alerted = state.last_alerted_streak

        # Build streak alert candidate against the PRIOR dedup floor — its
        # watermark advance is deferred to Phase 3 (only on delivery success).
        if not disabled and new_streak >= streak_threshold and new_streak > state.last_alerted_streak:
            streak_alert = AlertEvent(
                bridge_name=summary.bridge_name,
                severity="P1",
                kind="source_outage_streak",
                message=(
                    f"{summary.bridge_name} source-outage streak={new_streak} "
                    f"(threshold={streak_threshold}); ~{new_streak * 24}h since last successful read"
                ),
                payload={
                    "bridge_name": summary.bridge_name,
                    "streak": new_streak,
                    "threshold": streak_threshold,
                },
            )
            alerts.append(streak_alert)

        # Persist streak/run state — dedup floor stays at prior value until
        # delivery succeeds for a fired alert.
        _persist_state(
            conn,
            HealthState(
                bridge_name=summary.bridge_name,
                streak=new_streak,
                last_alerted_streak=persisted_last_alerted,
                last_run_at=now,
            ),
        )
        _persist_wing_freshness(conn, summary.bridge_name, summary.wings_written_at)

        # Build wing-staleness alert candidates against the PRIOR per-wing
        # last_alerted_at watermark. Marking is deferred to Phase 3.
        if not disabled:
            for wing, last_written in _query_stale_wings(
                conn, summary.bridge_name, now, threshold_seconds
            ):
                age_days = (now - last_written) / 86400.0
                wing_alert = AlertEvent(
                    bridge_name=summary.bridge_name,
                    severity="P2",
                    kind="corpus_stale_wing",
                    message=(
                        f"{summary.bridge_name} wing={wing} stale; "
                        f"{age_days:.1f}d since last write (threshold={staleness_threshold_days}d)"
                    ),
                    payload={
                        "bridge_name": summary.bridge_name,
                        "wing": wing,
                        "age_days": age_days,
                        "threshold_days": staleness_threshold_days,
                    },
                )
                alerts.append(wing_alert)
                wing_alerts_by_wing[wing] = wing_alert
        conn.commit()

    if disabled:
        return []

    # Phase 2: attempt delivery; track per-alert success so dedup advancement
    # is gated on the alert actually reaching its sink. When deliver is None
    # the return value IS the sink, so every alert counts as delivered.
    delivered: set[int] = set()
    if deliver is None:
        delivered = {id(a) for a in alerts}
    else:
        for alert in alerts:
            try:
                deliver(alert)
            except Exception:
                logger.exception(
                    "bridge_health deliver hook failed for alert=%s; "
                    "leaving dedup watermark unchanged so a retry can re-fire",
                    alert,
                )
            else:
                delivered.add(id(alert))

    # Phase 3: advance dedup watermarks ONLY for successfully delivered
    # alerts. A streak alert that failed delivery leaves last_alerted_streak
    # at its prior value, so the next run with the same-or-higher streak
    # re-fires. Same per-wing for staleness.
    streak_delivered = streak_alert is not None and id(streak_alert) in delivered
    wings_delivered = [
        wing
        for wing, alert in wing_alerts_by_wing.items()
        if id(alert) in delivered
    ]

    if streak_delivered or wings_delivered:
        with sqlite3.connect(db_path) as conn:
            if streak_delivered:
                conn.execute(
                    "UPDATE bridge_health "
                    "SET last_alerted_streak = ? "
                    "WHERE bridge_name = ?",
                    (new_streak, summary.bridge_name),
                )
            if wings_delivered:
                _mark_wings_alerted(conn, summary.bridge_name, wings_delivered, now)
            conn.commit()

    for alert in alerts:
        logger.warning(
            "bridge_health_alert severity=%s kind=%s bridge=%s message=%s delivered=%s",
            alert.severity,
            alert.kind,
            alert.bridge_name,
            alert.message,
            id(alert) in delivered,
        )

    return alerts


def _env_disabled() -> bool:
    value = os.environ.get(ENV_DISABLE, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


# Default DB path used by sync-script callers. Resolved relative to the
# working directory the script runs from (systemd sets WorkingDirectory to
# /home/owner/panella, so this lands at /home/owner/panella/data/...).
DEFAULT_DB_PATH = Path("data") / "bridge_health.db"

# Optional webhook URL for forwarding alerts off-host. When set,
# default_deliver() POSTs a fixed JSON shape:
#
#   {"bridge_name": str, "severity": "P1"|"P2", "kind": str,
#    "message": str, "details": {...}}
#
# This is a GENERIC JSON endpoint shape — it does NOT speak Discord's
# {"content"|"embeds": ...} format or Telegram's
# {"chat_id", "text"} format directly. To route alerts to Discord /
# Telegram / PagerDuty, point this env var at a thin proxy (e.g. a
# small Cloudflare Worker, a local FastAPI shim, or a Panella Daemon
# Gateway endpoint) that translates the body into the provider's
# required schema. The proxy can also enrich (add a @here mention,
# format a Markdown table, etc.) without coupling those concerns to
# the bridge sync scripts. Setting the env var directly to a Discord
# or Telegram URL will repeatedly fail with WebhookDeliveryError,
# leaving alerts log-only — that is the safe fallback but not
# operationally useful.
#
# Env-driven (not config-toml) so ops can swap proxy URLs by editing
# the systemd EnvironmentFile and the next bridge run picks it up,
# no code changes / restart needed.
ENV_WEBHOOK_URL = "PANELLA_MEMORY_BRIDGE_HEALTH_WEBHOOK_URL"

# Discord webhook URL — when set, discord_deliver() formats the alert as
# Discord's {"content": "..."} schema and POSTs to it. EXPLICIT OPT-IN
# only: must set PANELLA_MEMORY_BRIDGE_HEALTH_DISCORD_WEBHOOK directly. We
# deliberately do NOT fall back to PANELLA_DISCORD_ALERTS_WEBHOOK because
# that would silently activate cross-channel bridge alerting on every
# deploy where Panella already uses Discord for other system alerts —
# users who didn't explicitly ask for bridge alerts on Discord shouldn't
# get them. Discord webhook URLs carry the token in the path —
# _redact_webhook_url() strips it in logs.
ENV_DISCORD_URL = "PANELLA_MEMORY_BRIDGE_HEALTH_DISCORD_WEBHOOK"

# Telegram delivery — when ``PANELLA_MEMORY_BRIDGE_HEALTH_TELEGRAM=1`` (or
# any truthy value) is set, telegram_deliver() reads TELEGRAM_BOT_TOKEN
# and TELEGRAM_CHAT_ID (already in the env file for Panella) and POSTs to
# https://api.telegram.org/bot<token>/sendMessage with the bridge alert
# formatted as a Markdown message.
ENV_TELEGRAM_ENABLE = "PANELLA_MEMORY_BRIDGE_HEALTH_TELEGRAM"
ENV_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"

# Timeout cap so a slow webhook does not delay the sync run materially.
_WEBHOOK_TIMEOUT_SECONDS = 3.0


def default_deliver(alert: AlertEvent) -> None:
    """Default per-alert delivery hook for bridge sync scripts.

    Reads the webhook URL from ``PANELLA_MEMORY_BRIDGE_HEALTH_WEBHOOK_URL``
    at call time (not module-import time, so systemd EnvironmentFile
    changes take effect on next run without restart). If the env var is
    unset or empty, this returns immediately — the alert is still logged
    via ``logger.warning`` in record_run()'s existing path, so behavior
    is unchanged from the pre-PR-#137 log-only default.

    The HTTP body is a fixed generic JSON shape (see ENV_WEBHOOK_URL
    comment). Discord / Telegram / PagerDuty do NOT accept this body
    directly — they need provider-specific schemas. Wire those via a
    thin translating proxy (Cloudflare Worker, local shim, Panella Daemon
    Gateway endpoint). If the env var points directly at a Discord or
    Telegram URL, default_deliver will repeatedly raise
    WebhookDeliveryError and record_run will leave the dedup watermark
    unadvanced — alerts stay log-only.

    On exception, the error is logged with the URL and exception text
    sanitized (WebhookDeliveryError wrapper, ``raise ... from None``)
    so secrets carried in the URL path can't leak into traceback
    formatting upstream. The wrapped exception IS re-raised so
    record_run's deliver-success gate leaves the dedup watermark
    unchanged — letting the next bridge run retry delivery instead of
    permanently muting a streak alert.
    """
    url = os.environ.get(ENV_WEBHOOK_URL, "").strip()
    if not url:
        return  # webhook not configured — log-only fallback (existing logger.warning)
    try:
        # httpx is already a dependency for panella.client / panella_adapter.
        import httpx
    except ImportError:
        logger.exception(
            "bridge_health webhook configured but httpx not available; "
            "leaving alert log-only"
        )
        return
    payload = {
        "bridge_name": alert.bridge_name,
        "severity": alert.severity,
        "kind": alert.kind,
        "message": alert.message,
        "details": alert.payload,
    }
    try:
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
    except Exception as exc:
        # Build a sanitized signature BEFORE raising. record_run()
        # catches deliver failures with logger.exception(...) which
        # formats the exception's str into the traceback. Since
        # httpx.HTTPStatusError.__str__() embeds the full request URL
        # (carrying the webhook token), re-raising the original would
        # leak the secret into bridge logs even though we redact the
        # explicit endpoint above. Wrap in a sanitized WebhookDeliveryError
        # whose str contains only the class name + status code.
        sanitized = _sanitize_exception_for_log(exc)
        logger.warning(
            "bridge_health webhook delivery failed endpoint=%s alert=%s err_type=%s; "
            "dedup watermark NOT advanced so next run will retry",
            _redact_webhook_url(url),
            alert.kind,
            sanitized,
        )
        # IMPORTANT: do NOT `raise` (which propagates the original exc
        # with its URL-leaking __str__) — raise a sanitized wrapper that
        # carries no URL. ``from None`` suppresses the original __cause__
        # so logger.exception() upstream cannot re-render the leaky text.
        raise WebhookDeliveryError(
            f"webhook delivery failed: {sanitized}"
        ) from None


class WebhookDeliveryError(RuntimeError):
    """Sanitized wrapper for webhook delivery failures.

    Carries only the exception class name + HTTP status code (if any).
    Deliberately NEVER includes the webhook URL or the original
    exception's __str__() because both can embed the secret token (e.g.
    httpx.HTTPStatusError formats the full request URL into its
    message). record_run() catches deliver failures via
    logger.exception(...) which expands the traceback chain, so the
    `from None` in default_deliver suppresses __cause__ to keep this
    sanitized message as the only line that hits the log.
    """


def _sanitize_exception_for_log(exc: BaseException) -> str:
    """Return the exception class name + status code (if applicable).

    Specifically AVOIDS rendering ``str(exc)`` because httpx exceptions
    embed the request URL in their string form, which would leak the
    webhook token. For HTTPStatusError we surface only the status code.
    For other exception types (ConnectError, ReadTimeout, etc.) we
    surface only the class name.
    """
    cls = type(exc).__name__
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return f"{cls}(status={status})"
    return cls


def _redact_webhook_url(url: str) -> str:
    """Return scheme://hostname[:port] for logging.

    Drops EVERY component that can carry a secret:
    - path/query/fragment (Discord/Telegram put their token here)
    - userinfo (``user:password@`` form embedded in netloc)

    Uses ``parsed.hostname`` (not ``parsed.netloc``) because netloc
    still contains ``user:token@`` when credentials are embedded in
    the authority component. Falls back to ``"<unparseable>"`` if
    urlparse fails or required fields are missing.
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname:
            return "<unparseable>"
        if parsed.port:
            return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        return f"{parsed.scheme}://{parsed.hostname}"
    except Exception:
        return "<unparseable>"


def _format_alert_markdown(alert: AlertEvent) -> str:
    """Format a bridge alert as a short Markdown message (used by both
    Discord and Telegram deliver paths). Keep it under 4000 chars to
    fit Telegram's sendMessage limit and Discord's 2000-char content
    limit for the common case. Detail payload is folded into a code
    block for readability.
    """
    severity_emoji = {"P1": "🔴", "P2": "🟡"}.get(alert.severity, "⚪")
    lines = [
        f"{severity_emoji} **bridge_health {alert.severity}** — `{alert.bridge_name}`",
        f"_{alert.kind}_: {alert.message}",
    ]
    if alert.payload:
        # Format key=value pairs, truncate values >120 chars
        details = []
        for k, v in alert.payload.items():
            sv = str(v)
            if len(sv) > 120:
                sv = sv[:117] + "..."
            details.append(f"{k}={sv}")
        if details:
            lines.append(f"```\n{chr(10).join(details)}\n```")
    return "\n".join(lines)


def discord_deliver(alert: AlertEvent) -> None:
    """Discord-specific delivery hook.

    EXPLICIT OPT-IN only: reads ``PANELLA_MEMORY_BRIDGE_HEALTH_DISCORD_WEBHOOK``.
    Does NOT fall back to other Discord webhook env vars (e.g.
    PANELLA_DISCORD_ALERTS_WEBHOOK) because that would silently activate
    cross-channel bridge alerting on every machine where Panella already
    uses Discord for other system alerts. Operators who want bridge
    alerts on their existing Discord channel can simply reuse the same
    URL value here — the explicit opt-in is the contract.

    POSTs ``{"content": <markdown>}`` which is Discord's required schema
    for plain-text webhooks. Failure handling and URL/exception
    sanitization mirror default_deliver(): WebhookDeliveryError wrapper,
    ``raise ... from None``, _redact_webhook_url() in log lines.
    """
    url = os.environ.get(ENV_DISCORD_URL, "").strip()
    if not url:
        return
    try:
        import httpx
    except ImportError:
        logger.exception(
            "bridge_health discord webhook configured but httpx not available; "
            "leaving alert log-only"
        )
        return
    payload = {"content": _format_alert_markdown(alert)}
    try:
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
    except Exception as exc:
        sanitized = _sanitize_exception_for_log(exc)
        logger.warning(
            "bridge_health discord delivery failed endpoint=%s alert=%s err_type=%s; "
            "dedup watermark NOT advanced so next run will retry",
            _redact_webhook_url(url),
            alert.kind,
            sanitized,
        )
        raise WebhookDeliveryError(f"discord delivery failed: {sanitized}") from None


def telegram_deliver(alert: AlertEvent) -> None:
    """Telegram-specific delivery hook.

    Activated when ``PANELLA_MEMORY_BRIDGE_HEALTH_TELEGRAM`` is set to a
    truthy value (1/true/yes/on) AND both ``TELEGRAM_BOT_TOKEN`` +
    ``TELEGRAM_CHAT_ID`` are configured. Uses the standard Telegram Bot
    API ``sendMessage`` endpoint with MarkdownV2-lite formatting.

    The bot token sits in the URL path; _redact_webhook_url() strips
    it from log lines. Per-message URL is built inside the function so
    a token rotation via systemd EnvironmentFile takes effect on the
    next bridge run without restart.
    """
    flag = os.environ.get(ENV_TELEGRAM_ENABLE, "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return
    token = os.environ.get(ENV_TELEGRAM_BOT_TOKEN, "").strip()
    chat_id = os.environ.get(ENV_TELEGRAM_CHAT_ID, "").strip()
    if not token or not chat_id:
        logger.warning(
            "bridge_health telegram enabled but TELEGRAM_BOT_TOKEN or "
            "TELEGRAM_CHAT_ID missing; leaving alert log-only"
        )
        return
    try:
        import httpx
    except ImportError:
        logger.exception(
            "bridge_health telegram configured but httpx not available; "
            "leaving alert log-only"
        )
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": _format_alert_markdown(alert),
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
    except Exception as exc:
        sanitized = _sanitize_exception_for_log(exc)
        # URL contains the bot token in the path — redact to scheme+host.
        logger.warning(
            "bridge_health telegram delivery failed endpoint=%s alert=%s err_type=%s; "
            "dedup watermark NOT advanced so next run will retry",
            _redact_webhook_url(url),
            alert.kind,
            sanitized,
        )
        raise WebhookDeliveryError(f"telegram delivery failed: {sanitized}") from None


def make_deliver() -> Callable[[AlertEvent], None]:
    """Construct the active per-alert deliver hook based on env vars.

    Priority (highest first):
    1. Generic JSON proxy (PANELLA_MEMORY_BRIDGE_HEALTH_WEBHOOK_URL set)
       → default_deliver  (for ops who run a Cloudflare Worker / shim)
    2. Discord webhook (PANELLA_MEMORY_BRIDGE_HEALTH_DISCORD_WEBHOOK or
       PANELLA_DISCORD_ALERTS_WEBHOOK set) → discord_deliver
    3. Telegram (PANELLA_MEMORY_BRIDGE_HEALTH_TELEGRAM=1 plus token+chat
       id) → telegram_deliver
    4. None of the above → log-only no-op (alerts still get the
       existing logger.warning line in record_run()).

    The dispatcher runs at sync-script main() time, so each bridge run
    reads fresh env state. Activating Discord or Telegram delivery is
    a one-line env-file edit; no code change needed.
    """
    if os.environ.get(ENV_WEBHOOK_URL, "").strip():
        return default_deliver
    if os.environ.get(ENV_DISCORD_URL, "").strip():
        return discord_deliver
    if os.environ.get(ENV_TELEGRAM_ENABLE, "").strip().lower() in {"1", "true", "yes", "on"}:
        return telegram_deliver

    def _log_only(_alert: AlertEvent) -> None:
        return

    return _log_only


def record_bridge_run(
    *,
    bridge_name: str,
    source_errors_count: int,
    new_sessions_written: int,
    written_wings: Iterable[str] = (),
    db_path: Path | None = None,
    deliver: Callable[[AlertEvent], None] | None = None,
    now: float | None = None,
) -> list[AlertEvent]:
    """Best-effort convenience wrapper for bridge sync scripts.

    Each sync script (claude_session_sync, codex_session_sync,
    cc_memory_sync) calls this after its run finishes to record
    freshness state and emit any threshold-crossing alerts.

    Parameter contract — CRITICAL:

    - ``written_wings`` MUST contain ONLY wings that received at least
      one ACTUAL successful write this run. Callers are responsible
      for filtering out dedup-skipped and scanned-but-unwritten wings;
      passing those here will silently false-refresh a stale wing's
      clock and suppress the corpus-staleness P2 alert. Recommended
      pattern: maintain a ``written_wings: set[str]`` field on the
      bridge's SyncResult, populated only at the actual write-success
      site (e.g. ``_record_stored(wing=...)``), then pass that set
      verbatim here.
    - ``source_errors_count`` is the count of SOURCE-READ errors only
      (SSH list/cat failures, parse failures). DO NOT pass an
      aggregate ``len(result.errors)`` that mixes summarizer / write
      / archive errors — that inflates the source_outage_streak.
    - ``new_sessions_written`` is the count of those real writes. When
      it is 0 (dry-run or all-dedup), the helper additionally short-
      circuits wing freshness update as a belt-and-suspenders gate so
      even a buggy caller cannot bump watermarks.

    The function:
    - Stamps ``now`` once as the ``last_written_at`` for every wing in
      ``written_wings``.
    - Ensures the parent directory exists so a fresh checkout doesn't
      crash on first run.
    - Catches every exception and logs it: alerting failures must not
      change the sync script's exit code.

    Returns the list of alerts that fired (empty on failure or
    disabled-env), so callers can log it for telemetry.
    """
    now = now if now is not None else time.time()
    target_db = (db_path or DEFAULT_DB_PATH).resolve()
    try:
        target_db.parent.mkdir(parents=True, exist_ok=True)
        # Defense in depth: even though callers promise written_wings
        # contains only real-write wings, also gate on new_sessions_written>0.
        # A buggy caller that passes a populated written_wings with
        # new_sessions_written=0 still cannot bump watermarks. In the happy
        # path this gate is a no-op because the same write events that
        # populated written_wings also incremented new_sessions_written.
        wings_written_at: dict[str, float] = {}
        if new_sessions_written > 0:
            seen: set[str] = set()
            for wing in written_wings:
                if not wing:
                    continue
                key = str(wing)
                if key in seen:
                    continue
                seen.add(key)
                wings_written_at[key] = now
        return record_run(
            target_db,
            RunSummary(
                bridge_name=bridge_name,
                source_errors_count=source_errors_count,
                new_sessions_written=new_sessions_written,
                wings_written_at=wings_written_at,
                now=now,
            ),
            deliver=deliver,
        )
    except Exception:
        logger.exception(
            "bridge_health record_bridge_run failed for bridge_name=%s; "
            "sync exit code unaffected",
            bridge_name,
        )
        return []
