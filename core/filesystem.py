from __future__ import annotations

import shutil
from pathlib import Path

from core.paths import resolve_repo_path


def ensure_dir(path: str | Path) -> Path:
    resolved = resolve_repo_path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def clean_dir(path: str | Path) -> Path:
    resolved = resolve_repo_path(path)
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved
