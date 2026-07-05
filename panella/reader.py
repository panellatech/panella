"""Deterministic reader++ read pipeline + optional cross-encoder stage (Slice-S S-read).

The production lift of the eval arm-B reader
(`panella/eval/longmemeval/reader_residual_eval.readerpp_select`, :184): overfetched,
ABAC-filtered hits go through slot-proxy clustering -> within-cluster recency supersede ->
RRF fuse of retrieval rank + recency rank -> live-over-superseded lexicographic order ->
optional stratified cross-encoder re-score -> trim. Rows in, rows out.

Seam contract (anti-cheat BY CONSTRUCTION, mirrors the eval's structural guarantee):
``ReaderFn = Callable[[list[dict], int], list[dict]]`` — this module never receives a
lifecycle, gold sid, store handle, or principal. The eval's ``:387`` ``(lc, hits) -> dict``
arm wrapper exists only to SCORE arms against gold and must never be wired here; a
signature-lock test pins ``readerpp_select`` to the ``:184`` shape.

Production adaptations from the eval arm (the ONLY deltas — everything else is an exact
lift, incl. the tokenizer/stoplist, so the measured algorithm is what ships):

1. Recency keys on the row's ``metadata.source_last_timestamp`` (the writer's true
   session/event chronology, when present) falling back to ``created_at``/
   ``created_at_iso`` (store INGESTION time), normalized to a single float epoch via
   ``_row_epoch``; undated == oldest — instead of the eval fixture's
   ``[Session … | date: …]`` content header (production rows carry no such header).
2. ``content`` may be None/non-str on degenerate rows -> coerced to ``""`` (never crash
   the read path).

Governance placement: this module runs strictly AFTER ``MemoryClient._filter_hits`` (tenant
+ deny/read_allowlist ABAC) — it only ever sees rows the caller is authorized to read, and
the injected scorer only ever sees ``(query, [content])``. Everything here is dormant by
default: ``PANELLA_READER=off`` unless a box opts in (enable-gate: the holistic-review
LongMemEval re-run — see migration-log/briefs/panella-s-read-plan-2026-07-03.md §2.3).

The cross-encoder dependency (``sentence-transformers``, optional extra ``[reranker]``) is
imported lazily inside ``_load_cross_encoder`` only — module import stays stdlib-only so
the base P3a package needs no torch and no model download (AST-asserted by test).
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# ── eval-locked constants (exact lift from reader_residual_eval.py — do not retune here;
#    τ/rrf_k were pre-registered in the eval so the residual could not be tuned toward a
#    desired result; production overrides are kwargs, informational, no re-calibration gate) ──

READERPP_TAU = 0.3
READERPP_RRF_K = 60

# Ratified staging: cross-encoder sits between ANN overfetch (top-50) and trim (top-k).
DEFAULT_OVERFETCH = 50
# Hard ceiling on the operator-tunable overfetch. Two independent bounds meet here:
# (a) the adapter forwards this value straight to Panella store /api/search `n_results`, whose
#     upstream schema caps it at 100 (tests/fixtures/panella_openapi_v10.31.2.json,
#     SemanticSearchRequest.n_results maximum) — anything above 100 would 422 every
#     enabled search (GH-bot r3 P2);
# (b) _cluster is O(n^2) Jaccard over the fetched pool (code-reviewer P3#1) — 100 ->
#     ~5k comparisons, trivially cheap.
MAX_OVERFETCH = 100
DEFAULT_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

ENV_READER = "PANELLA_READER"  # "off" (default) | "readerpp"
ENV_RERANKER = "PANELLA_RERANKER"  # "off" (default) | "cross-encoder"
ENV_RERANKER_MODEL = "PANELLA_RERANKER_MODEL"
ENV_OVERFETCH = "PANELLA_READER_OVERFETCH"

# The production seam — matches eval readerpp_select:184 positionally (hits, k) -> rows.
ReaderFn = Callable[[list[dict], int], list[dict]]
# Scorer contract: (query, contents) -> one float per content, higher = more relevant,
# deterministic for identical inputs, pairs scored in the given order.
ScorerFn = Callable[[str, list[str]], list[float]]

# Exact copy of the eval stoplist (reader_residual_eval.py:62-66) — byte-identical source
# form on purpose (the .split() spelling matches the eval so a diff between the two is empty).
_STOP = frozenset(
    "the a an and or but if then so of to in on at for with from by as is am are was were be been "  # noqa: SIM905
    "being i me my mine you your we our it its this that these those now currently use used using "
    "have has had do did does just over again new my user".split()
)
_SESSION_HDR = re.compile(r"^\[Session [^\]]*\]")


# ── configuration (env-driven; unknown values fail toward OFF = today's behavior) ──


def _env_value(name: str, default: str = "off") -> str:
    return (os.environ.get(name) or default).strip().lower()


def reader_enabled() -> bool:
    """True iff this process opted into the reader++ pipeline. Unknown values -> off + warn
    (fail toward current behavior, never toward an unratified ranking change)."""
    raw = _env_value(ENV_READER)
    if raw in ("", "off"):
        return False
    if raw == "readerpp":
        return True
    logger.warning("unknown %s=%r; reader stays off", ENV_READER, raw)
    return False


def _overfetch_n() -> int:
    raw = (os.environ.get(ENV_OVERFETCH) or "").strip()
    if not raw:
        return DEFAULT_OVERFETCH
    try:
        n = int(raw)
        if n < 1:
            raise ValueError(n)
    except ValueError:
        logger.warning("malformed %s=%r; using default %d", ENV_OVERFETCH, raw, DEFAULT_OVERFETCH)
        return DEFAULT_OVERFETCH
    if n > MAX_OVERFETCH:
        logger.warning("%s=%d exceeds the O(n^2) clustering ceiling; clamped to %d",
                       ENV_OVERFETCH, n, MAX_OVERFETCH)
        return MAX_OVERFETCH
    return n


def fetch_k(limit: int, *, enabled: bool) -> int:
    """Internal adapter fetch width. ``enabled`` is passed in (snapshotted ONCE per search by
    the caller — plan r2 P3#4) so one search never straddles an env flip. Off -> exactly
    ``limit`` = byte-parity with the pre-S-read pipeline."""
    if not enabled:
        return limit
    return max(limit, _overfetch_n())


# ── deterministic core (exact lift; pure, no clock, no network, no store access) ──


def _content_body(content: str) -> str:
    """Strip the eval's ``[Session sid | date: …]`` header if present (identity on production
    content — kept so clustering is byte-faithful to the measured eval arm)."""
    return _SESSION_HDR.sub("", content, count=1).strip()


def _content_tokens(content: str) -> frozenset[str]:
    toks = re.findall(r"[a-z0-9]+", _content_body(content).lower())
    return frozenset(t for t in toks if len(t) > 2 and t not in _STOP)


def jaccard_sim(content_a: str, content_b: str) -> float:
    """Cheap lexical slot-proxy: stopword-filtered content-token Jaccard (eval default
    ``similarity_fn``). Pure + deterministic."""
    ta, tb = _content_tokens(content_a), _content_tokens(content_b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _hit_content(hit: dict) -> str:
    # Production hardening: content can be None/non-str on degenerate rows; the ranking
    # pipeline must degrade (empty tokens -> no cluster) rather than crash the read path.
    raw = hit.get("content")
    return raw if isinstance(raw, str) else ""


def _coerce_epoch(value: object) -> float | None:
    """One epoch parser for every recency source: numeric (int/float/numeric string) or ISO
    string (``Z`` normalized; naive assumed UTC). Unparseable, non-finite (``nan``/``inf``
    poison sort keys — Codex diff-r1 P2), or float-overflowing int (GH-bot r1 P2) -> None.
    Never raises."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            epoch = float(value)
        except OverflowError:  # arbitrary-precision int too large for float
            return None
        return epoch if math.isfinite(epoch) else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            epoch = float(text)
        except ValueError:
            pass
        else:
            return epoch if math.isfinite(epoch) else None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _row_epoch(hit: dict) -> float | None:
    """Normalize row recency to ONE comparable type (float epoch seconds) or None (undated).

    Precedence: the WRITER's source-event timestamp ``metadata.source_last_timestamp``
    (session-sync rows carry the true session chronology there; the store ``created_at``
    is INGESTION time, so a backfill/retry that ingests an older session later would
    otherwise make old content look newest and invert the cluster supersede — GH-bot r2
    P2) -> ``created_at`` (numeric or ISO-shaped) -> ``created_at_iso``. A malformed
    source timestamp falls back to the store timestamps rather than undating the row.
    Anything wholly unparseable -> None == undated == sorts oldest (eval semantics for a
    missing date). Never raises."""
    meta = hit.get("metadata")
    if isinstance(meta, dict):
        source_epoch = _coerce_epoch(meta.get("source_last_timestamp"))
        if source_epoch is not None:
            return source_epoch
    created_epoch = _coerce_epoch(hit.get("created_at"))
    if created_epoch is not None:
        return created_epoch
    return _coerce_epoch(hit.get("created_at_iso"))


