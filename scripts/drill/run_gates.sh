#!/usr/bin/env bash
# The evidence publish gate. Nothing from the drill bundle (PR bodies included) may be published
# before this passes: exact-match scrub of every minted secret, a zero-hit re-scan, gitleaks, and
# an optional extra scanner (PANELLA_EXTRA_SCAN=<executable> — it receives the bundle dir as $1).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
. "${HERE}/lib.sh"

ROOT="$(drill_root)"
UNION="${ROOT}/secrets-union.txt"
BUNDLE="${ROOT}/evidence-bundle"

if [ ! -f "${UNION}" ]; then
  echo "no secret union at ${UNION} — run evidence.sh preteardown for the secret-minting scenarios first" >&2
  exit 1
fi

rm -rf "${BUNDLE}"
mkdir -p "${BUNDLE}"
found=0
for dir in "${ROOT}"/d1-* "${ROOT}"/d2-* "${ROOT}"/d3-*; do
  [ -d "${dir}/evidence" ] || continue
  found=1
  cp -R "${dir}/evidence" "${BUNDLE}/$(basename "${dir}")"
done
if [ "${found}" = "0" ]; then
  echo "no scenario evidence found under ${ROOT}" >&2
  exit 1
fi

python3 "${HERE}/scrub_evidence.py" --evidence-root "${BUNDLE}" --secrets "${UNION}"

if command -v gitleaks >/dev/null 2>&1; then
  gitleaks detect --no-git --source "${BUNDLE}" --redact
else
  echo "gitleaks not installed — the gate requires it (brew install gitleaks / see gitleaks.io)" >&2
  exit 1
fi

if [ -n "${PANELLA_EXTRA_SCAN:-}" ]; then
  "${PANELLA_EXTRA_SCAN}" "${BUNDLE}"
fi

echo "gates passed — publishable bundle: ${BUNDLE}"
