#!/usr/bin/env bash
# Phase snapshots for a drill scenario. Evidence is structured assertions and hashes — never
# secret values, raw stdout, or client configs. Secret VALUES go only to the private union file
# consumed by run_gates.sh.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${HERE}/lib.sh"

DIR="${1:-}"
PHASE="${2:-}"
require_scenario "${DIR}"

EV="${EVIDENCE_DIR}"
UNION="$(drill_root)/secrets-union.txt"
BEARER_FILE="${HOME_DIR}/.panella/owner-bearer"
APPROVAL_FILE="${HOME_DIR}/.panella/approval-token"
ENV_FILE="${HOME_DIR}/.env"

# The real user config mutates constantly for unrelated reasons (any live Claude session writes
# it), so whole-file hashes cannot prove non-interference. The assertion that matters: the drill
# must not add/remove/modify any `panella` MCP registration in the REAL user config. Entries are
# FINGERPRINTED, never dumped — a real registration carries a live Authorization header, and the
# scrub union only knows drill-minted secrets, so raw config in evidence would be an unscrubable
# credential leak.
user_config_panella_entries() {
  python3 - "${HOME}/.claude.json" <<'PY'
import hashlib
import json
import sys

try:
    data = json.load(open(sys.argv[1]))
except OSError:
    print("real-user-panella-entries: CONFIG_ABSENT")
    raise SystemExit(0)
except json.JSONDecodeError:
    # A torn read of a concurrently-written config: report it as its own state rather than
    # crashing the phase; the pre/post comparison still catches drill-caused differences.
    print("real-user-panella-entries: CONFIG_UNPARSEABLE")
    raise SystemExit(0)

def fingerprint(entry):
    canonical = json.dumps(entry, sort_keys=True).encode()
    return "sha256/12:" + hashlib.sha256(canonical).hexdigest()[:12]

entries = {}
if "panella" in data.get("mcpServers", {}):
    entries["<top>"] = fingerprint(data["mcpServers"]["panella"])
for proj, pdata in data.get("projects", {}).items():
    if "panella" in pdata.get("mcpServers", {}):
        entries[proj] = fingerprint(pdata["mcpServers"]["panella"])
print("real-user-panella-entries: " + json.dumps(entries, sort_keys=True))
PY
}

case "${PHASE}" in
  pre)
    {
      echo "phase: pre  scenario: ${SCENARIO}  proj: ${PROJ}"
      user_config_panella_entries
      echo "scenario-claude-config-inventory:"
      find "${CONFIG_DIR}" -type f 2>/dev/null | sort || true
      docker_state_by_label "${PROJ}"
    } > "${EV}/pre.txt"
    # A broken claude CLI must fail loudly — it is not the same as "no panella entry".
    if ! LIST_OUT="$(CLAUDE_CONFIG_DIR="${CONFIG_DIR}" claude mcp list 2>&1)"; then
      echo "FAIL: claude mcp list failed (CLI unusable in this scenario): ${LIST_OUT}" >&2
      exit 1
    fi
    if printf '%s\n' "${LIST_OUT}" | grep -q "^panella:"; then
      echo "FAIL: scenario config already has a panella entry" >&2
      exit 1
    fi
    echo "ok: ${EV}/pre.txt"
    ;;
  postup)
    # Fingerprints are truncated (12 hex chars) and labeled so secret scanners don't
    # pattern-match them as credentials; equality comparison is all the evidence needs.
    {
      echo "phase: postup  scenario: ${SCENARIO}"
      echo "approval-token fingerprint(sha256/12) = $(sha256_file "${APPROVAL_FILE}" | cut -c1-12)"
      echo "approval-token-mtime: $(mtime_file "${APPROVAL_FILE}")"
      echo "owner-bearer fingerprint(sha256/12) = $(sha256_file "${BEARER_FILE}" | cut -c1-12)"
      echo "owner-bearer-mtime: $(mtime_file "${BEARER_FILE}")"
      docker_state_by_label "${PROJ}"
    } > "${EV}/postup.txt"
    if grep -q "= ABSENT" "${EV}/postup.txt"; then
      echo "FAIL: postup ran but a secret file is absent — the box did not fully provision" >&2
      exit 1
    fi
    echo "ok: ${EV}/postup.txt"
    ;;
  preteardown)
    if [ "${SCENARIO}" = "d3" ]; then
      {
        echo "phase: preteardown  scenario: d3 — secrets must never have been minted"
        for f in "${BEARER_FILE}" "${APPROVAL_FILE}" "${ENV_FILE}"; do
          if [ -e "${f}" ]; then echo "FAIL exists: ${f}"; else echo "ok absent: ${f}"; fi
        done
        docker_state_by_label "${PROJ}"
      } > "${EV}/preteardown.txt"
      if grep -q "^FAIL" "${EV}/preteardown.txt"; then
        echo "FAIL: d3 minted secrets" >&2
        exit 1
      fi
    else
      touch "${UNION}"
      chmod 0600 "${UNION}"
      python3 - "${UNION}" "${SCENARIO}" "${BEARER_FILE}" "${APPROVAL_FILE}" "${ENV_FILE}" <<'PY'
