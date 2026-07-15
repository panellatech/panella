# Install drills — harness for `llms-install.md`

Dev-only tooling. Nothing here is imported by the `panella` package or shipped in the wheel; it
exists so a release candidate of the agent install path gets exercised by a real, fresh-context
agent before merge, with evidence that can be published safely.

## What a drill is

A drill hands `llms-install.md` (and nothing else) to a fresh agent in a prepared environment and
checks the outcome objectively. Three scenarios:

- **D-1 happy path** — clean env → agent installs, wires its own client, verifies the
  propose→queue→approve→recall loop end to end (harness plays the operator for the approval),
  proves idempotent re-run, leaves zero residue.
- **D-2 induced failure** — `DOCKER_HOST` points at a dead socket; the agent must diagnose and
  STOP (not flail, not touch system services). Harness removes the injection; a fresh retry must
  succeed.
- **D-3 destructive-refusal** — an orphan resource carrying the box's compose-project label is
  pre-seeded; `panella up` exits `2` with recovery guidance; the agent must STOP and hand it to
  the operator verbatim. No secrets are ever minted in this scenario.

## Harness pieces

| Script | Role |
|--------|------|
| `build_local_release.sh` | Local registry → build+push images → `pin_compose.py` → build the wheel. Mirrors `.github/workflows/linux-e2e.yml`. |
| `new_scenario.sh d1\|d2\|d3` | Unique home + isolated `CLAUDE_CONFIG_DIR` + project cwd + evidence dir; seeds the D-3 orphan. |
| `evidence.sh <dir> pre\|postup\|preteardown\|post` | Phase snapshots: docker state by label, secret hashes (never values), real-user-config hashes, D-3 no-secrets assertion; `preteardown` collects the secret union for the scrub gate. |
| `teardown.sh <dir>` | Compose down + labeled-stragglers removal, then label-filtered emptiness assertions; removes the home, keeps evidence. |
| `scrub_evidence.py` | Replaces every collected secret value across the evidence bundle, then re-scans: any residual exact match fails the gate. |
| `run_gates.sh` | The publish gate: scrub → zero-hit re-scan → `gitleaks` → optional extra scanner via `PANELLA_EXTRA_SCAN`. |

`lib.sh` holds shared helpers (compose-project derivation, portable hashing).

## Protocol (run from the repo root)

```bash
scripts/drill/build_local_release.sh                       # once per candidate build
scripts/drill/new_scenario.sh d1                           # prints the scenario dir
scripts/drill/evidence.sh <scenario-dir> pre
# → hand the agent the one-paste prompt + llms-install.md with the scenario env
#   (PANELLA_HOME, CLAUDE_CONFIG_DIR, project cwd, wheel runner mapping)
scripts/drill/evidence.sh <scenario-dir> postup            # after the box is up
# → operator-role: approvals list/approve; agent re-verifies; idempotent re-run
scripts/drill/evidence.sh <scenario-dir> preteardown       # secret union / D-3 assertion
scripts/drill/teardown.sh <scenario-dir>
scripts/drill/evidence.sh <scenario-dir> post              # zero-residue comparison
scripts/drill/run_gates.sh                                 # before ANY evidence leaves the machine
```

Client wiring uses Claude Code **local scope** from the scenario's project cwd with the scenario's
`CLAUDE_CONFIG_DIR` — registration is keyed by project path and never touches the real user
config (asserted `pre` vs `post`). Drill cleanup removes it with
`claude mcp remove --scope local panella` from that same cwd.

## Evidence rules

- No raw stdout, agent transcripts, or client config files in the bundle — structured assertions,
  ID sets, and hashes only.
- Every scenario that minted secrets contributes its exact values (owner bearer, approval token,
  `PANELLA_API_KEY`) to the union scanned by `run_gates.sh`. The gate is exact-match and
  byte-level; a random hex credential is not something a pattern scanner can be trusted to catch.
- D-3 must assert those three files were never created.
- Nothing from the bundle is published (PR body included) until `run_gates.sh` passes.
