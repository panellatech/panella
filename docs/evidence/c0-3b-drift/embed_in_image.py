#!/usr/bin/env python3
"""Run INSIDE a panella store container: load the baked embedding model exactly as the store does
(offline, from HF_HOME=/opt/hf-cache), embed every line of the input file, dump vectors as JSON.

Usage: embed_in_image.py <input.txt> <out.json>
The store computes on CPU (no GPU in deploy targets); we force device='cpu' to match, so the only
variable between the old (CUDA-wheel) and new (CPU-wheel) images is the torch build itself.
"""
import glob
import json
import sys

import torch
from sentence_transformers import SentenceTransformer

snaps = glob.glob('/opt/hf-cache/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/*')
assert len(snaps) == 1, f"expected exactly one baked snapshot, found {snaps}"
model = SentenceTransformer(snaps[0], device='cpu')
with open(sys.argv[1], encoding='utf-8') as fh:
    texts = [ln.strip() for ln in fh if ln.strip()]
vecs = model.encode(texts, normalize_embeddings=False, convert_to_numpy=True).tolist()
payload = {
    "torch": torch.__version__,
    "cuda": torch.version.cuda,
    "dim": len(vecs[0]) if vecs else 0,
    "texts": texts,
    "vecs": vecs,
}
with open(sys.argv[2], 'w') as fh:
    json.dump(payload, fh)
print(f"embedded {len(texts)} texts, dim={len(vecs[0]) if vecs else 0}, "
      f"torch={torch.__version__} cuda={torch.version.cuda}")
