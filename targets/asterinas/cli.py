from __future__ import annotations

"""Compatibility dispatch module for the Asterinas target CLI.

The implementation now lives in ``targets.asterinas.runner_impl``.
This module preserves the historical import surface for callers that still
import ``targets.asterinas.cli`` directly.
"""

import importlib
import sys

_impl = importlib.import_module("targets.asterinas.runner_impl")

if __name__ == "__main__":
    _impl.main()
else:
    sys.modules[__name__] = _impl
