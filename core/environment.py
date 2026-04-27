from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.paths import resolve_repo_path

TEMP_DIR_ENV = "SYZABI_TMPDIR"


def temp_dir(*, override: str | None = None, cfg: dict[str, Any] | None = None) -> Path:
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    if cfg is not None:
        from core.paths import PathResolver
        path = PathResolver(cfg).temp_dir()
        path.mkdir(parents=True, exist_ok=True)
        return path
    env_override = os.environ.get(TEMP_DIR_ENV)
    if env_override:
        path = Path(env_override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path("/tmp")


def env_with_temp(base: dict[str, str] | None = None, *, cfg: dict[str, Any] | None = None) -> dict[str, str]:
    env = dict(base) if base is not None else os.environ.copy()
    env["TMPDIR"] = str(temp_dir(cfg=cfg))
    return env


def env_with_go(*, cfg: dict[str, Any]) -> dict[str, str]:
    env = env_with_temp(cfg=cfg)
    go_root = resolve_repo_path(cfg["paths"]["go_root"])
    env["GOROOT"] = str(go_root)
    env["PATH"] = f"{go_root / 'bin'}:{env.get('PATH', '')}"
    return env
