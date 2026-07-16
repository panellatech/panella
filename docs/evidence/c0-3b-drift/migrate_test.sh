#!/usr/bin/env bash
# Chief drift-evidence: populated-volume migration. Proves a sqlite_vec store WRITTEN by the OLD image
# (CUDA-torch embeddings) stays fully retrievable + extendable when served by the NEW image (CPU torch).
#   OLD_IMG = build of main@5c4172f    NEW_IMG = 3b build
# Writes N memories on OLD, restarts on NEW over the SAME volume, asserts all N retrievable, then
# writes 1 more (N -> N+1). Usage: OLD_IMG=.. NEW_IMG=.. bash migrate_test.sh
# Portable to bash 3.2 (macOS): no mapfile; JSON built via env-var + `curl --data @file` to dodge
# nested-quote mangling. API verified against mcp-memory-service 10.67.1: POST/search need Bearer auth,
# store count = GET /api/memories .total, POST success = HTTP 200.
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
: "${OLD_IMG:?set OLD_IMG}"; : "${NEW_IMG:?set NEW_IMG}"
# Per-run unique names so the harness never force-removes a pre-existing container/volume it did not create.
KEY=drift-mig-key; NAME="mig-drift-store-$$"; VOL="drift-mig-vol-$$"; PORT=18077; BASE="http://127.0.0.1:${PORT}"
AUTH="Authorization: Bearer ${KEY}"; N=20
TMPD="$(mktemp -d)"   # per-run temp dir so concurrent harnesses never share request/response files
cleanup(){ docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOL" >/dev/null 2>&1 || true; rm -rf "$TMPD"; }
trap cleanup EXIT
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker volume rm "$VOL" >/dev/null 2>&1 || true

start(){ # <image> <label>
  docker run -d --name "$NAME" -e MCP_API_KEY="$KEY" -p ${PORT}:8000 -v "${VOL}:/data" "$1" memory server --http >/dev/null
  for i in $(seq 1 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null)" = healthy ] && return 0; sleep 3; done
  echo "FAIL: $2 image did not become healthy"; docker logs "$NAME" 2>&1 | tail -20; exit 1
}
post(){ # <content> -> echoes http code
  C="$1" python3 -c 'import json,os;print(json.dumps({"content":os.environ["C"],"tags":["drift-mig"]}))' > "$TMPD/body.json"
  curl -s -o "$TMPD/post.json" -w '%{http_code}' -X POST "${BASE}/api/memories" \
    -H "$AUTH" -H 'content-type: application/json' --data @"$TMPD/body.json"
}
count(){ curl -s "${BASE}/api/memories" -H "$AUTH" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("total",""))' 2>/dev/null; }
# top1 returns the CONTENT of the top /api/search hit (results[0].memory.content) so we can assert the
# SPECIFIC expected memory is retrieved, not merely that some row came back.
top1(){ # <query> -> top result content ("" if none)
  Q="$1" python3 -c 'import json,os;print(json.dumps({"query":os.environ["Q"],"n_results":5}))' > "$TMPD/sbody.json"
  curl -s -X POST "${BASE}/api/search" -H "$AUTH" -H 'content-type: application/json' --data @"$TMPD/sbody.json" \
    | python3 -c 'import json,sys
d=json.load(sys.stdin); r=d.get("results",[])
print(r[0]["memory"]["content"] if r else "")' 2>/dev/null
}
# Query whose unambiguous best match is corpus line 20 (within the first N written); the retrieval
# assertions below require this exact memory to be the top hit on both OLD and NEW.
QUERY='How is embedding drift between two model builds measured?'
EXPECT='Embedding drift between two model builds'

echo "===== OLD image ${OLD_IMG}: write ${N} memories ====="
start "$OLD_IMG" OLD
ok=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  code="$(post "$line")"
  case "$code" in
    200|201) ok=$((ok+1));;
    401|403) echo "FAIL: auth rejected (HTTP $code)"; cat "$TMPD/post.json"; exit 1;;
    *) echo "warn: POST HTTP $code for: ${line:0:40}"; cat "$TMPD/post.json"; echo;;
  esac
done < <(head -n "$N" "$DIR/corpus.txt")
echo "OLD wrote ok=$ok / $N"
old_count="$(count)"; echo "OLD GET /api/memories total=$old_count"
old_top="$(top1 "$QUERY")"; echo "OLD top-1: ${old_top:0:60}"
[ "$ok" = "$N" ] || { echo "FAIL: only $ok/$N writes succeeded on OLD"; exit 1; }
[ "$old_count" = "$N" ] || { echo "FAIL: OLD total=$old_count expected $N"; exit 1; }
case "$old_top" in *"$EXPECT"*) echo "OLD retrieves the expected memory as top-1 OK";; *) echo "FAIL: OLD top-1 is not the expected memory: $old_top"; exit 1;; esac

echo "===== restart on NEW image ${NEW_IMG} over SAME volume ====="
docker rm -f "$NAME" >/dev/null 2>&1 || true
start "$NEW_IMG" NEW
new_count="$(count)"; echo "NEW GET /api/memories total=$new_count (expect $N)"
new_top="$(top1 "$QUERY")"; echo "NEW top-1: ${new_top:0:60}"
[ "$new_count" = "$N" ] || { echo "FAIL: NEW sees $new_count memories, expected $N (old-written DB not fully readable)"; exit 1; }
# The SAME old-written memory must still be the top semantic hit under the CPU-torch image — proves
# retrieval correctness over the migrated vectors, not merely that some row is returned.
case "$new_top" in *"$EXPECT"*) echo "NEW retrieves the same expected memory as top-1 over the old-written vectors OK";; *) echo "FAIL: NEW top-1 is not the expected memory (migration broke retrieval): $new_top"; exit 1;; esac

echo "===== NEW writes 1 more (N -> N+1) ====="
code="$(post 'A brand new memory written by the CPU-torch image after migration.')"
{ [ "$code" = 200 ] || [ "$code" = 201 ]; } || { echo "FAIL: N+1 write HTTP $code"; cat "$TMPD/post.json"; exit 1; }
final="$(count)"; echo "NEW total=$final (expect $((N+1)))"
[ "$final" = "$((N+1))" ] || { echo "FAIL: after N+1 write total=$final, expected $((N+1))"; exit 1; }

echo "===== MIGRATION PASSED: OLD wrote $N -> NEW read $new_count (expected memory top-1) + wrote 1 -> $final ====="
