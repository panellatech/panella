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


def test_lazy_getattr_exports_resolve():
    # The lazy __getattr__ export shim (__init__.py) is the highest-blast-radius surface: a typo in
    # the _EXPORTS map would silently break `from panella import X`. Assert every advertised name
    # resolves, and that an unknown attribute raises AttributeError (not a masked ImportError).
    for name in panella.__all__:
        assert getattr(panella, name) is not None
    try:
        panella.definitely_not_an_export  # noqa: B018
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown attribute must raise AttributeError")
