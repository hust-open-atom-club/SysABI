from __future__ import annotations

from pathlib import Path

from orchestrator.common import resolve_repo_path


def target_artifact_root() -> Path:
    return resolve_repo_path("artifacts/targets/asterinas")


def target_artifact_path(*parts: str) -> Path:
    return target_artifact_root().joinpath(*parts)


def git_mirror_root() -> Path:
    return target_artifact_path("git-mirrors")


def docker_cargo_home() -> Path:
    return target_artifact_path("docker-cargo-home")


def host_gitconfig_path() -> Path:
    return target_artifact_path("host-gitconfig")


def docker_gitconfig_path() -> Path:
    return target_artifact_path("docker-gitconfig")


def linux_vdso_dir() -> Path:
    return target_artifact_path("linux-vdso")


def host_tools_root() -> Path:
    return target_artifact_path("host-tools")


def host_tools_downloads_dir() -> Path:
    return host_tools_root() / "downloads"


def mtools_root() -> Path:
    return host_tools_root() / "mtools"


def build_probe_root() -> Path:
    return target_artifact_path("build-probe")


def host_target_root() -> Path:
    return target_artifact_path("host-target")
