# Panella defensive-parity eval bundle

This bundle exists to show governed reads do not cost recall — it is not a leaderboard entry.

It measures the **governed facade read path vs the raw store read path**, on the same
LongMemEval-derived corpus, on the same isolated eval box — the "governance is not worse recall"
defensive-parity instrument — plus K0 goldset scaffolding (a key-correctness scorer and a
supersede confusion-matrix goldset v0) that later construction-rung work baselines against.

Ported from a proven internal LongMemEval Stage-A harness. **All numeric output lands ONLY under
`eval/out/` (gitignored) — never in this README, never in a commit message, never on stdout.**
`make eval-public-scan` mechanically enforces that over the tracked tree.

## Quickstart (human walkthrough)

```bash
# 1. Stand up the isolated eval box (own compose project `panella-eval`, own ports 18000/18001,
#    own scratch key — NEVER your real box's .env or ports).
make eval-up

# 2. Get the (large, ~277MB) public LongMemEval dataset, sha256-verified.
make eval-dataset

# 3. Run BOTH read lanes end to end, then compare them.
make eval-retrieve

# 4. (optional, costs tokens/time) QA-accuracy scoring with an official LongMemEval rubric.
export OPENAI_API_KEY=...   # or pass --reader-transport/--judge-transport codex to skip this
make eval-qa

# 5. Render the human-readable report (fills eval/REPORT.template.md with THIS run's numbers).
make eval-report
cat eval/out/report.md

# 6. Tear the eval box down completely (compose project + its volumes; never touches a real box).
make eval-down
```

## Quickstart (terse agent runbook)

```
make eval-selftest              # ruff + pytest + goldset determinism + isolation-check (no box needed)
make eval-up                    # isolated box up, owner bearer minted into eval/out/state.env
make eval-dataset                # download + sha256-verify longmemeval_s_cleaned.json
make eval-retrieve                # both lanes, ingest_retrieve.py --lane {store,facade}, then compare_lanes.py
make eval-qa                     # optional QA-accuracy (needs OPENAI_API_KEY or --*-transport codex)
make eval-report                 # renders eval/out/report.md
make eval-down                   # teardown
make eval-smoke                  # n=2/type smoke, both lanes, writes eval/out/smoke-status.json
make eval-public-scan             # numbers gate over the TRACKED tree (git ls-files)
python eval/goldsets/synth_supersede.py --check    # K0 goldset determinism
```

## Isolation warnings

- **Never point this at a real box.** Every compose invocation this bundle makes goes through the
  `$(EVAL_COMPOSE)` Makefile variable — `docker compose -p panella-eval --env-file
  eval/out/compose.env -f docker-compose.yml -f eval/compose.eval.yml` — with NO exceptions
  (including the bearer mint step). This gives the eval box: a distinct compose project name
  (`panella-eval`, vs your real box's default project), distinct loopback ports (18000/18001 vs
  8000/8001), a scratch `--env-file` (a FRESH key every `eval-up`, never your real `.env`), and an
  overridden `/app/local` mount (an empty scratch dir, never your real `.panella/` approval
  token/overlay).
- `make eval-selftest`'s `eval-isolation-check` step is a MECHANICAL proof, not a promise: it runs
  `$(EVAL_COMPOSE) config` and asserts the project name, both host ports, the `/app/local` mount
  source, and the governance-overlay env are all eval-scoped — it fails BEFORE any container starts
  if that is ever not true.
- `make eval-down` = `$(EVAL_COMPOSE) down -v` — removes the eval project's own named volumes
  (Docker namespaces them by project, e.g. `panella-eval_panella-store`). Teardown never names a
  volume by pattern match; it can only ever reach the `panella-eval` project's own volumes.
- `panella init` is never called by this bundle. It is interactive-adjacent, assumes the DEFAULT
  compose project, and is being reworked on a parallel branch. The eval box's owner bearer is
  minted through the SAME `$(EVAL_COMPOSE)` wrapper as everything else:
  `$(EVAL_COMPOSE) exec -T panella-http panella tokens mint --principal <root> --label eval-owner-<ts>`.

## The ingest-visibility contract (read this before trusting a facade-lane number)

