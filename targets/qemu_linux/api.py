#!/usr/bin/env python3
"""QEMU Linux reference runner: boots a real Linux kernel per architecture via QEMU system-mode."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.common import configure_runtime, config, dump_json
from orchestrator.vm_runner import TRACE_EVENT_STDOUT_PREFIX, extract_framed_events


class RunnerError(RuntimeError):
    pass


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def _write_runner_result(payload: dict[str, object]) -> None:
    path = _env_path("SYZABI_RUNNER_RESULT_PATH")
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)


def _read_workflow_config() -> dict[str, Any]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "qemu_linux_riscv64")
    configure_runtime(workflow=workflow)
    cfg = config()
    if cfg.get("target") != "qemu_linux":
        raise RunnerError(
            f"qemu_linux entrypoint requires a qemu_linux workflow, got {cfg.get('target')!r}"
        )
    return cfg


def _target_config(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = cfg.get("target_config")
    if not isinstance(payload, dict):
        raise RunnerError("qemu_linux workflow is missing target_config")
    return payload


def _cache_dir(arch: str) -> Path:
    return Path(ROOT) / "artifacts" / "kernels" / "qemu_linux" / arch


def _prepare_assets(arch: str) -> None:
    """Ensure kernel and minirootfs exist for the given arch."""
    cdir = _cache_dir(arch)
    kernel = cdir / "vmlinux-lts"
    minirootfs = cdir / "minirootfs"

    if kernel.exists() and minirootfs.exists() and any(minirootfs.iterdir()):
        return

    fetch_script = Path(ROOT) / "tools" / "fetch_qemu_linux_assets.py"
    result = subprocess.run(
        [sys.executable, str(fetch_script), "--arch", arch],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RunnerError(
            f"Failed to fetch assets for {arch}: {result.stderr or result.stdout}"
        )


def _build_initramfs(
    *,
    binary_path: Path,
    work_dir: Path,
    arch: str,
) -> Path:
    """Build an initramfs cpio.gz containing the testcase and an init script."""
    cdir = _cache_dir(arch)
    minirootfs = cdir / "minirootfs"
    if not minirootfs.exists():
        raise RunnerError(f"minirootfs not found for {arch}")

    initramfs_dir = Path(work_dir) / "initramfs"
    initramfs_dir.mkdir(parents=True, exist_ok=True)

    # Copy minirootfs contents (preserve symlinks, ignore dangling symlinks)
    for item in minirootfs.iterdir():
        dest = initramfs_dir / item.name
        if item.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(item, dest, symlinks=True, ignore_dangling_symlinks=True)
        elif item.is_symlink():
            if dest.exists() or dest.is_symlink():
                dest.unlink()
            dest.symlink_to(os.readlink(item))
        else:
            shutil.copy2(item, dest)

    # Copy testcase
    testcase_dest = initramfs_dir / "testcase"
    shutil.copy2(binary_path, testcase_dest)

    # Write init script
    init_script = initramfs_dir / "init"
    init_script.write_text(
        '#!/bin/sh\n'
        'mount -t proc none /proc 2>/dev/null\n'
        'mount -t sysfs none /sys 2>/dev/null\n'
        '/testcase\n'
        'echo "__SYZABI_EXIT_CODE__ $?"\n'
        'reboot -f\n',
        encoding="utf-8",
    )
    init_script.chmod(0o755)
    testcase_dest.chmod(0o755)

    # Build cpio.gz
    initramfs_cpio = Path(work_dir) / "initramfs.cpio.gz"
    result = subprocess.run(
        ["sh", "-c", f'cd "{initramfs_dir}" && find . | cpio -H newc -o | gzip > "{initramfs_cpio}"'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RunnerError(f"Failed to build initramfs: {result.stderr}")

    return initramfs_cpio


def _build_qemu_command(
    *,
    arch: str,
    kernel: Path,
    initramfs: Path,
    memory_mb: int,
) -> list[str]:
    """Construct the QEMU command line."""
    qemu_binary = f"qemu-system-{arch}"
    machine = "virt" if arch in ("riscv64", "aarch64") else "q35"

    cmd: list[str] = [
        qemu_binary,
        "-machine", machine,
        "-m", str(memory_mb),
        "-nographic",
        "-no-reboot",
        "-kernel", str(kernel),
        "-initrd", str(initramfs),
        "-append", "console=ttyS0 quiet",
    ]

    if arch == "riscv64":
        cmd += ["-bios", "default"]

    return cmd


def _parse_exit_code(stdout: str) -> int | None:
    """Extract __SYZABI_EXIT_CODE__ from stdout."""
    for line in stdout.splitlines():
        match = re.match(r"__SYZABI_EXIT_CODE__\s+(-?\d+)", line.strip())
        if match:
            return int(match.group(1))
    return None


def _run_qemu(
    *,
    cmd: list[str],
    timeout_sec: int,
) -> tuple[str, str, int]:
    """Run QEMU and return stdout, stderr, returncode."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=False,
        timeout=timeout_sec,
    )
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return stdout, stderr, result.returncode


