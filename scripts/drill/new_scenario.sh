#!/usr/bin/env bash
# Create an isolated drill scenario: unique box home, scenario-scoped CLAUDE_CONFIG_DIR, a clean
# project cwd for client wiring, and an evidence dir. D-3 pre-seeds an orphan labeled volume.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${HERE}/lib.sh"

SCENARIO="${1:-}"
case "${SCENARIO}" in
  d1|d2|d3) ;;
  *) echo "usage: $0 d1|d2|d3" >&2; exit 1 ;;
esac

ROOT="$(drill_root)"
if [ ! -f "${ROOT}/build-manifest.env" ]; then
  echo "no build manifest — run build_local_release.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
. "${ROOT}/build-manifest.env"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
DIR="${ROOT}/${SCENARIO}-${TS}"
mkdir -p "${DIR}/home" "${DIR}/claude-config" "${DIR}/project-cwd" "${DIR}/evidence"

HOME_DIR="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${DIR}/home")"
PROJ="$(proj_for_home "${HOME_DIR}")"

ORPHAN_VOLUME=""
if [ "${SCENARIO}" = "d3" ]; then
  ORPHAN_VOLUME="${PROJ}-drill-orphan"
  docker volume create --label "com.docker.compose.project=${PROJ}" "${ORPHAN_VOLUME}" >/dev/null
fi

{
  echo "SCENARIO=${SCENARIO}"
  echo "SCENARIO_DIR=${DIR}"
  echo "HOME_DIR=${HOME_DIR}"
  echo "CONFIG_DIR=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${DIR}/claude-config")"
  echo "PROJECT_CWD=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${DIR}/project-cwd")"
  echo "EVIDENCE_DIR=${DIR}/evidence"
  echo "PROJ=${PROJ}"
  echo "ORPHAN_VOLUME=${ORPHAN_VOLUME}"
  echo "DRILL_WHEEL=${DRILL_WHEEL}"
  echo "DRILL_VERSION=${DRILL_VERSION}"
} > "${DIR}/scenario.env"

echo "${DIR}"
