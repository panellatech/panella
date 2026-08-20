"""Production modules must not depend on the resolver before its integration phase."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_only_resolver_package_imports_panella_resolver() -> None:
    violations: list[str] = []
    for path in (ROOT / "panella").rglob("*.py"):
        if path.is_relative_to(ROOT / "panella" / "resolver"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "panella.resolver" or alias.name.startswith("panella.resolver.") for alias in node.names):
                violations.append(str(path.relative_to(ROOT)))
            if isinstance(node, ast.ImportFrom) and node.module and (node.module == "panella.resolver" or node.module.startswith("panella.resolver.")):
                violations.append(str(path.relative_to(ROOT)))
    assert not violations, f"production modules imported panella.resolver early: {sorted(set(violations))}"