def _cluster(hits: list[dict], similarity_fn: Callable[[str, str], float], tau: float) -> list[list[int]]:
    """Single-linkage cluster of hit indices: i,j join iff similarity_fn >= tau. Passes the
    REDACTED body (header stripped) to similarity_fn, never raw content — exact lift of the
    eval's anti-shortcut guard (a future injected similarity fn must not see date/sid-shaped
    prefixes; recency is a separate signal)."""
    n = len(hits)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bodies = [_content_body(_hit_content(h)) for h in hits]
    for i in range(n):
        for j in range(i + 1, n):
            if similarity_fn(bodies[i], bodies[j]) >= tau:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _readerpp_order(
    hits: list[dict],
    *,
    similarity_fn: Callable[[str, str], float],
    tau: float,
    rrf_k: int,
) -> tuple[list[int], set[int]]:
    """The reader++ total order (all indices, untrimmed) + the demoted (within-cluster
    superseded) index set. Single implementation shared by ``readerpp_select`` and
    ``select`` so the seam function and the full pipeline can never diverge."""
    n = len(hits)
    epochs = [_row_epoch(h) for h in hits]
    # Recency rank: newest dated first; undated sort last (treated as oldest). Stable on
    # (dated, epoch, -i) — single-type keys, mixed float/ISO rows can never TypeError.
    by_recency = sorted(
        range(n), key=lambda i: (epochs[i] is not None, epochs[i] or 0.0, -i), reverse=True
    )
    recency_rank = {idx: r for r, idx in enumerate(by_recency)}

    # Within-cluster supersede: in each cluster the newest DATED row is live; older dated
    # rows are demoted BELOW every live row. Uses ONLY (cluster proxy + date) — the store's
    # own status:superseded rows were already excluded upstream (adapter validity drop +
    # client validity net); this demotes rows the store does NOT yet know are stale.
    superseded: set[int] = set()
    for cl in _cluster(hits, similarity_fn, tau):
        dated = [(i, epochs[i]) for i in cl if epochs[i] is not None]
        if len(dated) >= 2:
            newest = max(dated, key=lambda t: (t[1], -t[0]))[0]
            superseded.update(i for i, _ in dated if i != newest)

    def rrf(i: int) -> float:
        return 1.0 / (rrf_k + i + 1) + 1.0 / (rrf_k + recency_rank[i] + 1)

    # Lexicographic: every LIVE row outranks every demoted row; ties broken by RRF then
    # retrieval order. Strict contradiction-aware demotion — the canonical-row-wins
    # invariant the whole pipeline (incl. the scorer stage) must preserve.
    order = sorted(range(n), key=lambda i: (i not in superseded, rrf(i), -i), reverse=True)
    return order, superseded


