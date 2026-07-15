#!/usr/bin/env bash
# Build a locally-consumable release candidate: local registry images, digest-pinned compose
# asset, and a wheel that embeds it. Mirrors .github/workflows/linux-e2e.yml for a dev machine.
# Run from the repo root. Idempotent; safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${HERE}/lib.sh"

REGISTRY_NAME="panella-drill-registry"
REGISTRY_ADDR="127.0.0.1:5001"
TAG="drill"
ROOT="$(drill_root)"
mkdir -p "${ROOT}"

if [ ! -f pyproject.toml ] || [ ! -f docker-compose.yml ]; then
  echo "run from the repo root" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon unreachable" >&2
  exit 1
fi

if ! curl -fsS "http://${REGISTRY_ADDR}/v2/" >/dev/null 2>&1; then
  docker rm -f "${REGISTRY_NAME}" >/dev/null 2>&1 || true
  docker run -d --name "${REGISTRY_NAME}" -p 5001:5000 registry:2 >/dev/null
  for _ in $(seq 1 30); do
    curl -fsS "http://${REGISTRY_ADDR}/v2/" >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -fsS "http://${REGISTRY_ADDR}/v2/" >/dev/null

docker build --target store -t "${REGISTRY_ADDR}/panella-store:${TAG}" .
docker build --target app -t "${REGISTRY_ADDR}/panella-app:${TAG}" .
docker push "${REGISTRY_ADDR}/panella-store:${TAG}"
docker push "${REGISTRY_ADDR}/panella-app:${TAG}"

STORE_DIGEST="$(docker image inspect --format='{{index .RepoDigests 0}}' "${REGISTRY_ADDR}/panella-store:${TAG}" | sed 's|.*@||')"
APP_DIGEST="$(docker image inspect --format='{{index .RepoDigests 0}}' "${REGISTRY_ADDR}/panella-app:${TAG}" | sed 's|.*@||')"
VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
HEAD_SHA="$(git rev-parse HEAD)"

python3 scripts/release/pin_compose.py --compose docker-compose.yml \
  --store-ref "${REGISTRY_ADDR}/panella-store@${STORE_DIGEST}" \
  --app-ref "${REGISTRY_ADDR}/panella-app@${APP_DIGEST}" \
  --version "${VERSION}" --run-id 0 --run-attempt 1 --event local --ref local \
  --head-sha "${HEAD_SHA}" --out-compose panella_selfhost/_assets/compose.pinned.yml \
  --out-digests "${ROOT}/digests.json"

rm -rf dist
if command -v uv >/dev/null 2>&1; then
  uv build --wheel
else
  python3 -m build --wheel
fi
WHEEL="$(cd dist && ls panella-*.whl)"
python3 - "dist/${WHEEL}" <<'PY'
import sys, zipfile
names = zipfile.ZipFile(sys.argv[1]).namelist()
assert any(n.endswith("panella_selfhost/_assets/compose.pinned.yml") for n in names), \
    "wheel is missing the pinned compose asset"
PY

{
  echo "DRILL_WHEEL=$(pwd)/dist/${WHEEL}"
  echo "DRILL_VERSION=${VERSION}"
  echo "DRILL_HEAD_SHA=${HEAD_SHA}"
  echo "DRILL_STORE_DIGEST=${STORE_DIGEST}"
  echo "DRILL_APP_DIGEST=${APP_DIGEST}"
} > "${ROOT}/build-manifest.env"

echo "built: dist/${WHEEL} (pinned to local registry images)"
echo "manifest: ${ROOT}/build-manifest.env"
