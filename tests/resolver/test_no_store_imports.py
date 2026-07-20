from __future__ import annotations

import ast
from pathlib import Path


def test_resolver_import_graph_contains_no_store_write_or_http_clients() -> None:
    package = Path(__file__).parents[2] / "panella" / "resolver"
    forbidden = ("store", "write", "http", "requests", "urllib", "aiohttp")
    imports: set[str] = set()
    for source in package.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
    assert not [module for module in imports if any(word in module.lower() for word in forbidden)]
