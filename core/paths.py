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

    def workflow_name(self) -> str:
        return str(self.cfg.get("workflow", "baseline"))

    def target_name(self) -> str:
        return str(self.cfg.get("target", "linux"))

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

    def canonical_target_workflow_root(self) -> Path:
        return resolve_repo_path(f"targets/{self.target_name()}/{self.workflow_name()}")

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
        paths = self._paths()
        if "candidate_initramfs_packages_dir" in paths:
            return self._resolved("candidate_initramfs_packages_dir")
        target = str(self.cfg.get("target", "linux"))
        return resolve_repo_path(f"artifacts/targets/{target}/initramfs-packages")

    def canonical_build_dir(self) -> Path:
        return resolve_repo_path(f"build/targets/{self.target_name()}/{self.workflow_name()}/testcases")

    def canonical_artifacts_dir(self) -> Path:
        return resolve_repo_path(f"artifacts/runs/targets/{self.target_name()}/{self.workflow_name()}")

    def canonical_reports_dir(self) -> Path:
        return resolve_repo_path(f"reports/targets/{self.target_name()}/{self.workflow_name()}")

    def canonical_eligible_root(self) -> Path:
        return resolve_repo_path(f"eligible_programs/targets/{self.target_name()}/{self.workflow_name()}")

    def canonical_eligible_file(self) -> Path:
        return self.canonical_eligible_root() / "default.jsonl"

    def canonical_targets_file(self) -> Path:
        return self.canonical_eligible_root() / "targets.jsonl"

    def canonical_generated_file(self) -> Path:
        return self.canonical_eligible_root() / "generated.jsonl"

    def canonical_static_eligible_file(self) -> Path:
        return self.canonical_eligible_root() / "static.jsonl"

    def canonical_generated_root(self) -> Path:
        return resolve_repo_path(f"artifacts/generated/targets/{self.target_name()}/{self.workflow_name()}")

    def canonical_generated_raw_dir(self) -> Path:
        return self.canonical_generated_root() / "raw"

    def canonical_generated_normalized_dir(self) -> Path:
        return self.canonical_generated_root() / "normalized"

    def canonical_generated_meta_dir(self) -> Path:
        return self.canonical_generated_root() / "meta"

    def canonical_preflight_artifact_dir(self) -> Path:
        return resolve_repo_path(f"artifacts/preflight/targets/{self.target_name()}/{self.workflow_name()}")

    def canonical_build_info_path(self) -> Path:
        return resolve_repo_path(f"artifacts/targets/{self.target_name()}/build-info.json")
