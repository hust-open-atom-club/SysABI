from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def repo_root() -> Path:
    return ROOT


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


@dataclass(frozen=True, slots=True)
class PathResolver:
    cfg: dict[str, Any]

    def _paths(self) -> dict[str, Any]:
        paths = self.cfg.get("paths", {})
        if not isinstance(paths, dict):
            return {}
        return paths

    def _resolved(self, key: str, default: str | Path | None = None) -> Path:
        value = self._paths().get(key, default)
        if value is None:
            raise KeyError(f"missing path config for {key}")
        return resolve_repo_path(value)

    def temp_dir(self) -> Path:
        return self._resolved("temp_dir", "artifacts/tmp")

    def build_dir(self) -> Path:
        return self._resolved("build_dir")

    def artifacts_dir(self) -> Path:
        return self._resolved("artifacts_dir")

    def reports_dir(self) -> Path:
        return self._resolved("reports_dir")

    def eligible_file(self) -> Path:
        return self._resolved("eligible_file")

    def runner_profiles_path(self) -> Path:
        return resolve_repo_path(self.cfg.get("runner_profiles_path", "configs/runner_profiles.json"))

    def candidate_initramfs_packages_dir(self) -> Path:
        return self._resolved("candidate_initramfs_packages_dir", "artifacts/asterinas/initramfs-packages")

