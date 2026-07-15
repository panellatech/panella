#!/usr/bin/env bash
# The evidence publish gate. Nothing from the drill bundle (PR bodies included) may be published
# before this passes: a completeness manifest (all three scenarios, all lifecycle phases, union
# coverage for every secret-minting scenario), exact-match scrub of every minted secret, a
# zero-hit re-scan, gitleaks, and an optional extra scanner (PANELLA_EXTRA_SCAN=<executable> —
# it receives the bundle dir as $1).
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

# Completeness manifest: the gate covers a full D-1/D-2/D-3 protocol run, not whatever subset
# happens to be on disk. A missing scenario kind or a missing phase file fails the gate.
python3 - "${ROOT}" "${UNION}" <<'PY'
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
union_rows = [line.split("\t", 1)[0] for line in open(sys.argv[2]) if line.strip()]
required_phases = {
    "d1": ["pre.txt", "postup.txt", "preteardown.txt", "teardown.txt", "post.txt"],
    "d2": ["pre.txt", "postup.txt", "preteardown.txt", "teardown.txt", "post.txt"],
    "d3": ["pre.txt", "preteardown.txt", "teardown.txt", "post.txt"],
}
problems = []
for kind, phases in required_phases.items():
    dirs = sorted(p for p in root.glob(f"{kind}-*") if (p / "evidence").is_dir())
    if not dirs:
        problems.append(f"no {kind} scenario evidence present")
        continue
    # The scrub union is keyed by scenario KIND (d1-owner-bearer, ...), not by directory, so it
    # cannot distinguish two runs of the same kind. If a stale d1-* dir lingers while the union was
    # regenerated for the latest run only, the copy step below would publish the stale dir scrubbed
    # against a union that lacks its minted secrets — a raw credential could survive the gate. Refuse
    # multiple dirs per kind: keep only the current run's evidence before gating.
    if len(dirs) > 1:
        problems.append(
            f"{kind}: {len(dirs)} scenario dirs present ({[d.name for d in dirs]}) — the union is "
            f"keyed by scenario kind, not directory; remove stale runs and keep only the current one"
        )
        continue
    for d in dirs:
        for phase in phases:
            fpath = d / "evidence" / phase
            if not fpath.is_file():
                problems.append(f"{d.name}: missing evidence phase {phase}")
                continue
            # is_file() alone accepts a FAILED phase: evidence.sh/teardown.sh write the phase file
            # BEFORE their checks run, so a phase that exits non-zero (broken claude CLI, leftover
            # resources, set -e mid-write) leaves a file with NO failure marker at all. Require the
            # POSITIVE sentinel that each phase appends only after every check for it passed
            # ("PHASE_OK <phase>") — a failed or partial phase never reaches it, so it can never be
            # scrubbed and published as a clean bundle.
            body = fpath.read_text()
            phase_name = phase[:-4] if phase.endswith(".txt") else phase
            if f"PHASE_OK {phase_name}" not in body:
                problems.append(f"{d.name}: evidence phase {phase} lacks its success sentinel (failed/partial run)")
            failed = [ln.rstrip() for ln in body.splitlines() if ln.startswith("FAIL")]
            if failed:
                problems.append(f"{d.name}: evidence phase {phase} carries failure marker(s): {failed[:2]}")
for kind in ("d1", "d2"):
    problems.extend(
        f"union missing label {kind}-{suffix}"
        for suffix in ("owner-bearer", "approval-token", "api-key")
        if f"{kind}-{suffix}" not in union_rows
    )
if problems:
    print("gate manifest FAILED:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    sys.exit(1)
print("gate manifest: all scenarios present with full phase evidence + union coverage")
PY

rm -rf "${BUNDLE}"
mkdir -p "${BUNDLE}"
for dir in "${ROOT}"/d1-* "${ROOT}"/d2-* "${ROOT}"/d3-*; do
  [ -d "${dir}/evidence" ] || continue
  cp -R "${dir}/evidence" "${BUNDLE}/$(basename "${dir}")"
done

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
