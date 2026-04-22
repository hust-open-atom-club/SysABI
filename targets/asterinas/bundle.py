from __future__ import annotations

import hashlib
import json
import shutil
from core.compat import tomllib
from pathlib import Path

from orchestrator.common import resolve_repo_path
from targets.asterinas.build import build_info_path, ensure_revision
from targets.asterinas.common import RunnerError
from targets.asterinas.initramfs import create_minimal_initramfs
from targets.asterinas import paths as path_mod


def packaged_bundle_metadata_path(package_dir: Path) -> Path:
    return package_dir / ".osdk-build.meta.json"


def packaged_bundle_metadata(cfg: dict[str, object], initramfs_path: Path, *, kcmd_args: str) -> dict[str, object]:
    return {
        "docker_image": str(cfg["asterinas"]["docker_image"]),
        "initramfs_sha256": hashlib.sha256(initramfs_path.read_bytes()).hexdigest(),
        "kcmd_args": kcmd_args,
        "revision": ensure_revision(cfg),
    }


def packaged_bundle_metadata_matches(metadata_path: Path, expected: dict[str, object]) -> bool:
    if not metadata_path.exists():
        return False
    try:
        actual = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return actual == expected


def target_osdk_dir(cfg: dict[str, object]) -> Path:
    info_path = build_info_path(cfg)
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        target_dir = info.get("target_dir")
        if target_dir:
            return Path(str(target_dir))
    return resolve_repo_path("third_party/asterinas/target/osdk")


def built_bundle_dir(cfg: dict[str, object]) -> Path:
    return target_osdk_dir(cfg) / "aster-kernel"


def build_probe_root() -> Path:
    root = path_mod.build_probe_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_probe_initramfs(cfg: dict[str, object]) -> Path:
    probe_root = build_probe_root()
    shutil.copy2("/usr/bin/true", probe_root / "probe.bin")
    return create_minimal_initramfs(cfg, probe_root / "probe.bin", probe_root)


def load_bundle_manifest(cfg: dict[str, object]) -> dict[str, object]:
    manifest_path = built_bundle_dir(cfg) / "bundle.toml"
    if not manifest_path.exists():
        raise RunnerError(f"missing OSDK bundle manifest: {manifest_path}")
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def load_external_bundle_manifest(bundle_dir: Path) -> dict[str, object]:
    manifest_path = bundle_dir / "bundle.toml"
    if not manifest_path.exists():
        raise RunnerError(f"missing OSDK bundle manifest: {manifest_path}")
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def shared_bzimage_path(cfg: dict[str, object]) -> Path:
    manifest = load_bundle_manifest(cfg)
    aster_bin = manifest.get("aster_bin")
    if not isinstance(aster_bin, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing aster_bin section")
    kernel_relpath = aster_bin.get("path")
    if not isinstance(kernel_relpath, str):
        raise RunnerError("invalid OSDK bundle manifest: missing aster_bin.path")
    return built_bundle_dir(cfg) / kernel_relpath


def bundle_kcmdline(manifest: dict[str, object]) -> str:
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing run section")
    boot_section = run_section.get("boot")
    if not isinstance(boot_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing boot section")
    kcmdline = boot_section.get("kcmdline")
    if not isinstance(kcmdline, list):
        raise RunnerError("invalid OSDK bundle manifest: missing kcmdline")
    return " ".join(str(part) for part in kcmdline)


def bundle_qemu_path(manifest: dict[str, object]) -> str:
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing run section")
    qemu_section = run_section.get("qemu")
    if not isinstance(qemu_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing qemu section")
    path = qemu_section.get("path")
    if not isinstance(path, str) or not path:
        raise RunnerError("invalid OSDK bundle manifest: missing qemu path")
    return path


def bundle_grub_iso_path(bundle_dir: Path, manifest: dict[str, object]) -> Path:
    vm_image = manifest.get("vm_image")
    if not isinstance(vm_image, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing vm_image")
    image_relpath = vm_image.get("path")
    if not isinstance(image_relpath, str) or not image_relpath:
        raise RunnerError("invalid OSDK bundle manifest: missing vm_image.path")
    return bundle_dir / image_relpath


def kernel_build_ready(cfg: dict[str, object]) -> bool:
    try:
        load_bundle_manifest(cfg)
    except RunnerError:
        return False
    image_path = shared_bzimage_path(cfg)
    return image_path.exists() and image_path.stat().st_size > 1024


def build_lock_path(cfg: dict[str, object]) -> Path:
    info_path = build_info_path(cfg)
    return info_path.with_name(f"{info_path.name}.lock")