def readerpp_select(
    hits: list[dict],
    reader_k: int,
    *,
    similarity_fn: Callable[[str, str], float] = jaccard_sim,
    tau: float = READERPP_TAU,
    rrf_k: int = READERPP_RRF_K,
) -> list[dict]:
    """The ``ReaderFn`` seam — positionally identical to the eval arm at
    reader_residual_eval.py:184: takes ONLY ``(hits, reader_k)``, returns selected rows.
    Structurally incapable of receiving a lifecycle/gold handle (anti-cheat)."""
    if not hits:
        return []
    order, _ = _readerpp_order(hits, similarity_fn=similarity_fn, tau=tau, rrf_k=rrf_k)
    return [hits[i] for i in order[:reader_k]]


def select(
    hits: list[dict],
    k: int,
    *,
    query: str,
    scorer: ScorerFn | None = None,
    similarity_fn: Callable[[str, str], float] = jaccard_sim,
    tau: float = READERPP_TAU,
    rrf_k: int = READERPP_RRF_K,
) -> list[dict]:
    """Full S-read pipeline: reader++ order, then the optional STRATIFIED cross-encoder
    stage, then trim.

    ``scorer=None`` -> identical to ``readerpp_select``. With a scorer: one scorer call on
    every candidate's content (fixed reader++ order), then re-order WITHIN the live stratum
    and WITHIN the demoted stratum by ``(score desc, reader++ rank asc)``, concatenate
    live -> demoted, trim to ``k``. Stratification is what makes canonical-row-wins hold
    END-TO-END: no model score can lift a demoted (within-cluster stale) row above any live
    row (see plan §2.1 for why this is the only reading consistent with the parent spec's
    own test list). Scores are never written onto hit dicts — order IS the output.

    A scorer that breaks contract (wrong length / non-numeric) degrades LOUDLY to the
    deterministic reader++ order — the read path never 500s on a rerank-stage defect."""
    if not hits:
        return []
    order, superseded = _readerpp_order(hits, similarity_fn=similarity_fn, tau=tau, rrf_k=rrf_k)
    if scorer is None:
        return [hits[i] for i in order[:k]]

    contents = [_hit_content(hits[i]) for i in order]
    try:
        scores_raw = scorer(query, contents)
    except Exception:
        logger.exception("reranker scorer raised; degrading to deterministic reader++ order")
        return [hits[i] for i in order[:k]]
    scores: list[float] | None = None
    if isinstance(scores_raw, (list, tuple)) and len(scores_raw) == len(contents):
        try:
            scores = [float(s) for s in scores_raw]
        except (TypeError, ValueError, OverflowError):  # OverflowError: huge-int score (same class as GH-bot P2)
            scores = None
        # NaN comparisons are all-False -> a NaN key silently freezes rows in place instead
        # of sorting (Codex diff-r1 P2); non-finite output = broken scorer -> degrade loud.
        if scores is not None and not all(math.isfinite(s) for s in scores):
            scores = None
    if scores is None:
        logger.error(
            "reranker scorer broke contract (expected %d floats, got %r); degrading to "
            "deterministic reader++ order",
            len(contents),
            type(scores_raw).__name__,
        )
        return [hits[i] for i in order[:k]]

    positions = range(len(order))
    live = [p for p in positions if order[p] not in superseded]
    demoted = [p for p in positions if order[p] in superseded]
    live.sort(key=lambda p: (-scores[p], p))
    demoted.sort(key=lambda p: (-scores[p], p))
    return [hits[order[p]] for p in (live + demoted)][:k]


