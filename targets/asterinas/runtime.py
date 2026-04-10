from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from analyzer.schemas import validate_raw_trace
from targets.asterinas.common import RunnerError


SUPPORTED_MODES = ("unconfigured", "local-proxy", "host-direct", "docker-qemu")


def selected_run_timeout_sec(cfg: dict[str, Any], *, batch_timeout_env: str | None = None) -> int:
    if batch_timeout_env:
        return int(batch_timeout_env)
    return int(cfg["asterinas"]["run_timeout_sec"])


def containerized_grub_iso_command(cfg: dict[str, object], package_dir: Path, work_dir: Path, *, hooks) -> list[str]:
    host_env = hooks.host_osdk_env(work_dir, boot_method="grub-rescue-iso")
    host_env["OVMF"] = "on"
    host_env["OVMF_CODE_FILE"] = str(hooks.system_ovmf_code_path())
    host_env["OVMF_VARS_FILE"] = str(hooks.prepare_ovmf_vars(work_dir))
    host_env["EXT2_IMAGE"] = str(work_dir / "ext2.img")
    host_env["EXFAT_IMAGE"] = str(work_dir / "exfat.img")
    qemu_command, _ = hooks.grub_iso_qemu_command(cfg, hooks.shared_package_bundle_dir(package_dir), host_env)
    workspace_root = hooks.repo_root().resolve()
    replaced: list[str] = []
    for token in qemu_command:
        token = str(token).replace(str(workspace_root), str(hooks.docker_workspace_dir(cfg)))
        token = token.replace(str(hooks.system_ovmf_code_path()), hooks.container_ovmf_code_path())
        token = token.replace(str((work_dir / "OVMF_VARS.fd").resolve()), str(hooks.host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)))
        replaced.append(token)
    if hooks.kvm_enabled(host_env) and "-accel" not in replaced and "-enable-kvm" not in replaced:
        replaced.extend(["-accel", "kvm"])
    return replaced


def host_grub_bundle_command(cfg: dict[str, object], package_dir: Path, work_dir: Path, *, hooks) -> tuple[list[str], Path]:
    host_env = hooks.host_osdk_env(work_dir, boot_method="grub-rescue-iso")
    host_env["OVMF"] = "on"
    host_env["OVMF_CODE_FILE"] = str(hooks.system_ovmf_code_path())
    host_env["OVMF_VARS_FILE"] = str(hooks.prepare_ovmf_vars(work_dir))
    host_env["EXT2_IMAGE"] = str(work_dir / "ext2.img")
    host_env["EXFAT_IMAGE"] = str(work_dir / "exfat.img")
    return hooks.grub_iso_qemu_command(cfg, hooks.shared_package_bundle_dir(package_dir), host_env)


def docker_grub_bundle_script(cfg: dict[str, object], package_dir: Path, work_dir: Path, *, hooks) -> str:
    container_cmd = hooks.containerized_grub_iso_command(cfg, package_dir, work_dir)
    return "\n".join([
        "set -euo pipefail",
        "exec " + " ".join(shlex.quote(part) for part in container_cmd),
    ])


def containerized_qemu_direct_command(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    guest_kcmd_args: str = "",
    hooks,
) -> list[str]:
    host_env = hooks.host_osdk_env(work_dir, boot_method="qemu-direct")
    host_env["OVMF"] = "off"
    host_env["EXT2_IMAGE"] = str(work_dir / "ext2.img")
    host_env["EXFAT_IMAGE"] = str(work_dir / "exfat.img")
    if guest_kcmd_args:
        host_env["SYZABI_GUEST_KCMD_ARGS"] = guest_kcmd_args
    qemu_command, _ = hooks.qemu_direct_command(cfg, initramfs_path, host_env)
    workspace_root = hooks.repo_root().resolve()
    qemu_tokens = [str(token) for token in qemu_command]
    replaced: list[str] = []
    for token in qemu_tokens:
        token = token.replace(str(workspace_root), str(hooks.docker_workspace_dir(cfg)))
        replaced.append(token)
    if hooks.kvm_enabled(host_env) and "-accel" not in replaced and "-enable-kvm" not in replaced:
        replaced.extend(["-accel", "kvm"])
    return replaced


