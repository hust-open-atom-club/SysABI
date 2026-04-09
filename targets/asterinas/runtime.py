from __future__ import annotations

from typing import Any


SUPPORTED_MODES = ("unconfigured", "local-proxy", "host-direct", "docker-qemu")


def selected_run_timeout_sec(cfg: dict[str, Any], *, batch_timeout_env: str | None = None) -> int:
    if batch_timeout_env:
        return int(batch_timeout_env)
    return int(cfg["asterinas"]["run_timeout_sec"])
