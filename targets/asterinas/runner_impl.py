from __future__ import annotations

"""Compatibility shim for historical ``targets.asterinas.runner_impl`` imports.

The legacy import surface now lives in ``targets.asterinas.api``. Keep this file
small so Asterinas execution ownership stays in ``build.py`` / ``runtime.py`` /
``output.py`` plus the thin compatibility/export surface in ``api.py``.
"""

import importlib
import sys

_impl = importlib.import_module("targets.asterinas.api")

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