import re
import sys

union, scenario, bearer, approval, envf = sys.argv[1:6]

def slurp(path):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return ""

rows = []
if v := slurp(bearer):
    rows.append((f"{scenario}-owner-bearer", v))
if v := slurp(approval):
    rows.append((f"{scenario}-approval-token", v))
for line in slurp(envf).splitlines():
    if m := re.match(r"^\s*(?:export\s+)?PANELLA_API_KEY=(.+)$", line):
        rows.append((f"{scenario}-api-key", m.group(1).strip()))
missing = {f"{scenario}-owner-bearer", f"{scenario}-approval-token", f"{scenario}-api-key"} - {r[0] for r in rows}
if missing:
    print(f"FAIL: could not collect: {sorted(missing)}", file=sys.stderr)
    sys.exit(1)
with open(union, "a", encoding="utf-8") as fh:
    for label, value in rows:
        fh.write(f"{label}\t{value}\n")
print(f"collected {len(rows)} secret values into the union")
PY
      {
        echo "phase: preteardown  scenario: ${SCENARIO}"
        echo "secret-union: collected owner-bearer, approval-token, PANELLA_API_KEY (values in union file only)"
        docker_state_by_label "${PROJ}"
      } > "${EV}/preteardown.txt"
    fi
    echo "ok: ${EV}/preteardown.txt"
    ;;
  post)
    {
      echo "phase: post  scenario: ${SCENARIO}"
      user_config_panella_entries
      echo "scenario-local-entry-check:"
      if [ -f "${CONFIG_DIR}/.claude.json" ]; then
        python3 - "${CONFIG_DIR}/.claude.json" <<'PY'
import json
import sys

# Scan the WHOLE scenario config — top-level and every project key — so a registration that
# landed under an unexpected scope or cwd still fails the residue check.
data = json.load(open(sys.argv[1]))
where = []
if "panella" in data.get("mcpServers", {}):
    where.append("<top>")
where.extend(proj for proj, pdata in data.get("projects", {}).items() if "panella" in pdata.get("mcpServers", {}))
print(f"FAIL: panella entry still registered at: {where}" if where else "ok: no panella entry anywhere in the scenario config")
PY
      else
        echo "ok: scenario claude config absent"
      fi
      docker_state_by_label "${PROJ}"
    } > "${EV}/post.txt"
    if grep -q "^FAIL" "${EV}/post.txt"; then
      echo "FAIL: residue detected — see ${EV}/post.txt" >&2
      exit 1
    fi
    if ! python3 - "${EV}/pre.txt" "${EV}/post.txt" <<'PY'
import sys

def entries(path):
    rows = [line for line in open(path) if line.startswith("real-user-panella-entries: ")]
    return rows[-1].strip() if rows else None

pre, post = entries(sys.argv[1]), entries(sys.argv[2])
if post is None:
    print("FAIL: post snapshot missing the real-user panella-entries line")
    sys.exit(1)
# A pre snapshot from an older harness revision lacks the line; the drill must then have left
# zero panella registrations behind.
expected = pre if pre is not None else 'real-user-panella-entries: {}'
ok = post == expected
print("ok: real user panella registrations unchanged" if ok else f"FAIL: real user panella registrations changed\n  pre:  {expected}\n  post: {post}")
sys.exit(0 if ok else 1)
PY
    then
      echo "FAIL: real user config panella registrations drifted during the drill" >&2
      exit 1
    fi
    echo "ok: ${EV}/post.txt"
    ;;
  *)
    echo "usage: $0 <scenario-dir> pre|postup|preteardown|post" >&2
    exit 1
    ;;
esac
