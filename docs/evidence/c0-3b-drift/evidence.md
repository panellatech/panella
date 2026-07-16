# C0-3b vector-drift evidence — CUDA torch → CPU torch

**Claim under test:** slimming the store image by swapping the CUDA torch wheel for `torch==2.13.0+cpu`
does not change the embeddings the store produces, so existing `sqlite_vec` stores stay valid and
retrieval is unaffected.

**Method:** the same baked model (`all-MiniLM-L6-v2`, HF revision `1110a243`) is loaded on CPU inside
each image and used to embed a fixed 59-sentence corpus + 15 queries. The deploy targets have no GPU,
so both images compute on CPU — the only variable is the torch wheel build. Reproduce with:

```
OLD_IMG=<build of main@5c4172f>  NEW_IMG=<3b build>  bash run_drift.sh      # numerical drift
OLD_IMG=..                        NEW_IMG=..           bash migrate_test.sh  # populated-volume migration
```

Corpus/queries/scripts are checked in; the vector dumps (`*.json`, `all.txt`) are regenerable and
git-ignored.

## Images compared (recorded, not assumed)

| side | image | torch | image size |
|------|-------|-------|-----------|
| old  | build of `panella/panella` main **`5c4172f`** | `2.13.0+cu130` (cuda 13.0) | 5.16 GiB |
| new  | 3b (`task/c0-3b-slim`) | `2.13.0+cpu` (cuda None) | **1.43 GiB** |

Arch: `linux/arm64` (chief's native build). Both images bake the identical model snapshot; the
torch **version** is identical (2.13.0) — only the wheel variant differs (cu130 vs cpu). On amd64 the
same store-constraints.txt pins the same versions, so the transition is the same cu→cpu swap.

## Numerical drift (run_drift.sh)

- **determinism floor** — `min cosine(new, new_rerun) = 1.00000000` over 74 texts (embedding is
  bit-deterministic; establishes the measurement floor).
- **drift** — `min cosine(old, new) = 1.00000000`, `mean = 1.00000000` over the 59-sentence corpus
  (threshold ≥ 0.9999). The CUDA and CPU builds produce **identical** embeddings to 8 decimals.
- **retrieval** — per-query top-10 overlap (old-embedding ranking vs new-embedding ranking) =
  **10/10 on all 15 queries** (mean 10.00).

Interpretation: same torch 2.13.0 source + same weights + CPU execution in both → identical MKL/oneDNN
kernels → zero numerical drift. The CUDA wheel only added the GPU dependency closure (nvidia + triton,
~3.5GB), never a different CPU code path.

`compare.py` asserts each side's recorded torch metadata (old has a CUDA version, new has
`cuda is None`, and the two torch builds differ), so the gate cannot pass vacuously on a misconfigured
rerun that fed it the same image twice.

## Populated-volume migration (migrate_test.sh)

A store DB is populated on the OLD image and then served by the NEW image over the **same volume**:

- OLD (CUDA torch) wrote 20 memories → `GET /api/memories total = 20`, semantic search returns results.
- NEW (CPU torch) on the same volume → `total = 20` (all old-written memories readable) and the
  held-out query *"How is embedding drift between two model builds measured?"* retrieves the exact
  expected memory as the **top-1** `/api/search` hit over the old-written vectors (the test asserts the
  specific expected content, not merely that some row is returned).
- NEW then writes 1 more → `total = 21` (the migrated store is still writable).

Result: **MIGRATION PASSED** — the sqlite_vec store written with CUDA-torch embeddings is fully
retrievable, semantically searchable, and extendable under the CPU-torch image. No re-embedding,
dimension break, or data loss.

## Conclusion

Zero embedding drift; existing stores migrate cleanly old→new and remain writable. The image drops
5.45 GiB → 1.43 GiB (−74%) with no CUDA/nvidia/triton residue and no change to what the store computes.
