#!/usr/bin/env bash
# Tear down exactly one scenario's box: compose down, labeled-stragglers removal (the D-3 orphan
# is not compose-owned), then label-filtered emptiness assertions. Keeps the evidence dir.
# Every docker query is an explicit checked assignment: a failed query must fail the teardown,
# never masquerade as "nothing left" (that would delete the home while resources survive).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${HERE}/lib.sh"

DIR="${1:-}"
require_scenario "${DIR}"

FILTER="label=com.docker.compose.project=${PROJ}"

if [ -f "${HOME_DIR}/docker-compose.yml" ]; then
  docker compose -p "${PROJ}" -f "${HOME_DIR}/docker-compose.yml" down --volumes --remove-orphans || true
fi

CONTAINERS="$(docker ps -aq --filter "${FILTER}")"
for c in ${CONTAINERS}; do
  docker rm -f "${c}" >/dev/null
done
VOLUMES="$(docker volume ls -q --filter "${FILTER}")"
for v in ${VOLUMES}; do
  docker volume rm "${v}" >/dev/null
done
NETWORKS="$(docker network ls -q --filter "${FILTER}")"
for n in ${NETWORKS}; do
  docker network rm "${n}" >/dev/null
done

LEFT_C="$(docker ps -aq --filter "${FILTER}")"
LEFT_V="$(docker volume ls -q --filter "${FILTER}")"
LEFT_N="$(docker network ls -q --filter "${FILTER}")"

{
  echo "phase: teardown  scenario: ${SCENARIO}  proj: ${PROJ}"
  docker_state_by_label "${PROJ}"
} > "${EVIDENCE_DIR}/teardown.txt"

if [ -n "${LEFT_C}${LEFT_V}${LEFT_N}" ]; then
  echo "FAIL: labeled resources survived teardown:" >&2
  printf '%s\n' ${LEFT_C} ${LEFT_V} ${LEFT_N} >&2
  exit 1
fi

rm -rf "${HOME_DIR}"
echo "ok: ${PROJ} fully removed; evidence kept at ${EVIDENCE_DIR}"
