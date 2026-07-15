#!/usr/bin/env bash
# Tear down exactly one scenario's box: compose down, labeled-stragglers removal (the D-3 orphan
# is not compose-owned), then label-filtered emptiness assertions. Keeps the evidence dir.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${HERE}/lib.sh"

DIR="${1:-}"
require_scenario "${DIR}"

if [ -f "${HOME_DIR}/docker-compose.yml" ]; then
  docker compose -p "${PROJ}" -f "${HOME_DIR}/docker-compose.yml" down --volumes --remove-orphans || true
fi

for c in $(docker ps -aq --filter "label=com.docker.compose.project=${PROJ}"); do
  docker rm -f "${c}" >/dev/null
done
for v in $(docker volume ls -q --filter "label=com.docker.compose.project=${PROJ}"); do
  docker volume rm "${v}" >/dev/null
done
for n in $(docker network ls -q --filter "label=com.docker.compose.project=${PROJ}"); do
  docker network rm "${n}" >/dev/null
done

{
  echo "phase: teardown  scenario: ${SCENARIO}  proj: ${PROJ}"
  docker_state_by_label "${PROJ}"
} > "${EVIDENCE_DIR}/teardown.txt"

LEFT="$(docker ps -aq --filter "label=com.docker.compose.project=${PROJ}"; \
        docker volume ls -q --filter "label=com.docker.compose.project=${PROJ}"; \
        docker network ls -q --filter "label=com.docker.compose.project=${PROJ}")"
if [ -n "${LEFT}" ]; then
  echo "FAIL: labeled resources survived teardown:" >&2
  echo "${LEFT}" >&2
  exit 1
fi

rm -rf "${HOME_DIR}"
echo "ok: ${PROJ} fully removed; evidence kept at ${EVIDENCE_DIR}"
