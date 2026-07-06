"""``python -m panella.cli`` entry — parity with the old single-module form.

The ``__name__`` guard matters: the import-surface test walks and imports every
``panella.*`` module, and an unguarded call would run argparse against pytest's argv.
"""

from panella.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
