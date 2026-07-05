# Contributing to Panella

Thanks for your interest in improving Panella. Contributions are welcome — bug fixes, tests,
docs, and well-scoped features all help.

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

CI (`.github/workflows/ci.yml`) additionally runs a secret scan, builds the wheel, and
verifies the packaged distribution imports and renders its config. Keeping `ruff` and `pytest`
green locally is the fast path to a green PR.

## Code style

Style is enforced by `ruff`, configured in `pyproject.toml` (`[tool.ruff]` /
`[tool.ruff.lint]`): target `py312`, line length 120, with the `E, F, I, N, W, UP, B, A, SIM`
rule sets selected. Let `ruff` be the arbiter — match the existing code rather than
hand-formatting. Prefer small, focused changes; new behavior should come with tests.

## PR flow

1. Fork the repo and create a topic branch off `main`.
2. Make your change, add or update tests, and keep `ruff check .` and `pytest` green.
3. Write a clear PR description: what changed, why, and the risk. Reference any related issue.
4. Open the PR against `main`. CI must pass before review.

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
