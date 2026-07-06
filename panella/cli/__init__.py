"""Top-level ``panella`` CLI.

Each command family lives in its own module under ``panella.cli`` and exposes a
``register(subparsers)`` hook; ``build_parser`` stitches them together in
``_COMMAND_MODULES`` order. Adding a command family = one new module + one entry
in ``_COMMAND_MODULES`` — existing command modules are never edited, so parallel
feature branches don't collide here.
"""

from __future__ import annotations

import argparse

from panella.cli import connect, init, lifecycle, tokens
from panella.cli import approvals, audit, memories, stats

# Command modules in help-display order. Each exposes register(subparsers) -> None.
_COMMAND_MODULES = (tokens, init, connect, lifecycle, approvals, memories, audit, stats)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="panella",
        description="Panella operator utilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in _COMMAND_MODULES:
        module.register(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
