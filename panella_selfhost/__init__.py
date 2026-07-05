"""Self-host packaging entry points for the governed Panella store memory product (Slice-S P3a).

Console-script wrappers only — no governance/memory logic lives here. The serving factory,
store probe, and config render are ``panella``'s contracts; this package exists so a
packaged distribution (wheel / Docker image / ``uvx``) has stable executables:

- ``panella-http``          → :func:`panella_selfhost.serve.main`
- ``panella-render-config`` → :func:`panella_selfhost.render.main`
"""
