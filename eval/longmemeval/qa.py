#!/usr/bin/env python3
"""Panella defensive-parity harness — QA-accuracy scoring (Phase 2).

Reads the retrieval dump from ingest_retrieve.py (either lane), slices the stored context to a
top-N budget, generates an answer with the reader model, and scores it with the judge model using
LongMemEval's OFFICIAL per-type rubrics (https://github.com/xiaowu0162/LongMemEval).

Defaults match the published benchmarks for the fairest comparison:
  reader = gpt-4o-mini  (a commonly cited reader baseline)
  judge  = gpt-4o       (LongMemEval's own judge)
Verdict parsing matches upstream: correct iff 'yes' in the judge response (case-insensitive).

Config: OPENAI_API_KEY (or OPENAI_API_KEY_FILE) for the reader+judge calls, OR --reader-transport
codex / --judge-transport codex to use a local `codex` CLI subprocess instead (no OpenAI key
needed for that transport — see eval/README.md's transport-options section).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eval._paths import assert_eval_out

try:
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def _tok(s: str) -> int:
        return len(_enc.encode(s))
except Exception:  # pragma: no cover
    def _tok(s: str) -> int:
        return max(1, len(s) // 4)

SEP = "\n\n---\n\n"
READER_SYS = (
    "You are a helpful assistant answering a question using ONLY the user's retrieved past "
    "conversation history below. Be concise. If the history lacks the answer, say you do not know."
)


def _openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key and os.environ.get("OPENAI_API_KEY_FILE"):
        key = Path(os.environ["OPENAI_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
    if not key:
        sys.exit("set OPENAI_API_KEY or OPENAI_API_KEY_FILE")
    return key


def _is_reasoning_model(model: str) -> bool:
    """gpt-5.x / o-series are REASONING models: they use `max_completion_tokens` (reasoning tokens
    count toward it) and reject temperature != default. gpt-4o / gpt-4o-mini use `max_tokens` +
    temperature."""
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _codex_chat(system: str, user: str, model: str = "", timeout: int = 300, retries: int = 3) -> str:
    """Reader/judge transport via a local `codex` CLI subprocess (device-auth SUBSCRIPTION — NO
    OpenAI API key, no per-call cost if you have one configured). Reads the reply from
    `codex exec --output-last-message` (stdout has banner noise). FAIL-CLOSED -> __ERR__ on
    persistent failure (a transport failure must NOT be scored as a wrong answer). Use this to keep
    the reader/judge off OpenAI entirely; the OpenAI key path (if used at all) stays
    embeddings-only in the harness's own retrieval calls (it makes none — retrieval already
    happened by the time this script runs)."""
    import subprocess
    import tempfile
    from pathlib import Path

    prompt = f"{system}\n\n---\n\n{user}"
    # --sandbox read-only: a Q&A reasoning call must NEVER mutate the eval CWD (codex exec can
    # default to workspace-write -> auto-commit/push runaway). model='' or 'codex' -> use the
    # device-authed default; an explicit slug only when the caller really wants to pin one.
    argv = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--output-last-message", "", "-"]
    if model and model != "codex":
        argv[2:2] = ["--model", model]
    last = ""
    for attempt in range(retries):
        out_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="panella-eval-qa-codex-", suffix=".txt", delete=False) as tmp:
                out_path = tmp.name
            argv[-2] = out_path
            proc = subprocess.run(argv, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, check=False)
            text = Path(out_path).read_text(encoding="utf-8") if Path(out_path).exists() else ""
            if proc.returncode == 0 and text.strip():
                return text.strip()
            last = f"exit={proc.returncode} empty={not text.strip()}"
        except subprocess.TimeoutExpired:
            last = "timeout"
        except Exception as exc:  # noqa: BLE001 — fail-CLOSED: a missing `codex` binary, a tmp/OS/
            last = type(exc).__name__  # decode error, etc. must return __ERR__, NEVER raise into the pool.
        finally:
            if out_path:
                Path(out_path).unlink(missing_ok=True)
        if attempt < retries - 1:
            time.sleep(2.0 * (attempt + 1))
    return f"__ERR__codex({last})"


def chat(key: str, model: str, system: str, user: str, max_tokens: int = 256, transport: str = "openai") -> str:
    if transport == "codex":
        codex_model = model if (model == "codex" or _is_reasoning_model(model)) else ""
        return _codex_chat(system, user, model=codex_model)
    payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if _is_reasoning_model(model):
        payload["max_completion_tokens"] = max(max_tokens * 6, 4000)
        timeout = 240
    else:
        payload["temperature"] = 0
        payload["max_tokens"] = max_tokens
        timeout = 90
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)["choices"][0]["message"]["content"].strip()
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503):
                ra = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(2**attempt, 30)
                time.sleep(delay)
                continue
            return f"__ERR__{exc.code}"
        except Exception:  # noqa: BLE001
            time.sleep(min(2**attempt, 30))
            continue
    return "__ERR__retry"


def rubric(qtype: str) -> str:
    """LongMemEval official per-type judge rubric."""
    base = (
        "I will give you a question, a correct answer, and a response from a model. Please answer yes if "
        "the response contains the correct answer. Otherwise answer no. If the response is equivalent to the "
        "correct answer or contains all the intermediate steps, also answer yes. If it only contains a subset "
        "of the required information, answer no."
    )
    if qtype == "temporal-reasoning":
        base += " Do not penalize off-by-one errors for the number of days/weeks/months."
    if qtype == "knowledge-update":
        base += (
            " If the response contains previous information along with an updated answer, it is correct as long "
            "as the updated answer is the required answer."
        )
    if qtype == "single-session-preference":
        base = (
            "I will give you a question, a rubric for the desired personalized response, and a model response. "
            "Answer yes if the response satisfies the rubric (it need not reflect all points; correct as long as "
            "it recalls and utilizes the user's personal info correctly). Otherwise answer no."
        )
    return base


def build_reader(rec: dict, topn: int) -> tuple[str, str, int]:
    qdate = rec.get("question_date")
    chunks = rec["reader_context"].split(SEP)[:topn]
    ctx = SEP.join(chunks)
    date_line = f"Current date: {qdate}\n\n" if qdate else ""
    user = f"{date_line}User history:\n{ctx}\n\nQuestion: {rec['question']}\nAnswer:"
    return READER_SYS, user, _tok(ctx)


def _judge(key: str, rec: dict, ans: str, judge_model: str, transport: str = "openai") -> tuple[str, bool, bool]:
    jp = (
        f"Question: {rec['question']}\nCorrect Answer: {rec['gold']}\nModel Response: {ans}\n\n"
        "Is the model response correct? Answer yes or no."
    )
    verdict = chat(key, judge_model, rubric(rec["type"]), jp, max_tokens=5, transport=transport)
    errored = verdict.startswith("__ERR__")
    return verdict, ((not errored) and ("yes" in verdict.lower())), errored


def grade(
    key: str,
    rec: dict,
    topn: int,
    reader_model: str,
    judge_model: str,
    reader_max_tokens: int = 256,
    reader_transport: str = "openai",
    judge_transport: str = "openai",
) -> dict:
    base = {"qid": rec["qid"], "type": rec["type"], "lane": rec.get("lane"), "recall@5": rec.get("recall@5")}
    sys_prompt, user, ctx_tokens = build_reader(rec, topn)
    base["qa_context_tokens"] = ctx_tokens
    ans = chat(key, reader_model, sys_prompt, user, max_tokens=reader_max_tokens, transport=reader_transport)
    if ans.startswith("__ERR__"):
        # Reader failure — do NOT judge or score it. Marked errored so main() EXCLUDES it from the
        # accuracy denominator (an infra failure must not count as a wrong answer).
        return {**base, "answer": ans, "verdict": "__SKIP__", "correct": False, "errored": True}
    verdict, correct, errored = _judge(key, rec, ans, judge_model, transport=judge_transport)
    return {**base, "answer": ans[:300], "verdict": verdict, "correct": correct, "errored": errored}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--retr", default="eval/out/stage_a_retrieval.json")
    ap.add_argument("--reader-k", type=int, default=5, help="top-N sessions to feed the reader (budget control)")
    ap.add_argument("--reader-model", default="gpt-4o-mini")
    ap.add_argument("--judge-model", default="gpt-4o")
    ap.add_argument(
        "--reader-transport",
        choices=("openai", "codex"),
        default="openai",
        help="reader LLM transport: openai = OpenAI chat API; codex = local `codex` CLI subprocess (no OpenAI key)",
    )
    ap.add_argument(
        "--judge-transport",
        choices=("openai", "codex"),
        default="openai",
        help="judge LLM transport (default openai gpt-4o = the official LongMemEval judge)",
    )
    ap.add_argument("--reader-max-tokens", type=int, default=256)
    ap.add_argument(
        "--no-context",
        action="store_true",
        help="ABLATION (contamination probe): STRIP the retrieved haystack — feed the reader the question only",
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default="eval/out/stage_a_qa.json")
    a = ap.parse_args(argv)
    a.out = str(assert_eval_out(a.out))
    key = _openai_key() if "openai" in (a.reader_transport, a.judge_transport) else ""
    data = json.loads(Path(a.retr).read_text(encoding="utf-8"))
    if a.no_context:
        for r in data:
            r["reader_context"] = ""
        print(
            "[ABLATION] --no-context: retrieved haystack STRIPPED (closed-book contamination probe)",
            flush=True,
        )
    print(
        f"grading {len(data)} q (reader={a.reader_model}@{a.reader_transport} "
        f"judge={a.judge_model}@{a.judge_transport} budget=top{a.reader_k} workers={a.workers})",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        graded = list(
            ex.map(
                lambda r: grade(
                    key, r, a.reader_k, a.reader_model, a.judge_model, a.reader_max_tokens,
                    a.reader_transport, a.judge_transport,
                ),
                data,
            )
        )
    # EXCLUDE errored rows (reader/judge API failures) from accuracy — an infra failure must
    # not count as a wrong answer (it would deflate the reported accuracy).
    errs = [g for g in graded if g.get("errored")]
    scored = [g for g in graded if not g.get("errored")]

    # FAIL CLOSED (envelope shape, not a bare list): ANY transport error makes this run's accuracy
    # number unreliable — a partial QA run silently reads as a real (if slightly smaller-n)
    # benchmark unless something marks it incomplete. "complete" is false and "errors" carries the
    # count whenever errs is non-empty, regardless of how many rows still scored successfully
    # (the OLD behavior only failed when ALL rows errored — a single flaky transport call used to
    # exit 0 with a quietly-deflated-n accuracy number). render_report.py refuses to render an
    # accuracy row from an incomplete file (see its qa_rows handling).
    complete = len(errs) == 0
    envelope = {"complete": complete, "errors": len(errs), "rows": graded}
    Path(a.out).write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")

    # HARD CONSTRAINT: no metric values on stdout — per-type/overall breakdowns go ONLY to --out.
    print(f"graded {len(scored)}/{len(graded)} rows (excluded {len(errs)} transport errors); per-type breakdown in {a.out}", flush=True)
    if not scored:
        # Every reader/judge call errored (bad key / model / quota outage) — the most severe case
        # of incompleteness. Do NOT emit a successful-looking "0/0" benchmark artifact framing;
        # the FATAL message below is a stronger signal than the generic incomplete-run case.
        print(
            f"FATAL: 0/{len(graded)} rows scored — all reader/judge calls errored "
            f"(check OPENAI_API_KEY / model / quota, or --reader-transport/--judge-transport codex). "
            "Refusing to emit a benchmark number.",
            file=sys.stderr,
            flush=True,
        )
        # Same exit-code contract as the partial case below: ANY transport error -> 4. The all-
        # error case keeps its louder FATAL wording but must not carve out a different code.
        return 4
    if not complete:
        # FAIL CLOSED: at least one row transport-errored. The written envelope already records
        # "complete": false + "errors": N — exit nonzero so a caller's `make`/CI/dispatcher chain
        # observes the failure instead of silently treating a partial run as a clean pass.
        print(
            f"INCOMPLETE: {len(errs)}/{len(graded)} row(s) transport-errored (reader/judge API "
            "failure). The written envelope is marked \"complete\": false — render_report.py will "
            "refuse to report a QA-accuracy number from it. Fix the transport and re-run for a "
            "complete result.",
            file=sys.stderr,
            flush=True,
        )
        return 4
    print(f"wrote {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
