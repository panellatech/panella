#!/usr/bin/env python3
"""Panella defensive-parity harness — ingest + retrieval (model-free recall@k).

Ported from a proven LongMemEval Stage-A adapter. Measures the SAME corpus through TWO read
lanes on the SAME eval box:

  store  — search via the store's own mcp-memory-service API (PANELLA_EVAL_STORE_URL / PANELLA_EVAL_API_KEY).
           This is also the ONLY ingest path in both lanes: bulk ingest is the operator's direct
           store load, not a facade write (the facade's write path queues to approval — wrong shape
           for a bulk research corpus).
  facade — search via the governed facade `/v1/memory/search` (PANELLA_EVAL_FACADE_URL /
           PANELLA_EVAL_BEARER) — the path a real user actually runs.

This bundle exists to show governed reads do not cost recall — it is NOT a leaderboard entry, and it
is NOT pure recall parity: the facade path is a DIFFERENT ranking function (profile top-k cap,
tenant/read allowlists, lifecycle filtering, overfetch/backfill, wing-boost). See
eval/REPORT.template.md's "intentional lane deltas" table for the enumerated differences.

NEVER point this at a real box. Stand up the throwaway `make eval-up` box first.

Config via env (or flags):
  PANELLA_EVAL_STORE_URL   store base URL on the eval box (default http://127.0.0.1:18000)
  PANELLA_EVAL_API_KEY     the eval box's PANELLA_API_KEY (store<->facade shared secret)
  PANELLA_EVAL_FACADE_URL  facade base URL on the eval box (default http://127.0.0.1:18001)
  PANELLA_EVAL_BEARER      an owner bearer minted on the eval box (facade lane only)
  PANELLA_EVAL_DATA        path to the LongMemEval JSON dataset (or a compatible smoke fixture)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from eval._paths import assert_eval_out

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def _tok(s: str) -> int:
        return len(_enc.encode(s))

    TKNAME = "cl100k_base"
except Exception:  # pragma: no cover - tokenizer is best-effort
    def _tok(s: str) -> int:
        return max(1, len(s) // 4)

    TKNAME = "char/4-proxy"

SEP = "\n\n---\n\n"

# The visibility-canary marker content — a fixed, greppable string so a facade-lane miss is
# unambiguous ("did the marker come back?"), never a coincidental semantic near-hit.
CANARY_MARKER = "panella-eval-visibility-canary-3f9c1a"

# Isolation guard (mandatory, no escape hatch): the ONLY store/facade ports this harness may ever
# target. `eval/compose.eval.yml` publishes the eval box on these EXACT loopback ports specifically
# so a real box (8000/8001) can never collide with — or be silently targeted by — an eval run. A
# `--store-url`/`--facade-url` flag or `PANELLA_EVAL_*_URL` env override pointed at 127.0.0.1:8000
# (a REAL box's port) would otherwise let this script's bulk-ingest/bulk-delete calls mutate
# production data. See `_assert_isolated_urls` — runs before any network call, no bypass flag.
_ALLOWED_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
_EVAL_STORE_PORT = 18000
_EVAL_FACADE_PORT = 18001


def _assert_isolated_urls(store_url: str, facade_url: str) -> None:
    """Hard-fail (exit 2) unless BOTH URLs are loopback AND use the eval box's dedicated ports
    (store 18000 / facade 18001). No escape hatch — this check has no flag to disable it, and it
    must run before ANY network call this script makes (ingest/search/clear/canary all POST to
    these URLs). A store URL pointed at 127.0.0.1:8000 or a non-loopback host is a REAL box —
    this script bulk-ingests and bulk-deletes, and NEVER points at a real box (brief's mandatory
    isolation mechanics, same rule as the Makefile's $(EVAL_COMPOSE) wrapper)."""
    for label, url, expected_port in (("store", store_url, _EVAL_STORE_PORT), ("facade", facade_url, _EVAL_FACADE_PORT)):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
        if host not in _ALLOWED_LOOPBACK_HOSTS or port != expected_port:
            print(
                f"REFUSING to run: --{label}-url={url!r} is not the isolated eval box "
                f"(must be loopback host in {sorted(_ALLOWED_LOOPBACK_HOSTS)!r} on port {expected_port}). "
                "This script bulk-ingests and bulk-deletes; it must NEVER point at a real box. "
                "There is no flag to bypass this check — stand up `make eval-up`'s isolated box "
                "(store 18000 / facade 18001) and use its URLs.",
                file=sys.stderr,
            )
            raise SystemExit(2)


def _env_or_file(env_name: str, file_env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value and os.environ.get(file_env_name):
        value = Path(os.environ[file_env_name]).read_text(encoding="utf-8").strip()
    return value


def _api_key() -> str:
    key = _env_or_file("PANELLA_EVAL_API_KEY", "PANELLA_EVAL_API_KEY_FILE")
    if not key:
        sys.exit("set PANELLA_EVAL_API_KEY or PANELLA_EVAL_API_KEY_FILE (the eval box's PANELLA_API_KEY)")
    return key


def _bearer() -> str:
    token = _env_or_file("PANELLA_EVAL_BEARER", "PANELLA_EVAL_BEARER_FILE")
    if not token:
        sys.exit(
            "set PANELLA_EVAL_BEARER or PANELLA_EVAL_BEARER_FILE (an owner bearer minted via "
            "`make eval-up` -> eval/out/state.env) for the facade lane"
        )
    return token


def _store_req(base: str, key: str, path: str, payload: dict, method: str = "POST", timeout: int = 60) -> dict:
    """Raw store request. Panella store's OpenAPI advertises Bearer, but the live adapter middleware
    also accepts the legacy X-API-Key header (panella/panella_adapter.py) — send both so this stays
    portable across store versions without guessing which scheme is wired."""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": key, "Authorization": f"Bearer {key}"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _facade_req(base: str, bearer: str, path: str, payload: dict, timeout: int = 60) -> dict:
    """Facade request — POST only (search is the only lane operation the facade exposes)."""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _fmt_session(turns: list, date: Any, sid: str) -> str:
    # Prefix the unique session id so two byte-identical conversations in a haystack stay DISTINCT
    # memories — otherwise the store's exact content-hash dedup would collapse them and the measured
    # corpus would be smaller than the haystack (and an evidence session could silently disappear).
    out = [f"[Session {sid} | date: {date}]"]
    for t in turns:
        if isinstance(t, dict):
            out.append(f"{t.get('role', '?')}: {t.get('content', '')}")
    return "\n".join(out)


def count_eval_tag(base: str, key: str) -> int:
    """Live count of eval-tagged rows in the store (used for clear-between verification)."""
    return _store_req(base, key, "/api/search/by-tag", {"tags": ["panella_eval"], "match_all": False}).get(
        "total_found", 0
    )


def ingest(base: str, key: str, q: dict, *, wing: str, room: str) -> int:
    """Bulk-load one question's haystack into the store DIRECTLY (both lanes read the same corpus;
    only search differs). Stamped with the wing/room the shipped `serving` facade profile actually
    reads (see eval/longmemeval/visibility.py — derived from panella/config_render.py, not guessed).
    """
    sessions = q["haystack_sessions"]
    sids = q["haystack_session_ids"]
    dates = q.get("haystack_dates") or [None] * len(sessions)
    n = 0
    for turns, sid, date in zip(sessions, sids, dates, strict=True):
        for attempt in range(3):
            try:
                _store_req(
                    base,
                    key,
                    "/api/memories",
                    {
                        "content": _fmt_session(turns, date, sid),
                        "tags": ["panella_eval", "status:active", f"wing:{wing}", f"room:{room}"],
                        "memory_type": "observation",
                        "metadata": {
                            "wing": wing,
                            "room": room,
                            "session_id": sid,
                            "date": date,
                            "qid": q["question_id"],
                        },
                        # Unique conversation_id per session SKIPS the store's semantic dedup, so
                        # topically-similar distractor/evidence sessions are never collapsed —
                        # otherwise the measured corpus would be smaller than the haystack.
                        "conversation_id": f"{q['question_id']}::{sid}",
                    },
                )
                n += 1
                break
            except Exception as exc:  # noqa: BLE001 - sequential ingest, log terminal failures only
                if attempt == 2:
                    print(f"  store fail {sid}: {exc}", file=sys.stderr, flush=True)
                else:
                    time.sleep(0.3)
    # FAIL CLOSED on real ingest failures, but TOLERATE benign exact-duplicate sessions the store
    # legitimately dedups (a property of the dataset, not an infra failure). With the sid-prefixed
    # unique content above, collapse should be ~0; a few duplicate-session collapses are harmless.
    expected = len(sessions)
    if n != expected:
        raise RuntimeError(f"{q['question_id']}: only {n}/{expected} sessions POSTed (store failures); aborting")
    actual = count_eval_tag(base, key)
    if actual < expected * 0.5:
        raise RuntimeError(f"{q['question_id']}: catastrophic corpus loss ({actual}/{expected} rows); aborting")
    if actual < expected:
        print(
            f"  note: {q['question_id']} {expected - actual} duplicate session(s) deduped "
            f"({actual}/{expected} unique rows) — benign",
            file=sys.stderr,
            flush=True,
        )
    return actual  # real corpus size (rows) for n_sessions


def search_store(base: str, key: str, query: str, k: int) -> list[dict]:
    """Store lane: search via the store's own semantic search API directly."""
    last_exc = None
    for _attempt in range(3):
        try:
            r = _store_req(base, key, "/api/search", {"query": query, "n_results": k})
            out = []
            for res in r.get("results", []):
                mem = res.get("memory", {})
                md = mem.get("metadata", {}) or {}
                out.append(
                    {
                        "session_id": md.get("session_id"),
                        "score": res.get("similarity_score"),
                        "content": mem.get("content", ""),
                    }
                )
            return out
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.5)
    # FAIL CLOSED: a transport/API failure must NOT be returned as [] — main() would then record
    # recall@k=0 and an empty QA context, folding a transient outage into the accuracy metrics as a
    # real retrieval miss. Abort the run instead.
    raise RuntimeError(f"store search failed after retries ({last_exc}); aborting rather than scoring a transport failure as recall=0")


def search_facade(base: str, bearer: str, query: str, k: int) -> list[dict]:
    """Facade lane: search via the governed `/v1/memory/search` (SearchRequest{query,k,wings_hint} ->
    SearchResponse{hits}). Request/response mapping per panella/http/schemas.py — see
    eval/README.md's facade schema mapping table for the field-by-field correspondence."""
    last_exc = None
    for _attempt in range(3):
        try:
            r = _facade_req(base, bearer, "/v1/memory/search", {"query": query, "k": k})
            out = []
            for hit in r.get("hits", []):
                md = hit.get("metadata", {}) or {}
                out.append(
                    {
                        "session_id": md.get("session_id"),
                        "score": hit.get("score"),
                        "content": hit.get("content", ""),
                    }
                )
            return out
        except urllib.error.HTTPError as exc:
            last_exc = exc
            time.sleep(0.5)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"facade search failed after retries ({last_exc}); aborting rather than scoring a transport failure as recall=0")


