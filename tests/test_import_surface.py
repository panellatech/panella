from __future__ import annotations

import importlib
import pkgutil

import panella


def test_all_public_modules_import_without_embedding_deps():
    excluded = {"panella.eval", "panella.atomize", "panella.embed_cache", "panella.embed_proxy", "panella.embedder"}
    for mod in pkgutil.walk_packages(panella.__path__, panella.__name__ + "."):
        if mod.name in excluded:
            continue
        importlib.import_module(mod.name)
