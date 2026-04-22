from __future__ import annotations

import os
import subprocess
from core.compat import tomllib
from pathlib import Path

from orchestrator.common import resolve_repo_path
from targets.asterinas.common import RunnerError, local_tmp_dir


def asterinas_rust_toolchain() -> str | None:
    toolchain_path = resolve_repo_path("third_party/asterinas/rust-toolchain.toml")
    if not toolchain_path.exists():
        return None
    payload = tomllib.loads(toolchain_path.read_text(encoding="utf-8"))
    toolchain = payload.get("toolchain", {})
    if not isinstance(toolchain, dict):
        return None
    channel = toolchain.get("channel")
    return str(channel) if channel else None


def cargo_osdk_base_command() -> list[str]:
    command = ["cargo"]
    toolchain = asterinas_rust_toolchain()
    if toolchain:
        command.append(f"+{toolchain}")
    command.append("osdk")
    return command


def cargo_osdk_version() -> str:
    result = subprocess.run(
        [*cargo_osdk_base_command(), "--version"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "TMPDIR": str(local_tmp_dir())},
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ensure_host_osdk(cfg: dict[str, object]) -> None:
    expected = "cargo-osdk 0.17.1"
    if cargo_osdk_version().startswith(expected):
        return
    repo = resolve_repo_path(cfg["asterinas"]["repo_dir"])
    env = {**os.environ, "TMPDIR": str(local_tmp_dir()), "OSDK_LOCAL_DEV": "1"}
    install = subprocess.run(
        ["cargo", "install", "cargo-osdk", "--path", "osdk", "--force"],
        cwd=repo,
        env=env,
        timeout=1800,
        text=True,
        capture_output=True,
        check=False,
    )
    if install.returncode != 0:
        raise RunnerError(install.stderr.strip() or install.stdout.strip() or "failed to install cargo-osdk 0.17.1")


def osdk_build_command(initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> list[str]:
    return [
        *cargo_osdk_base_command(),
        "build",
        "--target-arch=x86_64",
        "--boot-method=grub-rescue-iso",
        "--grub-boot-protocol=linux",
        "--linux-x86-legacy-boot",
        f"--kcmd-args={kcmd_args}",
        "--initramfs",
        str(initramfs_path),
    ]


def osdk_qemu_direct_build_command(initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> list[str]:
    return [
        *cargo_osdk_base_command(),
        "build",
        "--target-arch=x86_64",
        "--boot-method=qemu-direct",
        f"--kcmd-args={kcmd_args}",
        "--initramfs",
        str(initramfs_path),
    ]


def osdk_run_command(initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> list[str]:
    command = [
        *cargo_osdk_base_command(),
        "run",
        "--target-arch=x86_64",
        f"--kcmd-args={kcmd_args}",
        "--initramfs",
        str(initramfs_path),
    ]
    if os.environ.get("SYZABI_ASTERINAS_ENABLE_KVM", "1") != "0":
        command.append('--qemu-args=-accel kvm')
    return command
