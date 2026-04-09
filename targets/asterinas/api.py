#!/usr/bin/env python3
from __future__ import annotations

"""Thin compatibility export surface for historical Asterinas runner imports.

Canonical target-owned execution now lives under:
- ``targets/asterinas/entrypoint.py``
- ``targets/asterinas/adapter.py``
- ``targets/asterinas/build.py``
- ``targets/asterinas/runtime.py``
- ``targets/asterinas/output.py``

This module stays as a backward-compatible import/patch surface for old callers
and tests. Its implementation delegates into narrowly owned support modules.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import (
    config,
    configure_runtime,
    dump_json,
    repo_root,
    resolve_repo_path,
    sha256_text,
    temp_dir as runtime_temp_dir,
)
from targets.asterinas import build as build_mod
from targets.asterinas import cache_support, runtime_support
from targets.asterinas import runtime as runtime_mod
from targets.asterinas.build import build_info_path, ensure_revision
from targets.asterinas.bundle import (
    build_lock_path,
    build_probe_initramfs,
    build_probe_root,
    built_bundle_dir,
    bundle_grub_iso_path,
    bundle_kcmdline,
    bundle_qemu_path,
    kernel_build_ready,
    load_bundle_manifest,
    load_external_bundle_manifest,
    shared_bzimage_path,
)
from targets.asterinas.common import RunnerError
from targets.asterinas.initramfs import (
    compose_autorun,
    compose_init,
    compose_init_hook,
    compose_packaged_autorun,
    compose_profile,
    create_batch_initramfs,
    create_minimal_initramfs,
    ensure_packaged_initramfs,
)
from targets.asterinas.osdk import (
    asterinas_rust_toolchain,
    cargo_osdk_base_command,
    cargo_osdk_version,
    ensure_host_osdk,
    osdk_build_command,
    osdk_qemu_direct_build_command,
    osdk_run_command,
)
from targets.asterinas.output import guest_crash_detail, write_missing_marker_crash_result as write_missing_marker_crash_result_impl
from targets.asterinas.runtime import selected_run_timeout_sec
from tools.run_asterinas_shared import (
    candidate_status_from_events,
    compose_batch_autorun,
    extract_batch_case_blocks,
    extract_section,
    materialize_batch_case_outputs,
    parse_batch_case_results,
    parse_events,
    parse_external_state,
    parse_process_exit,
    shared_package_bundle_dir,
    shared_package_runtime_dirs,
)


MARKER_PREFIX = "__SYZABI"
NETWORK_PORT_ENV_NAMES = (
    "SSH_PORT",
    "NGINX_PORT",
    "REDIS_PORT",
    "IPERF_PORT",
    "LMBENCH_TCP_LAT_PORT",
    "LMBENCH_TCP_BW_PORT",
    "MEMCACHED_PORT",
)
GUEST_ENV_HEADER_MAGIC = "SYZABI_ENV_V1"
GUEST_ENV_HEADER_SIZE = 1024

ASTERINAS_GIT_MIRRORS = {
    "inherit-methods-macro": "https://github.com/asterinas/inherit-methods-macro",
    "inventory": "https://github.com/asterinas/inventory",
    "rust-ctor": "https://github.com/asterinas/rust-ctor",
    "smoltcp": "https://github.com/asterinas/smoltcp",
}


def local_tmp_dir() -> Path:
    try:
        return runtime_temp_dir(config())
    except Exception:
        return runtime_temp_dir()


def is_docker_access_error(detail: str) -> bool:
    normalized = detail.lower()
    return (
        "permission denied while trying to connect to the docker daemon socket" in normalized
        or "dial unix /var/run/docker.sock" in normalized
        or "cannot connect to the docker daemon" in normalized
        or "no such command: `osdk`" in normalized
        or "failed to compile `cargo-osdk" in normalized
        or "static.rust-lang.org" in normalized
    )


def should_fallback_to_host_direct(exc: RunnerError) -> bool:
    return is_docker_access_error(str(exc))


def write_missing_marker_crash_result(
    *,
    console_text: str,
    raw_trace_path: Path,
    external_state_path: Path,
    kernel_build: str,
) -> bool:
    return write_missing_marker_crash_result_impl(
        console_text=console_text,
        raw_trace_path=raw_trace_path,
        external_state_path=external_state_path,
        kernel_build=kernel_build,
        hooks=sys.modules[__name__],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--batch-manifest")
    parser.add_argument("--work-dir")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["unconfigured", "local-proxy", "host-direct", "docker-qemu"],
        default=os.environ.get("SYZABI_ASTERINAS_MODE", "docker-qemu"),
    )
    return parser.parse_args()


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def required_env_path(name: str) -> Path:
    path = env_path(name)
    if path is None:
        raise RunnerError(f"missing required environment path: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def runner_result_path() -> Path | None:
    return env_path("SYZABI_RUNNER_RESULT_PATH")


def write_runner_result(payload: dict[str, object]) -> None:
    path = runner_result_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)


def read_workflow_config() -> dict[str, object]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "asterinas")
    configure_runtime(workflow=workflow)
    cfg = config()
    if "asterinas" not in cfg:
        raise RunnerError(f"run_asterinas.py requires an asterinas-capable config, got {cfg.get('workflow')}")
    return cfg


def docker_repo_dir(cfg: dict[str, object]) -> Path:
    return Path(str(cfg["asterinas"].get("docker_repo_dir", "/root/asterinas")))


def docker_workspace_dir(cfg: dict[str, object]) -> Path:
    return Path(str(cfg["asterinas"].get("docker_workspace_dir", "/workspace")))


def asterinas_git_mirror_root() -> Path:
    return cache_support.asterinas_git_mirror_root(hooks=sys.modules[__name__])


def ensure_git_mirror(name: str, remote_url: str) -> Path:
    return cache_support.ensure_git_mirror(name, remote_url, hooks=sys.modules[__name__])


def ensure_asterinas_git_mirrors() -> dict[str, Path]:
    return cache_support.ensure_asterinas_git_mirrors(hooks=sys.modules[__name__])


def existing_asterinas_git_mirrors() -> dict[str, Path]:
    return cache_support.existing_asterinas_git_mirrors(hooks=sys.modules[__name__])


def docker_cargo_home() -> Path:
    return cache_support.docker_cargo_home(hooks=sys.modules[__name__])


def shared_cargo_osdk_path() -> Path:
    return cache_support.shared_cargo_osdk_path(hooks=sys.modules[__name__])


def container_cargo_home(cfg: dict[str, object]) -> Path:
    return cache_support.container_cargo_home(cfg, hooks=sys.modules[__name__])


def ensure_docker_cargo_cache_dirs() -> tuple[Path, Path]:
    return cache_support.ensure_docker_cargo_cache_dirs(hooks=sys.modules[__name__])


def prepare_run_cargo_home(work_dir: Path) -> Path:
    return cache_support.prepare_run_cargo_home(work_dir, hooks=sys.modules[__name__])


def ensure_shared_package_cargo_home(package_dir: Path, *, refresh: bool = False) -> Path:
    return cache_support.ensure_shared_package_cargo_home(package_dir, hooks=sys.modules[__name__], refresh=refresh)


def prime_docker_cargo_cache(cfg: dict[str, object]) -> None:
    return cache_support.prime_docker_cargo_cache(cfg, hooks=sys.modules[__name__])


def gitconfig_lines(cfg: dict[str, object], *, path_transform, ensure_mirrors: bool) -> list[str]:
    return cache_support.gitconfig_lines(
        cfg,
        hooks=sys.modules[__name__],
        path_transform=path_transform,
        ensure_mirrors=ensure_mirrors,
    )


def prepare_host_gitconfig(cfg: dict[str, object]) -> Path:
    return cache_support.prepare_host_gitconfig(cfg, hooks=sys.modules[__name__])


def prepare_docker_gitconfig(cfg: dict[str, object]) -> Path:
    return cache_support.prepare_docker_gitconfig(cfg, hooks=sys.modules[__name__])


def host_path_to_container_path(path: Path, cfg: dict[str, object]) -> Path:
    return cache_support.host_path_to_container_path(path, cfg, hooks=sys.modules[__name__])


def docker_env_options(extra_env: dict[str, str] | None = None) -> list[str]:
    return cache_support.docker_env_options(extra_env)


def docker_run_command(
    cfg: dict[str, object],
    script: str,
    *,
    extra_env: dict[str, str] | None = None,
    workdir: Path | None = None,
    container_name: str | None = None,
) -> list[str]:
    return cache_support.docker_run_command(
        cfg,
        script,
        hooks=sys.modules[__name__],
        extra_env=extra_env,
        workdir=workdir,
        container_name=container_name,
    )


def docker_make_kernel_command(cfg: dict[str, object]) -> list[str]:
    return cache_support.docker_make_kernel_command(cfg, hooks=sys.modules[__name__])


def sanitize_container_component(value: str) -> str:
    return cache_support.sanitize_container_component(value)


def container_name_for_run(program_id: str, run_id: str) -> str:
    return cache_support.container_name_for_run(program_id, run_id)


def force_remove_container(container_name: str) -> None:
    return cache_support.force_remove_container(container_name, hooks=sys.modules[__name__])


def ensure_vdso_dir() -> Path:
    return runtime_support.ensure_vdso_dir(hooks=sys.modules[__name__])


def ensure_dummy_block_images(cfg: dict[str, object]) -> None:
    return runtime_support.ensure_dummy_block_images(cfg, hooks=sys.modules[__name__])


def ensure_local_mtools() -> Path | None:
    return runtime_support.ensure_local_mtools(hooks=sys.modules[__name__])


def selected_guest_cmdline_append() -> str:
    return runtime_support.selected_guest_cmdline_append(hooks=sys.modules[__name__])


def guest_env_lines() -> list[str]:
    return runtime_support.guest_env_lines(hooks=sys.modules[__name__])


def guest_env_header_bytes(lines: list[str]) -> bytes:
    return runtime_support.guest_env_header_bytes(lines, hooks=sys.modules[__name__])


def materialize_guest_env_file(ext2_image: Path) -> None:
    return runtime_support.materialize_guest_env_file(ext2_image, hooks=sys.modules[__name__])


def docker_osdk_build_script(cfg: dict[str, object], work_dir: Path, initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> str:
    return cache_support.docker_osdk_build_script(
        cfg,
        work_dir,
        initramfs_path,
        hooks=sys.modules[__name__],
        kcmd_args=kcmd_args,
    )


def ensure_packaged_docker_bundle(cfg: dict[str, object], package_dir: Path, initramfs_path: Path, *, kcmd_args: str) -> None:
    return cache_support.ensure_packaged_docker_bundle(
        cfg,
        package_dir,
        initramfs_path,
        hooks=sys.modules[__name__],
        kcmd_args=kcmd_args,
    )


def selected_initramfs(cfg: dict[str, object], binary_path: Path, work_dir: Path) -> Path:
    return runtime_support.selected_initramfs(cfg, binary_path, work_dir, hooks=sys.modules[__name__])


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


def qemu_log_paths(work_dir: Path) -> tuple[Path, Path]:
    return runtime_support.qemu_log_paths(work_dir)


def read_console_text(*paths: Path) -> str:
    return runtime_support.read_console_text(*paths)


def stop_process(process) -> None:
    return runtime_support.stop_process(process, hooks=sys.modules[__name__])


def matching_qemu_pids(work_dir: Path) -> list[int]:
    return runtime_support.matching_qemu_pids(work_dir, hooks=sys.modules[__name__])


def stop_qemu_processes(work_dir: Path) -> None:
    return runtime_support.stop_qemu_processes(work_dir, hooks=sys.modules[__name__])


def host_osdk_env(work_dir: Path, *, boot_method: str = "qemu-direct") -> dict[str, str]:
    return runtime_support.host_osdk_env(work_dir, hooks=sys.modules[__name__], boot_method=boot_method)


def reserve_tcp_port():
    return runtime_support.reserve_tcp_port()


def reserve_qemu_ports() -> tuple[list[object], dict[str, int]]:
    return runtime_support.reserve_qemu_ports(hooks=sys.modules[__name__])


def release_reserved_ports(sockets: list[object]) -> None:
    return runtime_support.release_reserved_ports(sockets)


def system_ovmf_code_path() -> Path:
    return runtime_support.system_ovmf_code_path(hooks=sys.modules[__name__])


def prepare_ovmf_vars(work_dir: Path) -> Path:
    return runtime_support.prepare_ovmf_vars(work_dir, hooks=sys.modules[__name__])


def prepare_run_block_images(cfg: dict[str, object], work_dir: Path) -> tuple[Path, Path]:
    return runtime_support.prepare_run_block_images(cfg, work_dir, hooks=sys.modules[__name__])


def ensure_host_build(cfg: dict[str, object]) -> str:
    return build_mod.ensure_host_build(cfg, hooks=sys.modules[__name__])


def ensure_docker_build(cfg: dict[str, object]) -> str:
    return build_mod.ensure_docker_build(cfg, hooks=sys.modules[__name__])


def qemu_args_tokens(cfg: dict[str, object], env: dict[str, str]) -> list[str]:
    return runtime_support.qemu_args_tokens(cfg, env, hooks=sys.modules[__name__])


def kvm_accessible() -> bool:
    return runtime_support.kvm_accessible(hooks=sys.modules[__name__])


def kvm_enabled(env: dict[str, str]) -> bool:
    return runtime_support.kvm_enabled(env)


def qemu_direct_command(cfg: dict[str, object], initramfs_path: Path, env: dict[str, str]) -> tuple[list[str], Path]:
    return runtime_support.qemu_direct_command(cfg, initramfs_path, env, hooks=sys.modules[__name__])


def grub_iso_qemu_command(cfg: dict[str, object], bundle_dir: Path, env: dict[str, str]) -> tuple[list[str], Path]:
    return runtime_support.grub_iso_qemu_command(cfg, bundle_dir, env, hooks=sys.modules[__name__])


def container_ovmf_code_path() -> str:
    return runtime_support.container_ovmf_code_path()


def container_ovmf_vars_seed_path() -> str:
    return runtime_support.container_ovmf_vars_seed_path()


def docker_run_env(cfg: dict[str, object], work_dir: Path) -> dict[str, str]:
    return runtime_support.docker_run_env(cfg, work_dir, hooks=sys.modules[__name__])


def containerized_qemu_direct_command(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    guest_kcmd_args: str = "",
) -> list[str]:
    return runtime_mod.containerized_qemu_direct_command(
        cfg,
        work_dir,
        initramfs_path,
        guest_kcmd_args=guest_kcmd_args,
        hooks=sys.modules[__name__],
    )


def docker_qemu_direct_script(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    guest_kcmd_args: str = "",
) -> str:
    return runtime_mod.docker_qemu_direct_script(
        cfg,
        work_dir,
        initramfs_path,
        guest_kcmd_args=guest_kcmd_args,
        hooks=sys.modules[__name__],
    )


def docker_osdk_run_script(cfg: dict[str, object], work_dir: Path, initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> str:
    return runtime_mod.docker_osdk_run_script(
        cfg,
        work_dir,
        initramfs_path,
        kcmd_args=kcmd_args,
        hooks=sys.modules[__name__],
    )


def containerized_grub_iso_command(cfg: dict[str, object], package_dir: Path, work_dir: Path) -> list[str]:
    return runtime_mod.containerized_grub_iso_command(cfg, package_dir, work_dir, hooks=sys.modules[__name__])


def host_grub_bundle_command(cfg: dict[str, object], package_dir: Path, work_dir: Path) -> tuple[list[str], Path]:
    return runtime_mod.host_grub_bundle_command(cfg, package_dir, work_dir, hooks=sys.modules[__name__])


def docker_grub_bundle_script(cfg: dict[str, object], package_dir: Path, work_dir: Path) -> str:
    return runtime_mod.docker_grub_bundle_script(cfg, package_dir, work_dir, hooks=sys.modules[__name__])


def load_batch_manifest(path: Path) -> list[dict[str, object]]:
    return runtime_mod.load_batch_manifest(path)


def docker_qemu_batch_run(args: argparse.Namespace) -> None:
    return runtime_mod.docker_qemu_batch_run(args)


def local_proxy(args: argparse.Namespace) -> None:
    return runtime_mod.local_proxy(args, hooks=sys.modules[__name__])


def host_direct_run(args: argparse.Namespace) -> None:
    return runtime_mod.host_direct_run(args, hooks=sys.modules[__name__])


def docker_qemu_run(args: argparse.Namespace) -> None:
    return runtime_mod.docker_qemu_run(args, hooks=sys.modules[__name__])


def main() -> None:
    from targets.asterinas.adapter import AsterinasTargetAdapter

    args = parse_args()
    adapter = AsterinasTargetAdapter()
    try:
        if args.healthcheck:
            adapter.healthcheck(args)
            return
        if args.batch_manifest:
            adapter.run_batch(args)
            return
        adapter.run_case(args)
    except subprocess.TimeoutExpired as exc:
        write_runner_result(
            {
                "status": "timeout",
                "exit_code": None,
                "status_detail": f"runner timed out after {exc.timeout} seconds",
                "kernel_build": "asterinas-timeout",
            }
        )
        raise SystemExit("asterinas runner timed out")
    except RunnerError as exc:
        write_runner_result(
            {
                "status": "infra_error",
                "exit_code": None,
                "status_detail": str(exc),
                "kernel_build": "asterinas-unconfigured",
            }
        )
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