# ── optional cross-encoder scorer (lazy; optional extra ``[reranker]``; sticky failure) ──

_scorer_lock = threading.Lock()
_scorer_cache: dict[str, ScorerFn] = {}
_scorer_failed: set[str] = set()


def _load_cross_encoder(model_name: str) -> ScorerFn:
    """Import + load the cross-encoder (separate function so tests can monkeypatch the
    load to fail/fake without touching sys.modules). Raises on any failure."""
    from sentence_transformers import CrossEncoder  # lazy: optional extra [reranker]

    model = CrossEncoder(model_name, device="cpu")

    def scorer(query: str, contents: list[str]) -> list[float]:
        if not contents:
            return []
        # Pairs scored in input order, CPU, one call — deterministic stage contract is
        # enforced by the pipeline's fixed candidate order + tie-breaks; real-model float
        # stability across machines is NOT a contract (plan §3.4).
        return [float(s) for s in model.predict([(query, c) for c in contents])]

    return scorer


def _reranker_mode() -> str:
    raw = _env_value(ENV_RERANKER)
    if raw in ("", "off"):
        return "off"
    if raw == "cross-encoder":
        return "cross-encoder"
    logger.warning("unknown %s=%r; reranker stays off", ENV_RERANKER, raw)
    return "off"


def resolve_scorer() -> tuple[ScorerFn | None, str]:
    """Resolve the configured scorer. Returns ``(scorer, state)`` with state in
    ``{"off", "ok", "unavailable"}`` so the caller can distinguish disabled from broken
    (and own the metrics for the latter). Failure is sticky per (process, model): an
    enabled-but-unavailable reranker logs ONE error and degrades every search loudly via
    the caller's counter, never a 500."""
    if _reranker_mode() == "off":
        return None, "off"
    model_name = (os.environ.get(ENV_RERANKER_MODEL) or "").strip() or DEFAULT_CROSS_ENCODER_MODEL
    with _scorer_lock:
        if model_name in _scorer_failed:
            return None, "unavailable"
        scorer = _scorer_cache.get(model_name)
        if scorer is None:
            try:
                scorer = _load_cross_encoder(model_name)
            except Exception as exc:
                _scorer_failed.add(model_name)
                logger.error(
                    "cross-encoder reranker unavailable (model=%s): %s — searches degrade to "
                    "the deterministic reader++ order until restart",
                    model_name,
                    exc,
                )
                return None, "unavailable"
            _scorer_cache[model_name] = scorer
    return scorer, "ok"
