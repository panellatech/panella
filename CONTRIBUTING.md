# Contributing to Panella

Thanks for your interest in improving Panella. Contributions are welcome — bug fixes, tests,
docs, and well-scoped features all help.

## Commit identity (required — read this first)

Panella keeps a single, neutral maintainer identity across its entire git history, and **CI
enforces it**: `scripts/ci/check-git-identity.sh` scans every commit reachable from a pushed
branch and every commit in a pull request. Before your first commit, set your identity **in this
repository** to the project identity:

```bash
git config user.name "Panella Maintainers"
git config user.email "noreply@panella.tech"
```

(Setting it without `--global` scopes it to this checkout, so it won't affect your other repos.)

Two rules follow from this:

- **Every commit** you push — on a branch or in a PR — must carry this author and committer. A
  commit under a personal name or email turns CI red.
- **Never merge through the GitHub web UI or `gh pr merge`.** A UI merge or squash stamps the
  pressing user (or `GitHub <noreply@github.com>`) into history, which fails the same gate.
  Merges land on `main` locally, under the neutral identity, and are pushed.

This is deliberate: Panella's history is a collective project record, not a per-author changelog.
Attribution and discussion still live in the pull request itself.

## Public by design

Treat everything in this repository — code, commit messages, PR descriptions, and issues — as
**permanently public**. Never put real names, personal emails, secrets, tokens, internal
hostnames, or private URLs into a commit or a comment. Write the message first, then re-read it as
if it were already on the public internet, before you push.

## Dev setup

Panella targets Python 3.12+. From a checkout:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

The `[dev]` extra pulls in the test and lint toolchain (`pytest`, `pytest-asyncio`, `ruff`,
`build`).

## Lint and test

Before you push, run the same checks CI runs:

```bash
ruff check .
pytest
```

CI (`.github/workflows/ci.yml`) additionally runs the commit-identity gate, a secret scan, builds
the wheel, and verifies the packaged distribution imports and renders its config. Keeping `ruff`
and `pytest` green locally is the fast path to a green PR.

## Code style

Style is enforced by `ruff`, configured in `pyproject.toml` (`[tool.ruff]` /
`[tool.ruff.lint]`): target `py312`, line length 120, with the `E, F, I, N, W, UP, B, A, SIM`
rule sets selected. Let `ruff` be the arbiter — match the existing code rather than
hand-formatting. Prefer small, focused changes; new behavior should come with tests.

## PR flow

Start every change from an up-to-date `main`, on a topic branch, with the commit identity set as
above.

**If you have push access to this repository:**

1. Branch off `main`: `git switch -c topic/your-change`.
2. Make your change, add or update tests, and keep `ruff check .` and `pytest` green.
3. Push the branch and open a PR against `main`. CI must pass before review.

**If you don't have push access:** fork the repository, then follow the same steps from your fork.

In both cases, write a clear PR description — what changed, why, and the risk — and reference any
related issue. Open the PR against `main`.

For anything touching the governance, approval, or serving surfaces, please read
`docs/GOVERNANCE.md` and `SECURITY.md` first — those areas are fail-closed by design, and a
change that relaxes a default needs an explicit rationale.

## Release checks

The maintainers run additional private release and content-hygiene checks on contributions
before they ship. These run on the maintainer side and are not something a contributor needs
to set up or invoke; they exist so that the public release stays clean. Just send your change
with clear intent and passing CI, and we will take it from there.

## Reporting security issues

Do not open a public issue for a suspected vulnerability. See `SECURITY.md` for private
reporting.
