#!/usr/bin/env bash
# Shared helpers for the install drills. Source, don't execute.

drill_root() {
  echo "${PANELLA_DRILL_ROOT:-/tmp/panella-drills}"
}

# The compose project `panella up` derives for a home: panella-box-<sha256(realpath)[:8]>.
# Must match panella/cli/up.py.
proj_for_home() {
  python3 - "$1" <<'PY'
import hashlib, os, sys
home = os.path.realpath(sys.argv[1])
print("panella-box-" + hashlib.sha256(home.encode()).hexdigest()[:8])
PY
}

sha256_file() {
  python3 - "$1" <<'PY'
import hashlib, sys
try:
    print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
except FileNotFoundError:
    print("ABSENT")
PY
}

mtime_file() {
  python3 - "$1" <<'PY'
import os, sys
try:
    print(int(os.stat(sys.argv[1]).st_mtime))
except FileNotFoundError:
    print("ABSENT")
PY
}

docker_state_by_label() {
  local proj="$1"
  echo "containers:"
  docker ps -aq --filter "label=com.docker.compose.project=${proj}" | sort
  echo "volumes:"
  docker volume ls -q --filter "label=com.docker.compose.project=${proj}" | sort
  echo "networks:"
  docker network ls -q --filter "label=com.docker.compose.project=${proj}" | sort
}

require_scenario() {
  local dir="$1"
  if [ ! -f "${dir}/scenario.env" ]; then
    echo "not a scenario dir (missing scenario.env): ${dir}" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  . "${dir}/scenario.env"
}
