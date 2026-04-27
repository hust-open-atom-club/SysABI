#!/usr/bin/env python3
"""Download and cache Alpine Linux kernel + minirootfs per architecture for QEMU Linux reference."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ALPINE_VERSION = "3.23.3"
ALPINE_MIRROR = "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases"

ARCH_TO_ALPINE = {
    "x86_64": "x86_64",
    "riscv64": "riscv64",
    "aarch64": "aarch64",
}


def cache_dir(arch: str) -> Path:
    return Path(ROOT) / "artifacts" / "kernels" / "qemu_linux" / arch


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    print(f"Downloading {url} ...")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response:
        with open(dest, "wb") as f:
            f.write(response.read())
    print(f"Saved to {dest}")


def extract_minirootfs(tar_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if any(dest_dir.iterdir()):
        print(f"Minirootfs already extracted at {dest_dir}")
        return
    print(f"Extracting minirootfs to {dest_dir} ...")
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(path=dest_dir)
    print("Done")


def extract_kernel_from_uboot(uboot_tar: Path, dest_kernel: Path) -> None:
    if dest_kernel.exists():
        print(f"Kernel already cached at {dest_kernel}")
        return
    vmlinuz = dest_kernel.with_name("vmlinuz-lts")
    print(f"Extracting kernel from {uboot_tar} ...")
    dest_kernel.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(uboot_tar, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith("vmlinuz-lts"):
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise RuntimeError(f"Failed to extract {member.name}")
                with open(vmlinuz, "wb") as f:
                    f.write(extracted.read())
                break
        else:
            raise RuntimeError("vmlinuz-lts not found in uboot tarball")
    # Decompress vmlinuz to vmlinux for riscv64 (OpenSBI needs uncompressed)
    print(f"Decompressing {vmlinuz} -> {dest_kernel} ...")
    subprocess.run(["gunzip", "-c", str(vmlinuz)], stdout=open(dest_kernel, "wb"), check=True)
    print(f"Saved kernel to {dest_kernel}")


def prepare_arch(arch: str) -> None:
    alpine_arch = ARCH_TO_ALPINE.get(arch)
    if alpine_arch is None:
        raise ValueError(f"Unsupported arch: {arch}")

    cdir = cache_dir(arch)
    cdir.mkdir(parents=True, exist_ok=True)

    minirootfs_url = f"{ALPINE_MIRROR}/{alpine_arch}/alpine-minirootfs-{ALPINE_VERSION}-{alpine_arch}.tar.gz"
    minirootfs_tar = cdir / f"alpine-minirootfs-{ALPINE_VERSION}-{alpine_arch}.tar.gz"

    uboot_url = f"{ALPINE_MIRROR}/{alpine_arch}/alpine-uboot-{ALPINE_VERSION}-{alpine_arch}.tar.gz"
    uboot_tar = cdir / f"alpine-uboot-{ALPINE_VERSION}-{alpine_arch}.tar.gz"

    fetch(minirootfs_url, minirootfs_tar)
    fetch(uboot_url, uboot_tar)

    extract_minirootfs(minirootfs_tar, cdir / "minirootfs")
    extract_kernel_from_uboot(uboot_tar, cdir / "vmlinux-lts")

    print(f"Assets ready for {arch}:")
    print(f"  Kernel: {cdir / 'vmlinux-lts'}")
    print(f"  Minirootfs: {cdir / 'minirootfs'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", required=True, choices=list(ARCH_TO_ALPINE.keys()))
    args = parser.parse_args()
    prepare_arch(args.arch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
