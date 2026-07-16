#!/usr/bin/env python3
"""Chief drift analysis (host-side). Proves the torch cu13x->cpu swap does not change embeddings.

Inputs: old.json (main@5c4172f, CUDA torch), new.json (3b, CPU torch), new2.json (3b again = noise
baseline / determinism floor). All produced by embed_in_image.py on the SAME corpus + queries files
(corpus lines first, then query lines, concatenated).

Gates (all must pass):
  - determinism floor: min cosine(new_i, new2_i) — establishes measurement noise; expect ~1.0
  - drift: min cosine(old_i, new_i) over the corpus >= min_cosine (default 0.9999)
  - retrieval: for each query, top-10 corpus rank under OLD vs NEW embeddings overlaps >= 9/10
Usage: compare.py <old.json> <new.json> <new2.json> <n_corpus> [min_cosine]
"""
import json
import sys

import numpy as np


def load(path):
    with open(path) as fh:
        return json.load(fh)


def cos_rows(a, b):
    an = a / np.linalg.norm(a, axis=1, keepdims=True)
    bn = b / np.linalg.norm(b, axis=1, keepdims=True)
    return np.sum(an * bn, axis=1)


def topk(query_vec, corpus, k=10):
    qn = query_vec / np.linalg.norm(query_vec)
    cn = corpus / np.linalg.norm(corpus, axis=1, keepdims=True)
    sims = cn @ qn
    return set(np.argsort(-sims)[:k].tolist())


def main():
    old = load(sys.argv[1])
    new = load(sys.argv[2])
    new2 = load(sys.argv[3])
    n_corpus = int(sys.argv[4])
    min_cosine = float(sys.argv[5]) if len(sys.argv) > 5 else 0.9999

    assert old["texts"] == new["texts"] == new2["texts"], "text ordering diverged across runs"
    assert old["dim"] == new["dim"] == 384, f"dim mismatch: {old['dim']}/{new['dim']}"
    # The comparison is only meaningful if the two sides are genuinely the OLD (CUDA) and NEW (CPU)
    # builds — otherwise a misconfigured rerun (e.g. OLD_IMG == NEW_IMG) yields a vacuous cosine=1.0.
    assert old["cuda"] is not None, f"old side is not a CUDA build (cuda={old['cuda']}); wrong OLD_IMG?"
    assert new["cuda"] is None, f"new side is not a CPU build (cuda={new['cuda']}); wrong NEW_IMG?"
    assert old["torch"] != new["torch"], f"old and new used the same torch build {old['torch']!r}; same image?"

    old_v = np.array(old["vecs"])
    new_v = np.array(new["vecs"])
    new2_v = np.array(new2["vecs"])
    # Reject degenerate embeddings at the source before any cosine is computed: a NaN/inf (non-finite)
    # OR a zero-norm vector both make cos_rows() produce NaN, and `NaN < threshold` is False — so a
    # degenerate run could otherwise slip past the determinism/drift gates and print "PASSED".
    for label, arr in (("old", old_v), ("new", new_v), ("new2", new2_v)):
        assert np.isfinite(arr).all(), f"{label} embeddings contain non-finite values (NaN/inf) — degenerate run"
        assert (np.linalg.norm(arr, axis=1) > 0).all(), f"{label} embeddings contain a zero-norm vector — degenerate run"

    failures = []
    det = cos_rows(new_v, new2_v)
    det_min = float(det.min())
    print(f"[determinism] min cosine(new,new2) = {det_min:.8f} over {len(det)} texts (floor; expect ~1.0)")
    # Gate the floor too: if the same image embeds the same text differently, the embeddings are not
    # reproducible and the drift comparison below is meaningless — fail rather than silently certify.
    if det_min < min_cosine:
        failures.append(f"determinism cosine {det_min:.8f} < {min_cosine} (embeddings not reproducible)")

    drift = cos_rows(old_v[:n_corpus], new_v[:n_corpus])
    drift_min = float(drift.min())
    drift_mean = float(drift.mean())
    worst = int(drift.argmin())
    print(f"[drift] min cosine(old,new) = {drift_min:.8f}  mean = {drift_mean:.8f}  (threshold {min_cosine})")
    print(f"        worst text[{worst}]: {old['texts'][worst][:70]!r}")
    if drift_min < min_cosine:
        failures.append(f"drift min cosine {drift_min:.8f} < {min_cosine}")

    corpus_old, corpus_new = old_v[:n_corpus], new_v[:n_corpus]
    queries_old, queries_new = old_v[n_corpus:], new_v[n_corpus:]
    overlaps = []
    for i in range(len(queries_old)):
        overlap = len(topk(queries_old[i], corpus_old) & topk(queries_new[i], corpus_new))
        overlaps.append(overlap)
        if overlap < 9:
            failures.append(f"query[{i}] top-10 overlap {overlap}/10 < 9 : {new['texts'][n_corpus + i][:60]!r}")
    print(f"[retrieval] per-query top-10 overlap (old-rank vs new-rank): "
          f"min={min(overlaps)} mean={np.mean(overlaps):.2f} over {len(overlaps)} queries")

    print(f"\nold torch={old['torch']} cuda={old['cuda']}  |  new torch={new['torch']} cuda={new['cuda']}")
    if failures:
        print("\nDRIFT GATE FAILED:")
        for failure in failures:
            print("  -", failure)
        sys.exit(1)
    print(f"\nDRIFT GATE PASSED: determinism_min={det_min:.8f} "
          f"drift_min={drift_min:.8f} retrieval_min={min(overlaps)}/10")


if __name__ == "__main__":
    main()
