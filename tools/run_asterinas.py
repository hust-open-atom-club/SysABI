#!/usr/bin/env python3
from __future__ import annotations

"""Compatibility module for the historical tools/run_asterinas.py entrypoint.

The implementation lives in ``targets.asterinas.api``. This file preserves:
- direct script execution: ``python3 tools/run_asterinas.py ...``
- legacy imports/patch targets: ``import tools.run_asterinas``
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_impl = importlib.import_module("targets.asterinas.api")

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