def clear(base: str, key: str) -> None:
    """Delete all eval rows between questions (store-side; both lanes share the corpus). FAIL-CLOSED:
    raises if it cannot CONFIRM the store is empty — a silent clear failure would leak the previous
    haystack into the next question and invalidate the per-question isolation the measurement needs."""
    try:
        c = count_eval_tag(base, key)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"clear: cannot count panella_eval rows (isolation unverifiable): {exc}") from exc
    if c:
        try:
            _store_req(base, key, "/api/manage/bulk-delete", {"tag": "panella_eval", "confirm_count": c})
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"clear: bulk-delete failed ({exc}); next question would not be isolated") from exc
    try:
        residual = count_eval_tag(base, key)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"clear: cannot verify empty store: {exc}") from exc
    if residual:
        raise RuntimeError(f"clear: {residual} rows remain after delete; aborting to preserve isolation")


def recall_at(hits: list[dict], evidence: list[str], k: int) -> float | None:
    got = {h["session_id"] for h in hits[:k] if h["session_id"]}
    ev = set(evidence)
    return len(got & ev) / len(ev) if ev else None


def select(data: list[dict], n_per_type: int) -> list[dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for q in sorted(data, key=lambda x: x["question_id"]):
        by[q["question_type"]].append(q)
    sub = []
    for qs in by.values():
        sub += qs[:n_per_type]
    return sub


def run_visibility_canary(store_base: str, store_key: str, facade_base: str, bearer: str, *, wing: str, room: str) -> bool:
    """Ingest ONE marker row directly into the store, then confirm the facade lane can retrieve it.
    This is the make-or-break check the brief calls out: a naive port that ingests without the
    wing/room the shipped `serving` profile reads gets recall@k=0 on the facade lane FOREVER, and
    that failure mode is silent unless something asserts visibility BEFORE any recall number is
    computed. Returns True iff the marker was found; never raises (caller decides abort policy)."""
    marker_qid = "visibility-canary"
    try:
        _store_req(
            store_base,
            store_key,
            "/api/memories",
            {
                "content": CANARY_MARKER,
                "tags": ["panella_eval", "status:active", f"wing:{wing}", f"room:{room}"],
                "memory_type": "observation",
                "metadata": {"wing": wing, "room": room, "qid": marker_qid, "canary": True},
                "conversation_id": f"{marker_qid}::canary-row",
            },
        )
    except Exception as exc:  # noqa: BLE001
        print(f"visibility canary: store ingest failed: {exc}", file=sys.stderr, flush=True)
        return False
    found = False
    try:
        hits = search_facade(facade_base, bearer, CANARY_MARKER, 5)
        found = any(CANARY_MARKER in (h.get("content") or "") for h in hits)
    except Exception as exc:  # noqa: BLE001
        print(f"visibility canary: facade search failed: {exc}", file=sys.stderr, flush=True)
        found = False
    finally:
        try:
            clear(store_base, store_key)
        except Exception as exc:  # noqa: BLE001
            print(f"visibility canary: cleanup failed (non-fatal): {exc}", file=sys.stderr, flush=True)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lane", choices=("store", "facade"), required=True, help="which read path to measure")
    ap.add_argument("--n-per-type", type=int, default=8)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--reader-k", type=int, default=10, help="how many top sessions to dump as reader context")
    ap.add_argument("--store-url", default=os.environ.get("PANELLA_EVAL_STORE_URL", "http://127.0.0.1:18000"))
    ap.add_argument("--facade-url", default=os.environ.get("PANELLA_EVAL_FACADE_URL", "http://127.0.0.1:18001"))
    ap.add_argument(
        "--wing",
        default=os.environ.get("PANELLA_EVAL_WING"),
        help="wing stamp override; default = derived LIVE from config_render.py (see visibility.py) — do not hardcode unless testing a drift",
    )
    ap.add_argument("--room", default=os.environ.get("PANELLA_EVAL_ROOM"), help="room stamp override; default = derived from visibility.py")
    ap.add_argument("--data", default=os.environ.get("PANELLA_EVAL_DATA", "longmemeval_s_cleaned.json"))
    ap.add_argument("--out", default="eval/out/stage_a_retrieval.json")
    ap.add_argument(
        "--canary-only",
        action="store_true",
        help="run ONLY the facade visibility canary (ingest one marker, confirm facade retrieves it, clean up) then exit — no dataset/--data needed. Used by `make eval-visibility-canary`.",
    )
    a = ap.parse_args(argv)
    if a.canary_only and a.lane != "facade":
        sys.exit("--canary-only requires --lane facade (the canary only exercises the facade read path)")

    # Isolation guard — MUST run before any network call (before even reading credentials, since
    # the point is to refuse before this script can act at all). No flag disables this.
    _assert_isolated_urls(a.store_url, a.facade_url)
    # Metric-output guard: --out (and its .jsonl sidecar) must land under eval/out/ — resolve to an
    # absolute path now so every later use (jsonl sidecar, final JSON, print) is unambiguously
    # inside the gitignored dir regardless of CWD.
    a.out = str(assert_eval_out(a.out))

    store_key = _api_key()
    bearer = _bearer() if a.lane == "facade" else ""

    # Derive the wing/room stamp LIVE from the box's actual governance (visibility.py reads the
    # SAME panella.governance.current_governance() the facade process resolves at boot) unless the
    # caller explicitly overrode it — never a hardcoded guess (brief's make-or-break constraint).
    if a.wing is None or a.room is None:
        from eval.longmemeval.visibility import eval_wing_room

        derived_wing, derived_room = eval_wing_room()
        a.wing = a.wing or derived_wing
        a.room = a.room or derived_room
    print(f"ingest stamp: wing={a.wing!r} room={a.room!r} (derived from config_render.py unless overridden)", flush=True)

    if a.lane == "facade":
        from eval.longmemeval.visibility import assert_serving_profile_reads

        assert_serving_profile_reads(a.wing, a.room)
        print("running facade visibility canary...", flush=True)
        ok = run_visibility_canary(a.store_url, store_key, a.facade_url, bearer, wing=a.wing, room=a.room)
        if not ok:
            print(
                "VISIBILITY CANARY FAILED: a marker row ingested directly into the store was NOT "
                "retrievable via the facade search. Every facade recall number below would be a "
                "silent 0 — aborting rather than reporting a meaningless run. Check: (1) the wing/room "
                "stamp matches the shipped `serving` profile's read_allowlist (see visibility.py), "
                "(2) the bearer is a valid owner token, (3) the facade box is actually serving.",
                file=sys.stderr,
                flush=True,
            )
            return 3
        print("visibility canary: PASS (marker row retrieved via facade)", flush=True)

    if a.canary_only:
        # `make eval-visibility-canary` — the canary itself already ran above; nothing left to do.
        return 0

    data = json.loads(Path(a.data).read_text(encoding="utf-8"))
    sub = select(data, a.n_per_type)
    print(f"tokenizer={TKNAME} subset={len(sub)} k={a.k} lane={a.lane}", flush=True)
    clear(a.store_url, store_key)  # ensure empty start
    results = []
    t0 = time.time()
    with open(a.out + "l", "w", encoding="utf-8") as jsonl:
        for i, q in enumerate(sub):
            n = ingest(a.store_url, store_key, q, wing=a.wing, room=a.room)
            fetch_k = max(a.k, a.reader_k, 10)
            if a.lane == "store":
                hits = search_store(a.store_url, store_key, q["question"], fetch_k)
            else:
                hits = search_facade(a.facade_url, bearer, q["question"], fetch_k)
            topk = hits[: a.reader_k]
            ctx = SEP.join(h["content"] for h in topk)
            rec = {
                "qid": q["question_id"],
                "type": q["question_type"],
                "question": q["question"],
                "gold": q["answer"],
                "question_date": q.get("question_date"),
                "evidence": q["answer_session_ids"],
                "n_sessions": n,
                "lane": a.lane,
                "recall@1": recall_at(hits, q["answer_session_ids"], 1),
                "recall@5": recall_at(hits, q["answer_session_ids"], 5),
                "recall@10": recall_at(hits, q["answer_session_ids"], 10),
                "retrieved": [{"sid": h["session_id"], "score": round(h["score"], 3) if h["score"] else None} for h in hits],
                "reader_context": ctx,
                "context_tokens": _tok(ctx),
            }
            results.append(rec)
            jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jsonl.flush()
            clear(a.store_url, store_key)
            print(f"  {i + 1}/{len(sub)} {q['question_type'][:20]:20} lane={a.lane} ({time.time() - t0:.0f}s)", flush=True)
    Path(a.out).write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    by: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by[r["type"]].append(r)
    # HARD CONSTRAINT: no metric values on stdout. Print progress/status only — every numeric
    # aggregate goes to eval/out/*.json, never a print() line.
    print(f"DONE lane={a.lane} wrote {a.out} in {time.time() - t0:.0f}s (per-question metrics in {a.out})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