def docker_qemu_direct_script(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    guest_kcmd_args: str = "",
    hooks,
) -> str:
    container_cmd = hooks.containerized_qemu_direct_command(cfg, work_dir, initramfs_path, guest_kcmd_args=guest_kcmd_args)
    return "\n".join([
        "set -euo pipefail",
        "exec " + " ".join(shlex.quote(part) for part in container_cmd),
    ])


def docker_osdk_run_script(cfg: dict[str, object], work_dir: Path, initramfs_path: Path, *, kcmd_args: str = "console=hvc0", hooks) -> str:
    container_work_dir = hooks.host_path_to_container_path(work_dir, cfg)
    container_initramfs = hooks.host_path_to_container_path(initramfs_path, cfg)
    container_ovmf_vars = hooks.host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)
    return "\n".join([
        "set -euo pipefail",
        f"mkdir -p {shlex.quote(str(container_work_dir))} \"${{OSDK_OUTPUT_DIR:-{shlex.quote(str(hooks.host_path_to_container_path(work_dir / 'osdk-output', cfg)))}}}\"",
        f"if [ ! -f {shlex.quote(str(container_ovmf_vars))} ]; then cp {shlex.quote(hooks.container_ovmf_vars_seed_path())} {shlex.quote(str(container_ovmf_vars))}; fi",
        f"cd {shlex.quote(str(hooks.docker_repo_dir(cfg) / 'kernel'))}",
        " ".join(shlex.quote(part) for part in hooks.osdk_run_command(container_initramfs, kcmd_args=kcmd_args)),
    ])


