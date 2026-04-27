from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def resolve_compiler_path(compiler: str) -> str | None:
    """Resolve a compiler name to an absolute path.

    Lookup order:
    1. ``PATH`` environment variable (``shutil.which``).
    2. ``SYZABI_<COMPILER>_PATH`` environment variable (e.g.
       ``SYZABI_RISCV64_LINUX_MUSL_GCC_PATH``).
    3. Common home-directory toolchain layout used by musl-cross-make,
       e.g. ``~/toolchains/riscv64-linux-musl-cross/bin/riscv64-linux-musl-gcc``.

    Set ``SYZABI_DISABLE_TOOLCHAIN_FALLBACK=1`` to disable the
    home-directory fallback (useful in tests that verify missing-tool
    behaviour).
    """
    # 1. Already on PATH?
    if found := shutil.which(compiler):
        return found

    # 2. Explicit per-compiler env var
    env_var = f"SYZABI_{compiler.upper().replace('-', '_')}_PATH"
    if env_path := os.environ.get(env_var):
        p = Path(env_path)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)

    # 3. Common musl-cross-make home-directory layout
    if os.environ.get("SYZABI_DISABLE_TOOLCHAIN_FALLBACK") != "1":
        #    e.g. riscv64-linux-musl-gcc -> ~/toolchains/riscv64-linux-musl-cross/bin/riscv64-linux-musl-gcc
        home = Path.home()
        prefix = compiler.rsplit("-", 1)[0]  # strip trailing -gcc / -clang
        candidate = home / "toolchains" / f"{prefix}-cross" / "bin" / compiler
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


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