The facade's `serving` profile reads an allowlist of wing/room
(`panella/config_render.py::render_serving_profile`); rows without wing/room metadata normalize to
`knowledge/legacy` (`panella/panella_adapter.py`'s `LEGACY_FALLBACK_WING`/`LEGACY_FALLBACK_ROOM`),
which the shipped `serving` profile does NOT read. A naive port that ingests without the right
wing/room stamp gets **facade-lane recall = 0, silently, forever** — every hit is filtered out by
`MemoryClient._filter_hits`'s `read_allowlist` check before it ever reaches the caller.

`eval/longmemeval/visibility.py` derives the correct stamp LIVE from the box's own governance
(`panella.governance.current_governance()` — the SAME call the facade process itself resolves at
boot), so the derivation cannot silently drift from what the box actually serves.
`ingest_retrieve.py`'s facade lane runs a **visibility canary** before computing any recall number:
it ingests one marker row directly into the store, confirms the facade search can retrieve it, and
**hard-aborts the run (loudly) on failure** rather than silently reporting zeros.

## Honest framing (locked)

The two lanes are NOT the same ranking function. The facade path adds profile top-k caps,
tenant/read allowlists, lifecycle filtering, overfetch/backfill, and wing-boost
(`panella/client.py`'s `search` + `panella_adapter.py`'s overfetch). This bundle's report
therefore claims **"the operational governed read path vs the raw store path on the same
corpus"** — never "pure recall parity". `eval/longmemeval/compare_lanes.py` emits recall@k
side-by-side + the delta, AND `eval/REPORT.template.md` carries a fixed "intentional lane deltas"
table enumerating every semantic difference with its ACTUAL config value in that run. Any facade
feature that is OFF by default for the shipped self-host box (docs/SELF_HOST.md) stays OFF here —
out-of-box posture, no cherry-picking.

## Facade schema mapping (harness field -> facade field)

The facade lane's `search_facade()` (`eval/longmemeval/ingest_retrieve.py`) talks to
`POST /v1/memory/search` (`panella/http/routes/search.py` + `panella/http/schemas.py`):

| Harness sends | Facade request field (`SearchRequest`) |
| --- | --- |
| `query` (the question text) | `query: str` (required, min_length=1) |
| `k` (fetch width, `max(k, reader_k, 10)`) | `k: int \| None` (clamped server-side to the profile's `max_query_k`) |
| _(not sent)_ | `wings_hint: list[str] \| None` (harness omits it — the profile's own `read_allowlist` already scopes the query to the owner wing; an explicit hint would just soft-boost within that same wing) |

| Facade response field (`SearchResponse`) | Harness reads |
| --- | --- |
| `hits: list[dict]` | iterated directly |
| `hit["metadata"]["session_id"]` | `session_id` (recall@k join key) |
| `hit["score"]` | `score` |
| `hit["content"]` | `content` (reader context) |

Contrast with the **store** lane's `search_store()`, which talks to the pinned mcp-memory-service OpenAPI
(`tests/fixtures/panella_openapi_v10.67.1.json`, `SemanticSearchRequest`/response envelope
`{"results": [{"memory": {...}, "similarity_score": ...}]}`) — a DIFFERENT request/response
envelope entirely (`n_results` vs `k`, `results[].memory.metadata` vs `hits[].metadata`), which is
exactly why the two lane implementations in `ingest_retrieve.py` are separate functions
(`search_store` / `search_facade`) rather than one function with an `if lane==...` branch deep
inside a shared parser.

## Transport options (reader/judge, QA-accuracy + key_correctness)

Both `eval/longmemeval/qa.py` and `eval/goldsets/key_correctness_eval.py` support two LLM
transports, chosen per-call so you never need an OpenAI key at all if you have a local `codex` CLI
authenticated:

- `openai` — an OpenAI-compatible chat endpoint. Needs `OPENAI_API_KEY` (or `OPENAI_API_KEY_FILE`).
  `qa.py`'s default reader is `gpt-4o-mini`, judge `gpt-4o` (LongMemEval's own published judge).
- `codex` — a local `codex` CLI subprocess (`codex exec --sandbox read-only ...`), device-auth
  subscription, no per-call API key. Fail-closed: a persistent transport failure raises/returns an
  `__ERR__...` sentinel that is EXCLUDED from the accuracy denominator, never silently scored as a
  wrong answer.

Both are fail-closed by design (a transport outage aborts the affected row rather than masquerading
as a real wrong-answer grade) — this is inherited unchanged from the source harness's proven
design and re-verified by `eval/tests/test_key_correctness_eval.py`.

## Cost note (full 500Q QA run)

The full public LongMemEval `longmemeval_s` set is 500 questions. At `--reader-k 5` (top-5 context
budget, ~13k tokens/question observed on the source harness this was ported from) with the
`openai` transport, budget on the order of **500 reader calls + 500 judge calls** at whatever your
configured models cost per call — for `gpt-4o-mini` reader / `gpt-4o` judge this is a low-single-
digit-dollars run, not a large one, but it is NOT free, and it is NOT run by any `make eval-*`
target automatically (the smoke target uses `--n-per-type 2` against a 4-question fixture, not the
real 500Q dataset). Use `--reader-transport codex --judge-transport codex` to run the full set at
no per-call API cost if you have a local `codex` CLI authenticated.

## What `make eval-smoke` proves (and does not)

`make eval-smoke` runs `--n-per-type 2` against a tiny synthetic fixture
(`eval/tests/fixtures/smoke_dataset.json`, 4 questions) through BOTH lanes end to end, including
the facade visibility canary, and writes `eval/out/smoke-status.json` (per-stage
pass/fail/skipped). It proves the PIPELINE WIRING works — ingest, both search paths, comparison,
report rendering — against a real (if throwaway) box. It does **not** produce a meaningful recall
number (n=2/type is far too small) and no accuracy claim is drawn from it; `smoke-status.json` is
machine-readable status only, never a benchmark artifact.

## Environment reference

See `eval/longmemeval/instance.env.template` for the full `PANELLA_EVAL_*` env var list
(`make eval-up` writes `eval/out/compose.env` + `eval/out/state.env`, which every later target
sources — you should rarely need to hand-edit these).

## Reproducing a specific report

Every `eval/REPORT.template.md` render carries a run stamp (dataset name+sha256, Panella
commit/version, compose project/ports, `PANELLA_HTTP_PROFILE`, subset size). To reproduce a
specific published report, check out that commit, then run the Quickstart above with the same
`--n-per-type`/`--reader-k` flags noted in the report's run stamp.
