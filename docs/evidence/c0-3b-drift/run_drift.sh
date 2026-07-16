#!/usr/bin/env bash
# Chief drift-evidence orchestrator (numerical). Requires two already-built store images:
#   OLD_IMG = build of main@5c4172f (CUDA torch)   NEW_IMG = 3b build (CPU torch)
# Embeds corpus+queries offline (--network none) in each image, runs NEW twice for a determinism
# floor, then compares. Usage: OLD_IMG=.. NEW_IMG=.. bash run_drift.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${OLD_IMG:?set OLD_IMG}"; : "${NEW_IMG:?set NEW_IMG}"
cat "$DIR/corpus.txt" "$DIR/queries.txt" > "$DIR/all.txt"
N_CORPUS="$(grep -c . "$DIR/corpus.txt")"
echo "corpus=$N_CORPUS queries=$(grep -c . "$DIR/queries.txt")  OLD=$OLD_IMG NEW=$NEW_IMG"
# --user host-uid so the JSON written into the bind-mount is owned by the caller (works on native
# Linux, not just Docker Desktop's uid-agnostic mounts). site-packages + /opt/hf-cache are world-readable.
embed () { docker run --rm --network none --user "$(id -u):$(id -g)" -v "$DIR":/io --entrypoint python "$1" /io/embed_in_image.py /io/all.txt "/io/$2"; }
echo "--- embedding in OLD (CUDA torch) ---"; embed "$OLD_IMG" old.json
echo "--- embedding in NEW (CPU torch) ---";  embed "$NEW_IMG" new.json
echo "--- embedding in NEW again (determinism floor) ---"; embed "$NEW_IMG" new2.json
echo "--- compare ---"
# Run the comparator INSIDE the NEW image: it has numpy, so `bash run_drift.sh` is self-contained on
# any Docker host (numpy is a store dependency, not a panella host dependency).
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$DIR":/io --entrypoint python "$NEW_IMG" \
  /io/compare.py /io/old.json /io/new.json /io/new2.json "$N_CORPUS" 0.9999