def run_case(args: argparse.Namespace) -> None:
    cfg = _read_workflow_config()
    tcfg = _target_config(cfg)
    arch = str(cfg.get("arch", ""))
    if not arch:
        raise RunnerError("workflow config missing arch")

    binary = Path(str(args.binary)).resolve()
    work_dir = Path(str(args.work_dir or ".")).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    # Env vars set by orchestrator
    events_path = _env_path("SYZABI_TRACE_EVENTS_PATH")
    raw_trace_path = _env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = _env_path("SYZABI_EXTERNAL_STATE_PATH")
    console_path = _env_path("SYZABI_CONSOLE_LOG_PATH")
    runner_result_path = _env_path("SYZABI_RUNNER_RESULT_PATH")
    program_id = os.environ.get("SYZABI_PROGRAM_ID", binary.stem)
    run_id = os.environ.get("SYZABI_RUN_ID", "")
    timeout_sec = int(tcfg.get("command_timeout_sec", 30))

    # Prepare assets
    _prepare_assets(arch)

    # Build initramfs
    kernel = _cache_dir(arch) / "vmlinux-lts"
    initramfs = _build_initramfs(binary_path=binary, work_dir=work_dir, arch=arch)

    # Launch QEMU
    memory_mb = int(tcfg.get("memory_mb", 512))
    cmd = _build_qemu_command(
        arch=arch,
        kernel=kernel,
        initramfs=initramfs,
        memory_mb=memory_mb,
    )

    stdout, stderr, qemu_rc = _run_qemu(cmd=cmd, timeout_sec=timeout_sec)

    # Parse trace events and exit code
    events = extract_framed_events(stdout)
    guest_exit_code = _parse_exit_code(stdout)

    # If no exit code marker but QEMU exited normally, consider it ok
    if guest_exit_code is None:
        guest_exit_code = 0 if qemu_rc == 0 else qemu_rc

    status = "ok" if guest_exit_code == 0 else "ok"

    # Write outputs
    if raw_trace_path is not None:
        raw_trace_path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(
            raw_trace_path,
            {
                "program_id": program_id,
                "side": "reference",
                "run_id": run_id,
                "status": status,
                "events": events,
                "process_exit": {
                    "status": status,
                    "exit_code": guest_exit_code,
                    "timed_out": False,
                },
            },
        )

    if events_path is not None:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            "".join(
                json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n"
                for e in events
            ),
            encoding="utf-8",
        )

    if external_state_path is not None:
        external_state_path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(external_state_path, {"files": []})

    if console_path is not None:
        console_path.parent.mkdir(parents=True, exist_ok=True)
        console_path.write_text(
            json.dumps(
                {
                    "command": cmd,
                    "qemu_returncode": qemu_rc,
                    "status": status,
                    "stdout_preview": stdout[:4096],
                    "stderr_preview": stderr[:4096],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    if runner_result_path is not None:
        runner_result_path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(
            runner_result_path,
            {
                "status": status,
                "exit_code": guest_exit_code,
                "kernel_build": f"qemu-linux-{arch}",
            },
        )


def healthcheck(args: argparse.Namespace) -> None:
    cfg = _read_workflow_config()
    tcfg = _target_config(cfg)
    arch = str(cfg.get("arch", ""))
    if not arch:
        raise RunnerError("workflow config missing arch")

    _prepare_assets(arch)

    # Build a trivial healthcheck binary
    work_dir = Path(str(args.work_dir or ".")).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    healthcheck_src = work_dir / "healthcheck.c"
    healthcheck_src.write_text(
        '#include <stdio.h>\n'
        'int main() {\n'
        '    printf("__SYZABI_HEALTHCHECK_OK__\\n");\n'
        '    return 0;\n'
        '}\n',
        encoding="utf-8",
    )
    healthcheck_bin = work_dir / "healthcheck"
    compiler = str(tcfg.get("healthcheck_compiler", "gcc"))
    # Use arch-specific compiler if available
    build_cfg = cfg.get("build", {})
    ref_compiler = build_cfg.get("reference", {}).get("compiler_by_arch", {}).get(arch)
    if ref_compiler:
        compiler = str(ref_compiler)

    compile_result = subprocess.run(
        [compiler, "-static", "-o", str(healthcheck_bin), str(healthcheck_src)],
        capture_output=True,
        text=True,
    )
    if compile_result.returncode != 0:
        raise RunnerError(
            f"Healthcheck compile failed: {compile_result.stderr}"
        )

    kernel = _cache_dir(arch) / "vmlinux-lts"
    initramfs = _build_initramfs(binary_path=healthcheck_bin, work_dir=work_dir, arch=arch)
    memory_mb = int(tcfg.get("memory_mb", 512))
    timeout_sec = int(tcfg.get("boot_timeout_sec", 30))

    cmd = _build_qemu_command(
        arch=arch,
        kernel=kernel,
        initramfs=initramfs,
        memory_mb=memory_mb,
    )

    stdout, stderr, qemu_rc = _run_qemu(cmd=cmd, timeout_sec=timeout_sec)

    if "__SYZABI_HEALTHCHECK_OK__" not in stdout:
        raise RunnerError(
            f"Healthcheck failed: QEMU did not produce expected output. stdout:\n{stdout[:2048]}"
        )

    _write_runner_result(
        {"status": "ok", "exit_code": 0, "kernel_build": f"qemu-linux-{arch}"}
    )


def run_batch(args: argparse.Namespace) -> None:
    raise RunnerError("batch execution is not supported for qemu_linux reference")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--batch-manifest")
    parser.add_argument("--work-dir")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode", default="system")
    parser.add_argument("--workflow", default=None)
    parsed = parser.parse_args()

    if parsed.workflow:
        configure_runtime(workflow=parsed.workflow)

    try:
        if parsed.healthcheck:
            healthcheck(parsed)
            return
        if parsed.batch_manifest:
            run_batch(parsed)
            return
        run_case(parsed)
    except RunnerError as exc:
        _write_runner_result(
            {"status": "infra_error", "exit_code": None, "detail": str(exc), "kernel_build": "unknown"}
        )
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
