#!/usr/bin/env bash
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${root}"

tmp_dir="$(mktemp -d)"
render_dir="$(mktemp -d)"
env_backup="$(mktemp)"
had_env=0

cleanup() {
  status=$?
  trap - EXIT

  if [ "${status}" -ne 0 ]; then
    echo "::group::docker compose logs"
    docker compose logs --no-color || true
    echo "::endgroup::"
  fi

  docker compose down -v --remove-orphans || true

  if [ "${had_env}" -eq 1 ]; then
    cp "${env_backup}" .env
  else
    rm -f .env
  fi

  rm -rf "${tmp_dir}" "${render_dir}" "${env_backup}"
  exit "${status}"
}
trap cleanup EXIT

if [ -f .env ]; then
  cp .env "${env_backup}"
  had_env=1
fi

api_key="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(32))')"
printf 'PANELLA_API_KEY=%s\n' "${api_key}" > .env

echo "boot_check: docker compose build"
docker compose build

echo "boot_check: docker compose up --wait"
docker compose up --wait --wait-timeout 300

assert_status() {
  label="$1"
  expected="$2"
  url="$3"
  body="${tmp_dir}/${label}.body"
  err="${tmp_dir}/${label}.stderr"

  set +e
  http_status="$(curl -fsS --retry 10 --retry-all-errors --retry-delay 2 --connect-timeout 5 --max-time 15 -o "${body}" -w '%{http_code}' "${url}" 2>"${err}")"
  curl_status=$?
  set -e

  echo "assert=${label}"
  echo "  url=${url}"
  echo "  expected_http_status=${expected}"
  echo "  actual_http_status=${http_status}"
  echo "  curl_exit=${curl_status}"

  if [ -s "${body}" ]; then
    sed 's/^/  body: /' "${body}"
  fi
  if [ -s "${err}" ]; then
    sed 's/^/  curl_stderr: /' "${err}"
  fi

  if [ "${http_status}" != "${expected}" ]; then
    echo "assert=${label} result=fail expected ${expected}, got ${http_status}" >&2
    exit 1
  fi

  if [ "${expected}" = "200" ] && [ "${curl_status}" -ne 0 ]; then
    echo "assert=${label} result=fail curl failed for expected 200 response" >&2
    exit 1
  fi

  echo "assert=${label} result=pass"
}

assert_status "health" "200" "http://127.0.0.1:8001/v1/health"
assert_status "mcp_unauth" "401" "http://127.0.0.1:8001/mcp"

echo "boot_check: panella-render-config --out ${render_dir}"
if command -v panella-render-config >/dev/null 2>&1; then
  env -u PANELLA_GOVERNANCE_OVERLAY panella-render-config --out "${render_dir}"
elif command -v python3 >/dev/null 2>&1; then
  env -u PANELLA_GOVERNANCE_OVERLAY python3 -m panella_selfhost.render --out "${render_dir}"
elif command -v python >/dev/null 2>&1; then
  env -u PANELLA_GOVERNANCE_OVERLAY python -m panella_selfhost.render --out "${render_dir}"
else
  echo "boot_check=fail no Python interpreter available for panella-render-config fallback" >&2
  exit 1
fi

scripts/ci/check-rendered-identity.sh "${render_dir}"
echo "boot_check=pass"
