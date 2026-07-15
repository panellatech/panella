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

user_config_hashes() {
  echo "real-user-claude-json: $(sha256_file "${HOME}/.claude.json")"
  echo "real-user-claude-dir-mcp: $(sha256_file "${HOME}/.claude/.claude.json")"
}

case "${PHASE}" in
  pre)
    {
      echo "phase: pre  scenario: ${SCENARIO}  proj: ${PROJ}"
      user_config_hashes
      echo "scenario-claude-config-inventory:"
      find "${CONFIG_DIR}" -type f 2>/dev/null | sort || true
      docker_state_by_label "${PROJ}"
    } > "${EV}/pre.txt"
    if CLAUDE_CONFIG_DIR="${CONFIG_DIR}" claude mcp list 2>/dev/null | grep -q "^panella:"; then
      echo "FAIL: scenario config already has a panella entry" >&2
      exit 1
    fi
    echo "ok: ${EV}/pre.txt"
    ;;
  postup)
    {
      echo "phase: postup  scenario: ${SCENARIO}"
      echo "approval-token-sha256: $(sha256_file "${APPROVAL_FILE}")"
      echo "approval-token-mtime: $(mtime_file "${APPROVAL_FILE}")"
      echo "owner-bearer-sha256: $(sha256_file "${BEARER_FILE}")"
      echo "owner-bearer-mtime: $(mtime_file "${BEARER_FILE}")"
      docker_state_by_label "${PROJ}"
    } > "${EV}/postup.txt"
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
      user_config_hashes
      echo "scenario-local-entry-check:"
      if [ -f "${CONFIG_DIR}/.claude.json" ]; then
        python3 - "${CONFIG_DIR}/.claude.json" "${PROJECT_CWD}" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
proj = data.get("projects", {}).get(sys.argv[2], {})
servers = proj.get("mcpServers", {})
print("FAIL: panella entry still registered" if "panella" in servers else "ok: no panella entry for the drill project")
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

def lines(path, prefix):
    return sorted(line for line in open(path) if line.startswith(prefix))

pre, post = sys.argv[1], sys.argv[2]
ok = lines(pre, "real-user-") == lines(post, "real-user-")
print("ok: real user Claude config byte-identical" if ok else "FAIL: real user Claude config changed")
sys.exit(0 if ok else 1)
PY
    then
      echo "FAIL: real user config drifted during the drill" >&2
      exit 1
    fi
    echo "ok: ${EV}/post.txt"
    ;;
  *)
    echo "usage: $0 <scenario-dir> pre|postup|preteardown|post" >&2
    exit 1
    ;;
esac
