from __future__ import annotations

from orchestrator.common import config, temp_dir as runtime_temp_dir


class RunnerError(RuntimeError):
    pass


def local_tmp_dir():
    try:
        return runtime_temp_dir(config())
    except Exception:
        return runtime_temp_dir()
