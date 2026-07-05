"""HTTP adapter for Panella store v10.31.2 (live OpenAPI 2026-05-03).

Contract pinned by tests/fixtures/panella_openapi_v10.31.2.json.

⚙️v6 auth: every outbound request sends ``X-API-Key`` (matches live middleware
``web/oauth/middleware.py:344-352`` accepting the legacy header even though
OpenAPI advertises only ``HTTPBearer``).

Tag namespace mapping (write-side): wing/room/agent/memory_type/tenant_id are
encoded as ``wing:<>``, ``room:<>``, ``agent:<>``, ``mtype:<>``,
``tenant:<>`` plus ``status:active``. Read-side ``_normalize_hit`` parses
these back; legacy untagged rows fall back to ``wing="knowledge",
room="legacy", tenant_id=<governance default_tenant_id>``.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from panella.principal import default_tenant_id
from panella.wing_boost import wing_boost
from panella.sanitize import sanitize
from panella.tag_lock import tag_lock

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_KEY_FILE = Path.home() / ".config" / "panella" / "panella-api-key"
ENV_API_KEY = "PANELLA_API_KEY"
# ⚙️v6 P2 — explicit key-file override so containerized callers can point at a
# host-mounted path without relying on Path.home() (which resolves to /root in
# Docker per CLAUDE.md). Honored only when the inline env var is unset.
ENV_API_KEY_FILE = "PANELLA_API_KEY_FILE"
LEGACY_FALLBACK_WING = "knowledge"
LEGACY_FALLBACK_ROOM = "legacy"


def legacy_fallback_tenant() -> str:
    """The tenant legacy UNTAGGED rows are attributed to — the deployment's default tenant
    (``governance identity.default_tenant_id``; owner overlay → the historical value)."""
    return default_tenant_id()

NAMESPACED_TAG_KEYS = ("wing", "room", "agent", "mtype", "tenant", "status")
# Mirror upstream's per-memory tag-count cap (services/memory_service.py:136 keeps
# only the first N tags on store). Panella clamps client-side with the never-lose
# guards FIRST so that cap can never silently truncate `permanent`/`status:active`.
PANELLA_MAX_TAGS_PER_MEMORY = 100
# Max active rows find_active_hashes_by_tag will enumerate for one tag before failing closed on
# overflow (it treats len(results) >= this as "more than fetched"). It bounds how many atoms a
# single cc-sync source can have active: a source's atom set must stay STRICTLY below this so the
# no-op/verify/delete-prior reads can always enumerate the full set. replace_source_atom_set
# rejects an over-cap set BEFORE writing (so it never stores an un-enumerable, un-deletable set).
FIND_ACTIVE_HASHES_LIMIT = 200
RETRYABLE_STATUS = {500, 502, 503, 504}
EXCLUDED_RECALL_STATUSES = frozenset({"candidate", "superseded", "tombstoned"})
# Over-fetch budget for the soft-boost reshuffle AND the Phase-1 validity-drop
# backfill. Sized at 40 to give the HTTP-mode adapter enough headroom for the
# soft-boost reshuffle. Applied UNCONDITIONALLY in search_memories (see there):
# the validity drop runs on every search, so the backfill pool is always needed.
PANELLA_OVERFETCH_N = 40


class PanellaAuthMissing(RuntimeError):  # noqa: N818 - public contract name from v6 auth tests.
    """Raised when no Panella store API key can be resolved."""


class PanellaDedupSkipped(RuntimeError):  # noqa: N818 - public contract name for bridge callers.
    """Raised when a memory write is skipped because content is already present.

    ``existing_hash`` carries the upstream first-writer's content hash when Panella store
    discloses it (semantic-duplicate responses do; exact-duplicate responses do not).
    ``duplicate_kind`` is one of ``"exact"``, ``"semantic"``, or ``"unknown"``.
    """

    def __init__(
        self,
        message: str,
        *,
        existing_hash: str | None = None,
        duplicate_kind: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.existing_hash = existing_hash
        self.duplicate_kind = duplicate_kind


class PanellaUnknownWriteOutcome(RuntimeError):  # noqa: N818 - public contract name for bridge callers.
    """Raised when a memory write completes without a usable drawer identifier."""


def resolve_panella_api_key(cli_arg: str | None = None) -> str:
    """⚙️v6 precedence: CLI ``--api-key`` > env ``PANELLA_API_KEY`` > 0600 file.

    Exits non-zero with a single human-readable message when nothing resolves.
    Never logs or returns the value back to caller for printing.
    """

    if cli_arg:
        return cli_arg.strip()
    env_value = os.environ.get(ENV_API_KEY)
    if env_value and env_value.strip():
        return env_value.strip()
    override = os.environ.get(ENV_API_KEY_FILE)
    path = Path(override) if override else DEFAULT_KEY_FILE
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise PanellaAuthMissing(
                f"{path} mode is {oct(mode)}; require 0600 (chmod 600 to fix)"
            )
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise PanellaAuthMissing(
        "missing Panella store API key: pass --api-key, set PANELLA_API_KEY, "
        f"set {ENV_API_KEY_FILE}=<path>, or create {DEFAULT_KEY_FILE} mode 0600"
    )


def _resolve_cf_access_headers() -> dict[str, str]:
    """Cloudflare Access service-token headers for headless clients.

    Returns both ``CF-Access-Client-Id`` / ``CF-Access-Client-Secret`` headers
    when BOTH env vars (``CF_ACCESS_CLIENT_ID`` + ``CF_ACCESS_CLIENT_SECRET``)
    are set — the Mac drain crossing CF Access at memory.example.invalid.com. Returns
    ``{}`` otherwise: VPS-localhost callers (cc-sync / claude-bridge /
    codex-bridge) set neither, so their request headers stay byte-identical to
    pre-CF behavior. A half-config (exactly one set) is a misconfiguration:
    warn by env-var NAME only (never the value) and send neither, so we never
    half-authenticate.
    """
    cf_id = (os.environ.get("CF_ACCESS_CLIENT_ID") or "").strip()
    cf_token = (os.environ.get("CF_ACCESS_CLIENT_SECRET") or "").strip()
    if cf_id and cf_token:
        return {"CF-Access-Client-Id": cf_id, "CF-Access-Client-Secret": cf_token}
    if cf_id or cf_token:
        missing = "CF_ACCESS_CLIENT_SECRET" if cf_id else "CF_ACCESS_CLIENT_ID"
        logger.warning(
            "panella_cf_access_half_config: %s missing; need both "
            "CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET set together "
            "(sending neither)",
            missing,
        )
    return {}


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_namespaced_tag(tags: Iterable[str], key: str) -> tuple[str | None, list[str]]:
    """Return ``(last_value, all_values)`` for ``key:`` tags. LWW + warn on multi."""

    prefix = f"{key}:"
    matches = [t[len(prefix):] for t in tags if isinstance(t, str) and t.startswith(prefix)]
    if len(matches) > 1:
        logger.warning(
            "memory_hit_multiple_%s_tags chosen=%s all=%s",
            key,
            matches[-1],
            matches,
        )
    return (matches[-1] if matches else None), matches


def _duplicate_info(payload: dict[str, Any]) -> tuple[bool, str | None, str]:
    """Inspect an Panella store POST response for duplicate-rejection signals.

    Returns ``(is_duplicate, existing_hash, duplicate_kind)``. ``existing_hash``
    is the first-writer's 64-char content hash when Panella store discloses it in the
    message (semantic-similar responses do; exact-match responses do not).
    ``duplicate_kind`` is ``"exact"``, ``"semantic"``, or ``"unknown"``.
    """

    if payload.get("success") is not False:
        return False, None, "unknown"
    message = str(payload.get("message") or "")
    lowered = message.lower()
    if "duplicate content" not in lowered:
        return False, None, "unknown"
    if "semantically similar" in lowered:
        kind = "semantic"
    elif "exact match" in lowered:
        kind = "exact"
    else:
        kind = "unknown"
    match = re.search(r"\b([0-9a-f]{64})\b", message, re.IGNORECASE)
    existing_hash = match.group(1) if match else None
    return True, existing_hash, kind


def _has_excluded_recall_status(hit: dict[str, Any]) -> bool:
    """Deny recall for explicit non-recall lifecycle statuses.

    This is deliberately deny-based instead of requiring ``status:active`` so
    legacy memories with no status tag remain visible. Check raw tags as well
    as normalized status; a deny tag must win even if stale metadata says
    ``status=active``.
    """

    status = str(hit.get("status") or "").lower()
    if status in EXCLUDED_RECALL_STATUSES:
        return True
    tags = hit.get("tags") or []
    return any(
        isinstance(tag, str)
        and tag.startswith("status:")
        and tag.split(":", 1)[1].lower() in EXCLUDED_RECALL_STATUSES
        for tag in tags
    )


class PanellaAdapter:
    """⚙️v6 Panella store HTTP adapter with X-API-Key transport auth."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        api_key: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
    ) -> None:
        if not api_key or not str(api_key).strip():
            raise ValueError("PanellaAdapter requires non-empty api_key (⚙️v6)")
        self.base_url = base_url.rstrip("/")
        self.api_key = str(api_key).strip()
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        # Resolved once per process. The Mac drain is a fresh process every 60s
        # (launchd sources the env file before exec), so env is stable per
        # instance; no need to re-read per request.
        self._cf_headers = _resolve_cf_access_headers()
        if client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(
                    timeout=self.timeout,
                    connect=min(5.0, self.timeout),
                    read=self.timeout,
                    write=self.timeout,
                    pool=min(5.0, self.timeout),
                ),
                headers={"X-API-Key": self.api_key},
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    # ------------------------------------------------------------------
    # HTTP plumbing — every outbound request goes through these helpers so
    # the X-API-Key header is universal.

    def _headers(self) -> dict[str, str]:
        # X-API-Key is the Panella store application auth. CF-Access-* (when configured
        # via env) let a headless client pass the Cloudflare Access edge; empty
        # for localhost callers, so their requests are unchanged. Emitted here —
        # not just on the owned client's defaults — so injected-client callers
        # (and every request path) carry them too.
        return {"X-API-Key": self.api_key, **self._cf_headers}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        """Issue an Panella store HTTP request with transient-error retry.

        Idempotency invariant: this helper retries POST as well as GET on
        TimeoutException/TransportError and on RETRYABLE_STATUS. That is
        only safe because Panella store performs server-side content-hash dedup
        on every write — a retried POST that already landed is absorbed
        as ``dedup_skipped`` rather than producing a duplicate drawer. If
        the Panella store write contract ever loses content-hash dedup, this
        retry policy must be narrowed to safe (idempotent) methods.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(self.retry_backoff * (2 ** attempt))
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                time.sleep(self.retry_backoff * (2 ** attempt))
                continue
            return response
        if last_exc:
            raise last_exc
        raise RuntimeError(f"panella request retries exhausted: {method} {path}")

    def _http_get(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    def _http_post(self, path: str, *, json: Any | None = None) -> httpx.Response:
        return self._request("POST", path, json_body=json)

    def _http_put(self, path: str, *, json: Any | None = None) -> httpx.Response:
        return self._request("PUT", path, json_body=json)

    def _http_delete(self, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("DELETE", path, params=params)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> PanellaAdapter:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API used by MemoryClient + scripts

    def health(self) -> dict[str, Any]:
        resp = self._http_get("/api/health")
        resp.raise_for_status()
        return dict(resp.json() or {})

    def add_memory(
        self,
        wing: str,
        room: str,
        content: str,
        metadata: dict[str, Any],
        *,
        sanitize_content: bool = True,
        conversation_id: str | None = None,
    ) -> str:
        # G1-A (never-leak): sanitize at the universal Panella store POST boundary so
        # EVERY write — including direct-adapter callers that bypass
        # MemoryClient.write — is redacted-and-kept before it leaves the host.
        # sanitize() is idempotent (re-running on already-redacted text is a
        # no-op), so the MemoryClient.write()-top sanitize double-pass is
        # harmless. The G0-B recovery re-POST passes sanitize_content=False: it
        # restores the EXACT pre-existing content so the re-POST's content hash
        # matches the tombstone it must purge (sanitizing would fork the hash).
        if sanitize_content:
            content = sanitize(content)
        meta = dict(metadata or {})
        memory_type = str(meta.get("memory_type") or "observation")
        agent = str(meta.get("agent") or meta.get("agent_profile") or "unknown")
        tenant_id = str(meta.get("tenant_id") or legacy_fallback_tenant())
        # G0-A (never-lose): tag every status:active write `permanent` so upstream
        # forgetting skips it (consolidation/base.py:165 protected_tags includes
        # 'permanent'; forgetting.py:136 skips _is_protected_memory). `permanent`
        # is the least-perturbing protected tag — absent from tag-importance
        # scoring (decay.py) and the compression-preserved set. importance_score
        # is a secondary belt (a finite decay floor); setdefault lets an explicit
        # caller value win.
        explicit_tags = list(meta.pop("tags", []) or [])
        # G0-A robustness (truncation-proof guards): upstream keeps only the first
        # PANELLA_MAX_TAGS_PER_MEMORY tags on store (memory_service.py:136). Emit the
        # never-lose guards + tenant/taxonomy FIRST, then clamp to that cap, so a
        # pathological tag-flood truncates excess *semantic* tags — never `permanent`
        # /`status:active` (silently un-protecting the row from forgetting) nor the
        # tenant tag (breaking multi-tenant isolation).
        tags = list(dict.fromkeys([
            "permanent",
            "status:active",
            f"wing:{wing}",
            f"room:{room}",
            f"agent:{agent}",
            f"mtype:{memory_type}",
            f"tenant:{tenant_id}",
            *explicit_tags,
        ]))[:PANELLA_MAX_TAGS_PER_MEMORY]
        meta.setdefault("importance_score", 2.0)
        body = {
            "content": content,
            "tags": tags,
            "memory_type": memory_type,
            "metadata": {**meta, "wing": wing, "room": room, "agent": agent, "tenant_id": tenant_id},
        }
        # G0-B recovery: a unique conversation_id makes upstream skip SEMANTIC dedup
        # (memory_service.py:389 skip_dedup=bool(conversation_id)); exact-hash dedup
        # still runs, but a tombstoned row's hash is excluded (deleted_at IS NULL
        # filter), so the re-POST purges the tombstone + regenerates the embedding.
        # None on the hot path → body byte-identical to pre-Phase-2.
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        resp = self._http_post("/api/memories", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        is_duplicate, dup_hash, dup_kind = _duplicate_info(payload)
        if is_duplicate:
            raise PanellaDedupSkipped(
                str(payload.get("message") or "Duplicate content detected"),
                existing_hash=dup_hash,
                duplicate_kind=dup_kind,
            )
        content_hash = payload.get("content_hash")
        if not content_hash and isinstance(payload.get("memory"), dict):
            content_hash = payload["memory"].get("content_hash")
        if not content_hash:
            raise PanellaUnknownWriteOutcome(
                f"add_memory: missing content_hash in response: {payload!r}"
            )
        return str(content_hash)

    # MemoryClient looks up either ``add_memory`` or ``add_drawer``.
    add_drawer = add_memory

    def search_memories(
        self,
        query: str,
        *,
        k: int = 5,
        wings_hint: list[str] | None = None,
        retrieval_mode: str | None = None,
        tenant_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        # k is already clamped upstream in MemoryClient.search (client.py:90)
        # against the caller's profile.max_query_k. No adapter-level cap needed.
        # Over-fetch UNCONDITIONALLY: the lifecycle validity drop (status ∈
        # EXCLUDED_RECALL_STATUSES, below) runs on EVERY search and can remove
        # top-k hits, so we always fetch a backfill pool — otherwise a superseded
        # hit inside the raw top-k underfills recall (<k) even when more active
        # hits exist lower in the ranking. This supersedes PR #141's
        # wings_hint-only over-fetch gate: the load delta (40 vs k) is negligible
        # on the local sqlite_vec store, and the main MemoryClient.search path
        # already over-fetched (it always passes a non-empty wings_hint — see
        # client.py:96), so only direct-adapter callers like eval/runner.py
        # (which calls with wings_hint=None) change. Resolves Codex bot P2 on
        # PR #197.
        fetch_n = max(k, PANELLA_OVERFETCH_N)
        body: dict[str, Any] = {"query": query, "n_results": fetch_n}
        resp = self._http_post("/api/search", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        raw_results = payload.get("results") or []
        boosted: list[dict[str, Any]] = []
        for sr in raw_results:
            if not isinstance(sr, dict):
                continue
            mem = sr.get("memory") or {}
            if not isinstance(mem, dict) or not mem:
                continue
            normalized = self._normalize_hit(mem)
            # Phase 1 §3.0/§3.3 — validity drop (load-bearing for backfill).
            # Exclude non-recall lifecycle statuses BEFORE the sort+[:k] slice so the
            # over-fetch pool (fetch_n) backfills the active top-k instead of
            # underfilling. This is deny-based, not status:active-required, so
            # legacy/untagged rows stay visible.
            if _has_excluded_recall_status(normalized):
                continue
            # Preserve scoreless-hit behavior: only convert similarity_score
            # when present. Do NOT drop on missing score — same as pre-soft-boost.
            score_raw = sr.get("similarity_score")
            if score_raw is not None:
                with suppress(TypeError, ValueError):
                    normalized["score"] = float(score_raw)
            # Tenant filter remains hard — privacy gate at adapter level.
            if tenant_ids and "*" not in tenant_ids and normalized.get("tenant_id") not in set(tenant_ids):
                continue
            # Wing soft boost — multiplicative on score. hybrid.wing_boost
            # returns HINT_WING_BOOST_FACTOR=1.5 on-hint, OUT_OF_HINT_WING_FACTOR=0.7
            # off-hint, 1.0 when no hint. Only apply when the hit has a score.
            if wings_hint and "score" in normalized:
                normalized["score"] *= wing_boost(normalized.get("wing") or "", wings_hint)
            boosted.append(normalized)
        # Sort by score where present; scoreless hits sort to the back via -inf surrogate.
        boosted.sort(
            key=lambda h: float(h.get("score") if h.get("score") is not None else float("-inf")),
            reverse=True,
        )
        return boosted[:k]

    def get_drawer(self, drawer_id: str) -> dict[str, Any] | None:
        resp = self._http_get(f"/api/memories/{drawer_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, dict) or "content_hash" not in raw:
            return None
        return self._normalize_hit(raw)

    def _tag_transition(
        self,
        drawer_id: str,
        *,
        drop_tags: set[str],
        add_tags: list[str],
        meta_delta_fn: Any,
        max_attempts: int = 2,
    ) -> bool:
        """G1-B — the one locked GET-merge-PUT primitive for tag-status transitions.

        Holds the cross-process ``tag_lock`` for the whole GET→PUT→verify so two
        Panella writers can't interleave and clobber each other's tag delta (Panella store
        tags are full-REPLACEMENT). Tags are GET-merge-PUT (strip ``drop_tags``,
        append ``add_tags``, PRESERVE every other tag). Metadata is a DELTA only
        (``meta_delta_fn(existing_meta)``): upstream ``update_memory_metadata``
        MERGES it into the *current* row (sqlite_vec.py:2637-2653), so a
        concurrent metadata write (e.g. consolidation) is not clobbered by our
        possibly-stale GET. After PUT we re-GET and verify the transition landed
        (drop_tags gone, add_tags present, other tags preserved); on mismatch we
        retry the whole locked GET-merge-PUT, then alert + return False.

        Returns False on 404 (no such live row). True means the row is VERIFIED in
        the target state — idempotent: a re-run on an already-transitioned row also
        returns True; it does not assert THIS call issued the winning PUT.
        ``meta_delta_fn`` receives the freshest GET's metadata each attempt (so e.g.
        supersede's valid_to idempotency keys on the current value).
        """
        with tag_lock(drawer_id):
            last_problem: str | None = None
            for _attempt in range(max_attempts):
                resp = self._http_get(f"/api/memories/{drawer_id}")
                if resp.status_code == 404:
                    return False
                resp.raise_for_status()
                existing = resp.json()
                if not isinstance(existing, dict) or "content_hash" not in existing:
                    raise RuntimeError(f"unexpected GET shape for {drawer_id}: {existing!r}")
                existing_tags = list(existing.get("tags") or [])
                existing_meta = dict(existing.get("metadata") or {})

                kept = [t for t in existing_tags if t not in drop_tags]
                new_tags = list(dict.fromkeys(kept + list(add_tags)))
                meta_delta = dict(meta_delta_fn(existing_meta))

                put_resp = self._http_put(
                    f"/api/memories/{drawer_id}",
                    json={"tags": new_tags, "metadata": meta_delta},
                )
                put_resp.raise_for_status()
                if not bool((put_resp.json() or {}).get("success", False)):
                    last_problem = "PUT success=False"
                    continue

                # Re-GET verify: the transition must be visible on the row.
                verify_resp = self._http_get(f"/api/memories/{drawer_id}")
                verify_resp.raise_for_status()
                vtags = set((verify_resp.json() or {}).get("tags") or [])
                preserved = set(kept)
                if (
                    set(add_tags) <= vtags
                    and not (drop_tags & vtags)
                    and preserved <= vtags
                ):
                    return True
                last_problem = f"verify mismatch: tags={sorted(vtags)}"
            logger.warning(
                "panella_tag_transition_failed drawer=%s after %d attempts: %s",
                drawer_id,
                max_attempts,
                last_problem,
            )
            return False

    def tombstone(self, drawer_id: str, reason: str, *, principal: Any) -> bool:
        """⚙️v3/v4 — locked GET-merge-PUT (G1-B). Strips status:active, appends
        status:tombstoned, preserves every other tag; metadata DELTA only."""

        tombstoned_by = getattr(principal, "id", None) or "unknown"
        return self._tag_transition(
            drawer_id,
            drop_tags={"status:active"},
            add_tags=["status:tombstoned"],
            meta_delta_fn=lambda _meta: {
                "status": "tombstoned",
                "tombstoned_at": _utcnow_iso(),
                "tombstone_reason": reason,
                "tombstoned_by": tombstoned_by,
            },
        )

    def supersede(
        self,
        drawer_id: str,
        reason: str,
        *,
        superseded_by: str | None = None,
        principal: Any,
    ) -> bool:
        """Phase 1 §3.2 — mark a memory superseded WITHOUT deleting it. NEVER
        DELETEs, NEVER sets deleted_at. Strips status:active, appends
        status:superseded, PRESERVES all other tags. Sets metadata
        status="superseded" + valid_to + supersede provenance.

        G1-B: this now runs through the locked ``_tag_transition`` (cross-process
        serialization + re-GET verify) and PUTs a metadata DELTA (upstream merges
        it, avoiding clobber of concurrent metadata writes).

        ``valid_to`` is ``existing_meta.get("valid_to") or _utcnow_iso()`` — the
        idempotency guard (P0-2): a retry (transient-error or verify-retry)
        re-GETs valid_to already set and leaves it unchanged; a distinct
        re-supersede of an already-superseded memory preserves the original
        valid_to while updating superseded_reason / superseded_by_ref.
        """
        del principal  # supersede provenance is recorded client-side (memory_history)
        return self._tag_transition(
            drawer_id,
            drop_tags={"status:active"},
            add_tags=["status:superseded"],
            meta_delta_fn=lambda existing_meta: {
                "status": "superseded",
                "valid_to": existing_meta.get("valid_to") or _utcnow_iso(),
                "superseded_reason": reason,
                "superseded_by_ref": superseded_by,
            },
        )

    def ensure_tag(self, drawer_id: str, tag: str) -> bool:
        """G0-A backfill primitive — idempotently add ``tag`` to a live row via
        the locked GET-merge-PUT, preserving every existing tag. No status flip,
        no metadata change (empty delta). Idempotent: a row that already carries
        ``tag`` re-PUTs the same tag set and verifies True. Used by the
        ``panella_backfill_permanent_tag`` script to make the ~5,553 live
        status:active rows forgetting-immune before the next monthly forgetting.
        """
        return self._tag_transition(
            drawer_id,
            drop_tags=set(),
            add_tags=[tag],
            meta_delta_fn=lambda _meta: {},
        )

    def hard_delete(self, drawer_id: str, reason: str, *, principal: Any) -> bool:
        # Panella store DELETE returns success/message; reason/principal are
        # audit only (already captured client-side).
        del reason, principal
        resp = self._http_delete(f"/api/memories/{drawer_id}")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        body = resp.json() or {}
        return bool(body.get("success", True))

    def aggregate_stats(
        self,
        *,
        wing_filter: str | None = None,
        tenant_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Metadata-only corpus aggregate. Returns per-wing drawer counts +
        per-room breakdown without any drawer content.

        Walks `/api/memories` paginated (per scripts/panella_backfill_associations
        .py:72-86) and tallies wing/room from each row. Cheap enough for the
        60s-cached HTTP route at low corpus sizes; option 3 (direct SQLite at
        PANELLA_KG_PATH) is the proper VPS-local accelerator and is wired via
        tools.memory_probes when this method's cost becomes prohibitive.

        ``wing_filter`` narrows the aggregation to one wing (the API still
        paginates everything; client-side filter trims after normalization).
        """
        page = 1
        page_size = 100
        per_wing: dict[str, dict[str, int]] = {}
        per_wing_latest: dict[str, str | None] = {}
        while True:
            # Fail-fast on per-page error — returning partial counts would
            # silently undercount and mislead callers acting on the result.
            # Codex PR #142 P2 hardening; clear 500 > silent partial.
            try:
                resp = self._http_get(
                    "/api/memories",
                    params={"page": page, "page_size": page_size},
                )
                resp.raise_for_status()
            except Exception as exc:
                raise RuntimeError(
                    f"aggregate_stats: pagination failed at page={page} "
                    f"({type(exc).__name__}: {exc})"
                ) from exc
            body = resp.json() or {}
            for mem in body.get("memories") or []:
                if not isinstance(mem, dict):
                    continue
                normalized = self._normalize_hit(mem)
                wing = str(normalized.get("wing") or "")
                room = str(normalized.get("room") or "")
                if not wing:
                    continue
                if wing_filter and wing != wing_filter:
                    continue
                # Tenant scoping — drop foreign-tenant rows BEFORE counting
                # (Codex PR #142 P1). Without this, multi-tenant corpora leak
                # foreign drawer counts into /v1/memory/stats responses.
                if tenant_ids and "*" not in tenant_ids and normalized.get("tenant_id") not in set(tenant_ids):
                    continue
                per_wing.setdefault(wing, {})
                per_wing[wing][room] = per_wing[wing].get(room, 0) + 1
                ts = str(normalized.get("created_at_iso") or "")
                if ts and (per_wing_latest.get(wing) is None or ts > per_wing_latest[wing]):
                    per_wing_latest[wing] = ts
            if not body.get("has_more"):
                break
            page += 1
        wing_breakdown = [
            {
                "wing": wing,
                "drawer_count": sum(rooms.values()),
                "rooms": rooms,
                "most_recent_write_ts": per_wing_latest.get(wing),
            }
            for wing, rooms in sorted(per_wing.items())
        ]
        return {
            "total_drawers": sum(w["drawer_count"] for w in wing_breakdown),
            "wing_breakdown": wing_breakdown,
            "last_synced_ts": datetime.now(UTC).isoformat(),
        }

    def find_active_hash_by_marker(self, marker_tag: str, tenant_id: str) -> str | None:
        """Stage 2 P0 — recover a durable row's upstream content_hash by a UNIQUE marker.

        The finalizer stamps ``approval_ref:{approval_id}`` (a non-reserved, per-approval-
        unique tag) on every durable approval write. When a POST returns an exact-duplicate
        with no disclosed hash (a re-drive of an already-written row, or a stale-claim
        overlap), this recovers the authoritative content_hash so the finalizer never leaves
        a durable row unmapped — which would break the RTBF divergent-id cascade. Returns
        None when no active row carries the marker (genuinely not yet written → safe to
        retry). Raises on >1 distinct match (the marker MUST be unique; a duplicate is a
        loud invariant violation, never a silent pick).
        """
        tags = [marker_tag, "status:active"]
        if tenant_id and tenant_id != "*":
            tags.append(f"tenant:{tenant_id}")
        body = {"tags": tags, "match_all": True, "n_results": 2}
        resp = self._http_post("/api/search/by-tag", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        hashes: list[str] = []
        for sr in payload.get("results") or []:
            if not isinstance(sr, dict):
                continue
            mem = sr.get("memory") or {}
            if isinstance(mem, dict) and mem.get("content_hash"):
                hashes.append(str(mem["content_hash"]))
        unique = list(dict.fromkeys(hashes))
        if len(unique) > 1:
            raise PanellaUnknownWriteOutcome(
                f"find_active_hash_by_marker: marker {marker_tag!r} matched {len(unique)} rows: {unique}"
            )
        return unique[0] if unique else None

    def find_active_hashes_by_tag(self, tag: str, tenant_id: str, *, limit: int = FIND_ACTIVE_HASHES_LIMIT) -> list[str]:
        """Return the content_hashes of ALL active rows carrying ``tag`` in ``tenant_id``.

        Like find_active_hash_by_marker but returns the full set (does NOT raise on >1).
        cc-sync source-version replace uses it to find every prior active version of a source
        file (tagged ``ccsk:<source_artifact_key>``) so they can be hard-deleted BEFORE the new
        version is written — avoiding Panella store's O(n*m) conflict-detection ``SequenceMatcher`` on
        large near-duplicate rows (which only runs against active, non-superseded candidates and
        wedges the service under ``_conn_lock``). Tenant-scoped server-side; ``"*"`` skips the tag.

        FAIL-CLOSED on overflow: if more rows match than were fetched, RAISE rather than return a
        partial set — a caller that deletes "all priors" then writes must never proceed on a
        truncated list (a missed large near-dup would re-wedge the service). With the replace flow
        a source keeps a single active version, so >limit means a legacy-orphan pathology that an
        operator must clear, not a silent partial delete.
        """
        tags = [tag, "status:active"]
        if tenant_id and tenant_id != "*":
            tags.append(f"tenant:{tenant_id}")
        body = {"tags": tags, "match_all": True, "n_results": limit}
        resp = self._http_post("/api/search/by-tag", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        raw_results = payload.get("results") or []
        total_found = payload.get("total_found")
        if (isinstance(total_found, int) and total_found > len(raw_results)) or len(raw_results) >= limit:
            raise PanellaUnknownWriteOutcome(
                f"find_active_hashes_by_tag: tag {tag!r} matched more rows "
                f"(total_found={total_found}) than the fetched {len(raw_results)} (limit={limit}); "
                "refusing to return a partial set"
            )
        hashes: list[str] = []
        for sr in raw_results:
            if not isinstance(sr, dict):
                continue
            mem = sr.get("memory") or {}
            if not isinstance(mem, dict) or not mem.get("content_hash"):
                continue
            # Client-side re-verify the row ACTUALLY carries ALL the load-bearing query tags
            # (ccsk:<key> + status:active + tenant) — defense-in-depth vs a server-side match_all
            # regression (the endpoint's own field is documented "ANY match"), mirroring
            # list_recent_by_wing's client-side re-filter. Load-bearing here because this list feeds
            # a hard-DELETE loop: never delete a row that doesn't match EVERY tag we asked for (e.g.
            # a tombstoned/superseded row that still carries ccsk must not slip through status:active).
            mem_tags = mem.get("tags") or []
            if not all(t in mem_tags for t in tags):
                continue
            hashes.append(str(mem["content_hash"]))
        return list(dict.fromkeys(hashes))

    def list_recent_by_wing(
        self,
        *,
        wing: str,
        k: int = 10,
        tenant_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        """k most-recent drawers from a wing (metadata + content). Uses
        `/api/search/by-tag` with `wing:<wing>` + `status:active` + tenant tag.

        Tenant scoping — adds `tenant:<id>` tag to the query so foreign-tenant
        rows are filtered server-side. Without this, `_filter_hits` raises
        `TenantIsolationError` on the returned rows, breaking probe samples
        (Codex PR #142 P2). Wildcard `"*"` in tenant_ids skips the tag.
        """
        tags = [f"wing:{wing}", "status:active"]
        # If exactly one concrete tenant is requested, add it as a server-side
        # tag filter. Multiple tenants would need OR semantics not supported by
        # match_all=True; in that rare case we fall back to client-side filter.
        if tenant_ids and "*" not in tenant_ids and len(list(tenant_ids)) == 1:
            tags.append(f"tenant:{list(tenant_ids)[0]}")
        body = {"tags": tags, "match_all": True, "n_results": k}
        resp = self._http_post("/api/search/by-tag", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        raw_results = payload.get("results") or []
        normalized: list[dict[str, Any]] = []
        for sr in raw_results:
            if not isinstance(sr, dict):
                continue
            mem = sr.get("memory") or {}
            if not isinstance(mem, dict) or not mem:
                continue
            hit = self._normalize_hit(mem)
            # Defense-in-depth: also filter client-side. Catches the multi-tenant
            # case above plus any server-side tag-leak edge case.
            if tenant_ids and "*" not in tenant_ids and hit.get("tenant_id") not in set(tenant_ids):
                continue
            normalized.append(hit)
        normalized.sort(key=lambda h: str(h.get("created_at_iso") or ""), reverse=True)
        return normalized[:k]

    def list_recent(
        self,
        *,
        agent: str | None = None,
        hours: int = 24,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """⚙️v4 server-side coarse + client-side authoritative epoch cutoff."""

        tags: list[str] = ["mtype:session_summary"]
        if agent:
            tags.append(f"agent:{agent}")
        body = {
            "tags": tags,
            "match_all": True,
            "time_filter": f"last {hours} hours",
        }
        resp = self._http_post("/api/search/by-tag", json=body)
        resp.raise_for_status()
        payload = resp.json() or {}
        raw_results = payload.get("results") or []

        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).timestamp()
        candidates: list[dict[str, Any]] = []
        for sr in raw_results:
            mem = (sr or {}).get("memory") or {}
            if not isinstance(mem, dict) or not mem:
                continue
            ts = mem.get("created_at")
            if isinstance(ts, (int, float)) and ts >= cutoff:
                candidates.append(mem)
                continue
            iso = mem.get("created_at_iso")
            if ts is None and isinstance(iso, str):
                try:
                    parsed = datetime.fromisoformat(iso.rstrip("Z")).replace(tzinfo=UTC)
                    if parsed.timestamp() >= cutoff:
                        candidates.append(mem)
                except ValueError:
                    continue
        candidates.sort(key=lambda m: float(m.get("created_at") or 0.0), reverse=True)
        # Phase 1 §3.3 — drop non-recall lifecycle statuses CLIENT-SIDE before [:limit]
        # (forward-looking; no production caller today, NEW-2). Deliberately NOT
        # a server-side status:active match_all tag — that would hide the ~8,605
        # legacy session_summary rows lacking the tag (P1-2). Normalize first so
        # the metadata-wins status drives the drop; then slice so an excluded
        # invalid row does not consume a limit slot.
        normalized = [self._normalize_hit(m) for m in candidates]
        valid = [
            h for h in normalized
            if not _has_excluded_recall_status(h)
        ]
        return valid[:limit]

    # ------------------------------------------------------------------
    # Hit normalization

    def _normalize_hit(self, raw: dict[str, Any]) -> dict[str, Any]:
        meta = dict(raw.get("metadata") or {})
        tags = list(raw.get("tags") or [])

        wing_tag, _ = _parse_namespaced_tag(tags, "wing")
        room_tag, _ = _parse_namespaced_tag(tags, "room")
        agent_tag, _ = _parse_namespaced_tag(tags, "agent")
        mtype_tag, _ = _parse_namespaced_tag(tags, "mtype")
        tenant_tag, _ = _parse_namespaced_tag(tags, "tenant")
        status_tag, _ = _parse_namespaced_tag(tags, "status")

        # ⚙️v3 P0-3: metadata wins over tag, but tenant defaults to t_owner_personal
        # — never None. Fallback wing/room is knowledge/legacy.
        wing = str(meta.get("wing") or wing_tag or LEGACY_FALLBACK_WING)
        room = str(meta.get("room") or room_tag or LEGACY_FALLBACK_ROOM)
        agent = meta.get("agent") or agent_tag or "unknown"
        memory_type = (
            raw.get("memory_type")
            or meta.get("memory_type")
            or mtype_tag
            or "observation"
        )
        tenant_id = str(meta.get("tenant_id") or tenant_tag or legacy_fallback_tenant())
        status = str(meta.get("status") or status_tag or "active")

        return {
            "drawer_id": raw.get("content_hash"),
            "id": raw.get("content_hash"),
            "content": raw.get("content"),
            "wing": wing,
            "room": room,
            "agent": agent,
            "memory_type": memory_type,
            "tenant_id": tenant_id,
            "status": status,
            "tags": tags,
            "metadata": meta,
            "created_at": raw.get("created_at"),
            "created_at_iso": raw.get("created_at_iso"),
            "updated_at": raw.get("updated_at"),
            "updated_at_iso": raw.get("updated_at_iso"),
        }


def cli_resolve_or_exit(cli_arg: str | None = None) -> str:
    """Convenience wrapper for scripts: print message + sys.exit on failure."""

    try:
        return resolve_panella_api_key(cli_arg)
    except PanellaAuthMissing as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
