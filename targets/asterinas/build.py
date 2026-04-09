from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from orchestrator.common import resolve_repo_path


class BuildConfigError(RuntimeError):
    pass


def build_info_path(cfg: dict[str, Any]) -> Path:
    return resolve_repo_path(cfg["asterinas"]["build_info_path"])


def current_asterinas_revision(cfg: dict[str, Any]) -> str:
    repo = resolve_repo_path(cfg["asterinas"]["repo_dir"])
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "failed to read Asterinas revision"
        raise BuildConfigError(detail)
    return result.stdout.strip()


def ensure_revision(cfg: dict[str, Any]) -> str:
    revision = current_asterinas_revision(cfg)
    expected = str(cfg["asterinas"]["revision"])
    if revision != expected:
        raise BuildConfigError(f"Asterinas revision mismatch: expected {expected}, got {revision}")
    return revision
