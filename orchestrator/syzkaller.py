from __future__ import annotations

import json
import subprocess
from pathlib import Path

from orchestrator.common import config, env_with_go, resolve_repo_path


def syzkaller_dir() -> Path:
    cfg = config()
    return resolve_repo_path(cfg["paths"]["syzkaller_dir"])


def syzkaller_bin(name: str) -> Path:
    return syzkaller_dir() / "bin" / name


def project_bin(name: str) -> Path:
    return resolve_repo_path("build/bin") / name


def ensure_binary(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"required binary is missing: {path}")


def inspect_program(program_path: Path, strict: bool = False) -> dict[str, object]:
    binary = project_bin("syzabi_inspect")
    ensure_binary(binary)
    cmd = [
        str(binary),
        "-prog",
        str(program_path),
        "-os",
        config()["target_os"],
        "-arch",
        config()["arch"],
    ]
    if strict:
        cmd.append("-strict")
    result = subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=True,
        env=env_with_go(),
    )
    return json.loads(result.stdout)


def mutate_drop_call(program_path: Path, drop_index: int) -> str:
    binary = project_bin("syzabi_mutate")
    ensure_binary(binary)
    cmd = [
        str(binary),
        "-prog",
        str(program_path),
        "-drop-index",
        str(drop_index),
        "-os",
        config()["target_os"],
        "-arch",
        config()["arch"],
    ]
    result = subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=True,
        env=env_with_go(),
    )
    return result.stdout


def build_prog2c(program_path: Path) -> subprocess.CompletedProcess[str]:
    syz_prog2c = syzkaller_bin("syz-prog2c")
    ensure_binary(syz_prog2c)
    cmd = [
        str(syz_prog2c),
        "-os",
        config()["target_os"],
        "-arch",
        config()["arch"],
        "-prog",
        str(program_path),
        "-repeat=1",
        "-procs=1",
    ]
    return subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=True,
        env=env_with_go(),
    )