def load_batch_manifest(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RunnerError("invalid batch manifest: missing cases")
    return [dict(case) for case in cases]


def docker_qemu_batch_run(args: argparse.Namespace) -> None:
    raise RunnerError("batch manifest mode is disabled because candidate cases must run in isolated VMs")


def local_proxy(args: argparse.Namespace, *, hooks) -> None:
    if not args.binary or not args.work_dir:
        raise RunnerError("--binary and --work-dir are required in local-proxy mode")
    completed = subprocess.run([args.binary], cwd=Path(args.work_dir), text=True, capture_output=True, check=False)
    hooks.required_env_path("SYZABI_STDOUT_PATH").write_text(completed.stdout, encoding="utf-8")
    hooks.required_env_path("SYZABI_STDERR_PATH").write_text(completed.stderr, encoding="utf-8")
    status = "crash" if completed.returncode < 0 else "ok"
    hooks.write_runner_result({"status": status, "exit_code": completed.returncode, "kernel_build": "local-proxy"})


def host_direct_run(args: argparse.Namespace, *, hooks) -> None:
    if not args.binary or not args.work_dir:
        raise RunnerError("--binary and --work-dir are required in host-direct mode")

    cfg = hooks.read_workflow_config()
    binary_path = Path(args.binary).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = hooks.required_env_path("SYZABI_STDOUT_PATH")
    stderr_path = hooks.required_env_path("SYZABI_STDERR_PATH")
    console_log_path = hooks.required_env_path("SYZABI_CONSOLE_LOG_PATH")
    raw_trace_path = hooks.required_env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = hooks.required_env_path("SYZABI_EXTERNAL_STATE_PATH")
    result_path = hooks.runner_result_path()
    for stale_path in (stdout_path, stderr_path, console_log_path, raw_trace_path, external_state_path, result_path):
        stale_path.unlink(missing_ok=True)

    package_dir = hooks.env_path("SYZABI_ASTERINAS_PACKAGE_DIR")
    resolved_package_dir = package_dir.resolve() if package_dir is not None else None
    custom_initramfs = hooks.selected_initramfs(cfg, binary_path, work_dir)
    guest_kcmd_args = " ".join(part for part in ("console=hvc0", hooks.selected_guest_cmdline_append()) if part)
    packaged_bundle_ready = False
    if resolved_package_dir is not None and (hooks.shared_package_bundle_dir(resolved_package_dir) / "bundle.toml").exists():
        try:
            hooks.ensure_packaged_docker_bundle(cfg, resolved_package_dir, custom_initramfs, kcmd_args=guest_kcmd_args)
            packaged_bundle_ready = True
        except RunnerError as exc:
            if not hooks.should_fallback_to_host_direct(exc):
                raise
    revision = hooks.ensure_revision(cfg) if packaged_bundle_ready else hooks.ensure_host_build(cfg)
    hooks.ensure_dummy_block_images(cfg)
    qemu_log_path, qemu_serial_log_path = hooks.qemu_log_paths(work_dir)
    qemu_log_path.unlink(missing_ok=True)
    qemu_serial_log_path.unlink(missing_ok=True)

    cargo_stdout_path = work_dir / "qemu.stdout.txt"
    cargo_stderr_path = work_dir / "qemu.stderr.txt"
    ext2_image, exfat_image = hooks.prepare_run_block_images(cfg, work_dir)
    if packaged_bundle_ready:
        run_env = hooks.host_osdk_env(work_dir, boot_method="grub-rescue-iso")
        run_env["OVMF"] = "on"
        run_env["OVMF_CODE_FILE"] = str(hooks.system_ovmf_code_path())
        run_env["OVMF_VARS_FILE"] = str(hooks.prepare_ovmf_vars(work_dir))
        run_env["EXT2_IMAGE"] = str(ext2_image)
        run_env["EXFAT_IMAGE"] = str(exfat_image)
        qemu_command, qemu_cwd = hooks.host_grub_bundle_command(cfg, resolved_package_dir, work_dir)
    else:
        run_env = hooks.host_osdk_env(work_dir, boot_method="qemu-direct")
        run_env["OVMF"] = "on"
        run_env["OVMF_CODE_FILE"] = str(hooks.system_ovmf_code_path())
        run_env["OVMF_VARS_FILE"] = str(hooks.prepare_ovmf_vars(work_dir))
        run_env["EXT2_IMAGE"] = str(ext2_image)
        run_env["EXFAT_IMAGE"] = str(exfat_image)
        run_env["SYZABI_GUEST_KCMD_ARGS"] = hooks.selected_guest_cmdline_append()
        qemu_command, qemu_cwd = hooks.qemu_direct_command(cfg, custom_initramfs, run_env)

    with cargo_stdout_path.open("a", encoding="utf-8") as cargo_stdout, cargo_stderr_path.open("a", encoding="utf-8") as cargo_stderr:
        run = subprocess.Popen(qemu_command, cwd=qemu_cwd, env=run_env, text=True, stdout=cargo_stdout, stderr=cargo_stderr, start_new_session=True)
        deadline = time.monotonic() + hooks.selected_run_timeout_sec(cfg)
        while True:
            console_preview = hooks.read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
            markers_complete = hooks.extract_section(console_preview, "PROCESS_EXIT") is not None and hooks.extract_section(console_preview, "EXTERNAL_STATE") is not None
            if hooks.extract_section(console_preview, "PROCESS_EXIT") is None:
                if hooks.write_missing_marker_crash_result(
                    console_text=console_preview,
                    raw_trace_path=raw_trace_path,
                    external_state_path=external_state_path,
                    kernel_build=f"asterinas@{revision[:12]}",
                ):
                    console_log_path.write_text(console_preview, encoding="utf-8")
                    hooks.stop_qemu_processes(work_dir)
                    hooks.stop_process(run)
                    return
            if markers_complete:
                hooks.stop_qemu_processes(work_dir)
                hooks.stop_process(run)
                break
            if run.poll() is not None:
                break
            if time.monotonic() >= deadline:
                hooks.stop_qemu_processes(work_dir)
                hooks.stop_process(run)
                stdout = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
                stderr = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
                raise subprocess.TimeoutExpired(run.args, hooks.selected_run_timeout_sec(cfg), output=stdout, stderr=stderr)
            time.sleep(1)
        if run.poll() is None:
            hooks.stop_process(run)

    stdout_text = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
    console_text = hooks.read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
    console_log_path.write_text(console_text, encoding="utf-8")

    process_exit = hooks.parse_process_exit(hooks.extract_section(console_text, "PROCESS_EXIT"))
    stdout_path.write_text(hooks.extract_section(console_text, "STDOUT") or "", encoding="utf-8")
    stderr_path.write_text(hooks.extract_section(console_text, "STDERR") or "", encoding="utf-8")
    external_state = hooks.parse_external_state(hooks.extract_section(console_text, "EXTERNAL_STATE"))
    hooks.dump_json(external_state_path, external_state)

    if hooks.extract_section(console_text, "PROCESS_EXIT") is None:
        if hooks.write_missing_marker_crash_result(console_text=console_text, raw_trace_path=raw_trace_path, external_state_path=external_state_path, kernel_build=f"asterinas@{revision[:12]}"):
            return
        detail = stderr_text.strip() or stdout_text.strip()
        raise RunnerError(detail or "Asterinas autorun markers not found")

    events = hooks.parse_events(hooks.extract_section(console_text, "EVENTS"))
    status = hooks.candidate_status_from_events(events, process_exit)
    raw_trace = {
        "program_id": os.environ.get("SYZABI_PROGRAM_ID", "unknown"),
        "side": os.environ.get("SYZABI_SIDE", "candidate"),
        "run_id": os.environ.get("SYZABI_RUN_ID", "unknown"),
        "status": status,
        "events": events,
        "process_exit": process_exit,
    }
    validate_raw_trace(raw_trace)
    hooks.dump_json(raw_trace_path, raw_trace)
    hooks.write_runner_result({"status": status, "exit_code": process_exit.get("exit_code"), "kernel_build": f"asterinas@{revision[:12]}"})


def docker_qemu_run(args: argparse.Namespace, *, hooks) -> None:
    if not args.binary or not args.work_dir:
        raise RunnerError("--binary and --work-dir are required in docker-qemu mode")

    cfg = hooks.read_workflow_config()
    binary_path = Path(args.binary).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = hooks.required_env_path("SYZABI_STDOUT_PATH")
    stderr_path = hooks.required_env_path("SYZABI_STDERR_PATH")
    console_log_path = hooks.required_env_path("SYZABI_CONSOLE_LOG_PATH")
    raw_trace_path = hooks.required_env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = hooks.required_env_path("SYZABI_EXTERNAL_STATE_PATH")
    result_path = hooks.runner_result_path()
    for stale_path in (stdout_path, stderr_path, console_log_path, raw_trace_path, external_state_path, result_path):
        stale_path.unlink(missing_ok=True)

    custom_initramfs = hooks.selected_initramfs(cfg, binary_path, work_dir)
    hooks.ensure_dummy_block_images(cfg)
    qemu_log_path, qemu_serial_log_path = hooks.qemu_log_paths(work_dir)
    qemu_log_path.unlink(missing_ok=True)
    qemu_serial_log_path.unlink(missing_ok=True)

    cargo_stdout_path = work_dir / "docker-qemu.stdout.txt"
    cargo_stderr_path = work_dir / "docker-qemu.stderr.txt"
    ext2_image, exfat_image = hooks.prepare_run_block_images(cfg, work_dir)
    container_name = hooks.container_name_for_run(os.environ.get("SYZABI_PROGRAM_ID", "program"), os.environ.get("SYZABI_RUN_ID", "run"))
    run_env = hooks.docker_run_env(cfg, work_dir)
    guest_kcmd_args = " ".join(part for part in ("console=hvc0", hooks.selected_guest_cmdline_append()) if part)
    package_dir = hooks.env_path("SYZABI_ASTERINAS_PACKAGE_DIR")
    if package_dir is not None:
        resolved_package_dir = package_dir.resolve()
        hooks.ensure_packaged_docker_bundle(cfg, resolved_package_dir, custom_initramfs, kcmd_args=guest_kcmd_args)
        revision = hooks.ensure_revision(cfg)
        run_cargo_home = hooks.ensure_shared_package_cargo_home(resolved_package_dir)
        cargo_target_dir, osdk_output_dir = hooks.shared_package_runtime_dirs(resolved_package_dir)
    else:
        revision = hooks.ensure_docker_build(cfg)
        run_cargo_home = hooks.prepare_run_cargo_home(work_dir)
        cargo_target_dir = work_dir / "cargo-target"
        osdk_output_dir = work_dir / "osdk-output"
    run_env["CARGO_HOME"] = str(hooks.host_path_to_container_path(run_cargo_home, cfg))
    run_env["CARGO_TARGET_DIR"] = str(hooks.host_path_to_container_path(cargo_target_dir, cfg))
    run_env["OSDK_OUTPUT_DIR"] = str(hooks.host_path_to_container_path(osdk_output_dir, cfg))
    run_env["CARGO_NET_OFFLINE"] = "true"
    run_env["EXT2_IMAGE"] = str(hooks.host_path_to_container_path(ext2_image, cfg))
    run_env["EXFAT_IMAGE"] = str(hooks.host_path_to_container_path(exfat_image, cfg))
    if package_dir is not None:
        qemu_command = hooks.docker_run_command(cfg, hooks.docker_grub_bundle_script(cfg, resolved_package_dir, work_dir), extra_env=run_env, workdir=hooks.docker_repo_dir(cfg), container_name=container_name)
    else:
        qemu_command = hooks.docker_run_command(cfg, hooks.docker_osdk_run_script(cfg, work_dir, custom_initramfs, kcmd_args=guest_kcmd_args), extra_env=run_env, workdir=hooks.docker_repo_dir(cfg), container_name=container_name)

    with cargo_stdout_path.open("a", encoding="utf-8") as cargo_stdout, cargo_stderr_path.open("a", encoding="utf-8") as cargo_stderr:
        run = subprocess.Popen(qemu_command, text=True, stdout=cargo_stdout, stderr=cargo_stderr, start_new_session=True)
        deadline = time.monotonic() + hooks.selected_run_timeout_sec(cfg)
        try:
            while True:
                console_preview = hooks.read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
                markers_complete = hooks.extract_section(console_preview, "PROCESS_EXIT") is not None and hooks.extract_section(console_preview, "EXTERNAL_STATE") is not None
                if hooks.extract_section(console_preview, "PROCESS_EXIT") is None:
                    if hooks.write_missing_marker_crash_result(
                        console_text=console_preview,
                        raw_trace_path=raw_trace_path,
                        external_state_path=external_state_path,
                        kernel_build=f"asterinas@{revision[:12]}",
                    ):
                        console_log_path.write_text(console_preview, encoding="utf-8")
                        hooks.force_remove_container(container_name)
                        hooks.stop_process(run)
                        return
                if markers_complete:
                    hooks.force_remove_container(container_name)
                    hooks.stop_process(run)
                    break
                if run.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    hooks.force_remove_container(container_name)
                    hooks.stop_process(run)
                    stdout = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
                    stderr = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
                    raise subprocess.TimeoutExpired(run.args, hooks.selected_run_timeout_sec(cfg), output=stdout, stderr=stderr)
                time.sleep(1)
            if run.poll() is None:
                hooks.stop_process(run)
        finally:
            hooks.force_remove_container(container_name)

    stdout_text = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
    console_text = hooks.read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
    console_log_path.write_text(console_text, encoding="utf-8")

    process_exit = hooks.parse_process_exit(hooks.extract_section(console_text, "PROCESS_EXIT"))
    stdout_path.write_text(hooks.extract_section(console_text, "STDOUT") or "", encoding="utf-8")
    stderr_path.write_text(hooks.extract_section(console_text, "STDERR") or "", encoding="utf-8")
    external_state = hooks.parse_external_state(hooks.extract_section(console_text, "EXTERNAL_STATE"))
    hooks.dump_json(external_state_path, external_state)

    if hooks.extract_section(console_text, "PROCESS_EXIT") is None:
        if hooks.write_missing_marker_crash_result(console_text=console_text, raw_trace_path=raw_trace_path, external_state_path=external_state_path, kernel_build=f"asterinas@{revision[:12]}"):
            return
        detail = stderr_text.strip() or stdout_text.strip()
        raise RunnerError(detail or "Asterinas autorun markers not found")

    events = hooks.parse_events(hooks.extract_section(console_text, "EVENTS"))
    status = hooks.candidate_status_from_events(events, process_exit)
    raw_trace = {
        "program_id": os.environ.get("SYZABI_PROGRAM_ID", "unknown"),
        "side": os.environ.get("SYZABI_SIDE", "candidate"),
        "run_id": os.environ.get("SYZABI_RUN_ID", "unknown"),
        "status": status,
        "events": events,
        "process_exit": process_exit,
    }
    validate_raw_trace(raw_trace)
    hooks.dump_json(raw_trace_path, raw_trace)
    hooks.write_runner_result({"status": status, "exit_code": process_exit.get("exit_code"), "kernel_build": f"asterinas@{revision[:12]}"})
