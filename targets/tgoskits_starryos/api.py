#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.common import config, configure_runtime, dump_json, resolve_repo_path
from orchestrator.vm_runner import TRACE_EVENT_STDOUT_PREFIX, extract_framed_events


class RunnerError(RuntimeError):
    pass


HEALTHCHECK_SUCCESS_MARKER = "__SYZABI_HEALTHCHECK_OK__"
CASE_BEGIN_PREFIX = "__SYZABI_CASE_BEGIN__:"
CASE_EXIT_PREFIX = "__SYZABI_CASE_EXIT__:"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--batch-manifest")
    parser.add_argument("--work-dir")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode", default=os.environ.get("SYZABI_TGOSKITS_STARRY_MODE", "shell-qemu"))
    return parser.parse_args()


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def runner_result_path() -> Path | None:
    return env_path("SYZABI_RUNNER_RESULT_PATH")


def write_runner_result(payload: dict[str, object]) -> None:
    path = runner_result_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)


def read_workflow_config() -> dict[str, Any]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "tgoskits_starryos")
    configure_runtime(workflow=workflow)
    cfg = config()
    if cfg.get("target") != "tgoskits_starryos":
        raise RunnerError(
            f"tgoskits_starryos entrypoint requires a tgoskits_starryos workflow, got {cfg.get('workflow')}"
        )
    return cfg


def target_config(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = cfg.get("target_config")
    if not isinstance(payload, dict):
        raise RunnerError("tgoskits_starryos workflow is missing target_config")
    return payload


def require_feature_flag(cfg: dict[str, Any]) -> None:
    feature_flag_env = str(target_config(cfg).get("feature_flag_env", ""))
    if feature_flag_env and os.environ.get(feature_flag_env) != "1":
        raise RunnerError(f"set {feature_flag_env}=1 to enable TGOSKits external target workflows")


def repo_dir(cfg: dict[str, Any]) -> Path:
    target_cfg = target_config(cfg)
    env_name = str(target_cfg.get("repo_dir_env", "SYZABI_TGOSKITS_DIR"))
    override = os.environ.get(env_name)
    selected = override if override else str(target_cfg.get("repo_dir", ""))
    if not selected:
        raise RunnerError(
            f"missing TGOSKits workspace; set {env_name} or configure target_config.repo_dir for workflow {cfg['workflow']}"
        )
    path = Path(selected).expanduser()
    if not path.exists():
        raise RunnerError(f"configured TGOSKits workspace does not exist: {path}")
    return path


def workspace_dir(cfg: dict[str, Any]) -> Path:
    workspace = repo_dir(cfg) / str(target_config(cfg).get("workspace_subdir", ""))
    if not workspace.exists():
        raise RunnerError(f"configured StarryOS workspace does not exist: {workspace}")
    return workspace


def ensure_supported_arch(cfg: dict[str, Any]) -> None:
    supported_arches = {str(item) for item in target_config(cfg).get("supported_arches", [])}
    arch = str(cfg.get("arch", ""))
    if supported_arches and arch not in supported_arches:
        raise RunnerError(f"unsupported StarryOS arch {arch}; supported={sorted(supported_arches)!r}")


def ensure_pinned_revision(cfg: dict[str, Any]) -> str:
    revision = str(target_config(cfg).get("revision", "")).strip()
    if not revision:
        raise RunnerError("target_config.revision must pin the TGOSKits checkout")
    result = subprocess.run(
        ["git", "-C", str(repo_dir(cfg)), "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RunnerError(result.stderr.strip() or result.stdout.strip() or "failed to read TGOSKits revision")
    current = result.stdout.strip()
    if current != revision:
        raise RunnerError(f"TGOSKits revision mismatch: expected {revision}, got {current}")
    return current


def ensure_toolchain_probes(cfg: dict[str, Any]) -> None:
    missing = [tool for tool in target_config(cfg).get("toolchain_probes", []) if shutil_which(str(tool)) is None]
    if missing:
        raise RunnerError(f"missing required StarryOS tools: {', '.join(missing)}")


def shutil_which(tool: str) -> str | None:
    return subprocess.run(
        ["sh", "-lc", f"command -v {shlex.quote(tool)}"],
        check=False,
        text=True,
        capture_output=True,
    ).stdout.strip() or None


def command_values(
    cfg: dict[str, Any],
    *,
    serial_port: int = 0,
) -> dict[str, str]:
    return {
        "arch": str(cfg.get("arch", "riscv64")),
        "repo_dir": str(repo_dir(cfg)),
        "serial_port": str(serial_port),
    }


def resolve_command(template: object, values: dict[str, str]) -> list[str]:
    if isinstance(template, str):
        return [token.format(**values) for token in shlex.split(template)]
    if isinstance(template, list):
        return [str(token).format(**values) for token in template]
    raise RunnerError(f"unsupported command template type: {type(template)!r}")


def run_subprocess(command: list[str], *, cwd: Path, timeout_sec: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        text=True,
        timeout=timeout_sec,
        capture_output=True,
    )


def disk_image_path(cfg: dict[str, Any]) -> Path:
    return repo_dir(cfg) / str(target_config(cfg).get("disk_image_path", ""))


def guest_binary_path(cfg: dict[str, Any], *, suffix: str = "") -> str:
    base = str(target_config(cfg).get("guest_binary_path", "/bin/testcase.candidate.bin"))
    if not suffix:
        return base
    path = Path(base)
    return str(path.with_name(f"{path.stem}{suffix}{path.suffix}"))


def install_binary_into_rootfs(cfg: dict[str, Any], host_binary: Path, guest_path: str) -> None:
    image = disk_image_path(cfg)
    if not image.exists():
        raise RunnerError(f"StarryOS disk image is missing: {image}")
    subprocess.run(
        ["debugfs", "-w", "-R", f"rm {guest_path}", str(image)],
        check=False,
        text=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["debugfs", "-w", "-R", f"write {host_binary} {guest_path}", str(image)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RunnerError(result.stderr.strip() or result.stdout.strip() or f"failed to write {guest_path} into {image}")


def reserve_tcp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


class ShellSession:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self._console_parts: list[str] = []
        self._lock = threading.Lock()
        self.process: subprocess.Popen[str] | None = None
        self.sock: socket.socket | None = None

    def _append_console(self, chunk: str) -> None:
        with self._lock:
            self._console_parts.append(chunk)

    def console_text(self) -> str:
        with self._lock:
            return "".join(self._console_parts)

    def _stream_reader(self, stream) -> None:
        for line in iter(stream.readline, ""):
            if not line:
                break
            self._append_console(line)

    def start(self) -> None:
        serial_port = reserve_tcp_port()
        launch_command = resolve_command(target_config(self.cfg)["shell_launch_command"], command_values(self.cfg, serial_port=serial_port))
        self.process = subprocess.Popen(
            launch_command,
            cwd=str(workspace_dir(self.cfg)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            bufsize=1,
        )
        for stream in (self.process.stdout, self.process.stderr):
            if stream is None:
                continue
            thread = threading.Thread(target=self._stream_reader, args=(stream,), daemon=True)
            thread.start()
        deadline = time.time() + int(target_config(self.cfg).get("boot_timeout_sec", 60))
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RunnerError("StarryOS launch process exited before shell became available")
            try:
                self.sock = socket.create_connection(("127.0.0.1", serial_port), timeout=1)
                self.sock.settimeout(1)
                break
            except OSError:
                time.sleep(0.2)
        if self.sock is None:
            raise RunnerError("failed to connect to StarryOS serial console")
        self.read_until(target_config(self.cfg)["shell_prompt"], timeout_sec=int(target_config(self.cfg).get("boot_timeout_sec", 60)))

    def read_until(self, marker: str, *, timeout_sec: int) -> str:
        if self.sock is None:
            raise RunnerError("serial socket is not connected")
        deadline = time.time() + timeout_sec
        collected: list[str] = []
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096).decode("utf-8", errors="ignore")
            except socket.timeout:
                continue
            if not chunk:
                if self.process is not None and self.process.poll() is not None:
                    break
                continue
            collected.append(chunk)
            self._append_console(chunk)
            if marker in "".join(collected):
                return "".join(collected)
        raise RunnerError(f"did not observe marker {marker!r} before timeout")

    def run_command(self, label: str, command: str, *, timeout_sec: int) -> tuple[str, int]:
        if self.sock is None:
            raise RunnerError("serial socket is not connected")
        begin_marker = f"{CASE_BEGIN_PREFIX}{label}"
        exit_marker = f"{CASE_EXIT_PREFIX}{label}:"
        full_command = f"echo {begin_marker}; {command}; echo {exit_marker}$?"
        self.sock.sendall((full_command + "\r\n").encode("utf-8"))
        output = self.read_until(target_config(self.cfg)["shell_prompt"], timeout_sec=timeout_sec)
        begin_index = output.find(begin_marker)
        exit_index = output.find(exit_marker)
        if begin_index == -1 or exit_index == -1:
            raise RunnerError(f"failed to capture command markers for {label}")
        exit_line = output[exit_index:].splitlines()[0]
        try:
            exit_code = int(exit_line[len(exit_marker) :].strip())
        except ValueError as exc:
            raise RunnerError(f"failed to parse exit marker for {label}: {exit_line!r}") from exc
        section = output[begin_index + len(begin_marker) : exit_index]
        return section, exit_code

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except Exception:
                    pass
            for stream in (self.process.stdout, self.process.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except OSError:
                    pass
        self.process = None


def prepare_target(cfg: dict[str, Any]) -> str:
    require_feature_flag(cfg)
    ensure_supported_arch(cfg)
    ensure_toolchain_probes(cfg)
    revision = ensure_pinned_revision(cfg)
    values = command_values(cfg)
    for template in target_config(cfg).get("prepare_commands", []):
        command = resolve_command(template, values)
        completed = run_subprocess(command, cwd=workspace_dir(cfg), timeout_sec=int(target_config(cfg).get("prepare_timeout_sec", 1800)))
        if completed.returncode != 0:
            raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or f"prepare command failed: {' '.join(command)}")
    dump_json(
        resolve_repo_path(target_config(cfg)["build_info_path"]),
        {
            "target": "tgoskits_starryos",
            "revision": revision,
            "arch": str(cfg.get("arch", "")),
            "disk_image_path": str(disk_image_path(cfg)),
            "workspace": str(repo_dir(cfg)),
        },
    )
    return f"tgoskits-starryos@{revision[:12]}"


def healthcheck(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    session = ShellSession(cfg)
    try:
        session.start()
        section, exit_code = session.run_command(
            "healthcheck",
            str(target_config(cfg)["healthcheck_shell_command"]),
            timeout_sec=int(target_config(cfg).get("command_timeout_sec", 30)),
        )
        if exit_code != 0 or HEALTHCHECK_SUCCESS_MARKER not in section:
            raise RunnerError("StarryOS healthcheck shell command did not complete successfully")
        write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": label})
    finally:
        console_path = env_path("SYZABI_CONSOLE_LOG_PATH")
        if console_path is not None:
            console_path.write_text(session.console_text(), encoding="utf-8")
        session.close()


def env_assignments(cfg: dict[str, Any]) -> str:
    variables = {
        "SYZABI_SIDE": os.environ.get("SYZABI_SIDE", "candidate"),
        "SYZABI_TRACE_EVENTS_PATH": os.environ.get("SYZABI_TRACE_EVENTS_PATH", "stdout"),
        "SYZABI_TRACE_PREVIEW_BYTES": os.environ.get("SYZABI_TRACE_PREVIEW_BYTES", str(cfg["normalization"]["preview_bytes"])),
    }
    for name in (
        "SYZABI_INJECT_TRACE_ENABLED",
        "SYZABI_INJECT_TRACE_CALL_INDEX",
        "SYZABI_INJECT_TRACE_SYSCALL",
        "SYZABI_INJECT_TRACE_FIELD",
        "SYZABI_INJECT_TRACE_VALUE",
    ):
        if name in os.environ:
            variables[name] = os.environ[name]
    return " ".join(f"{name}={shlex.quote(value)}" for name, value in variables.items())


def write_case_outputs(
    *,
    cfg: dict[str, Any],
    program_id: str,
    run_id: str,
    console_text: str,
    command_output: str,
    exit_code: int,
    kernel_build: str,
    raw_trace_path: Path,
    external_state_path: Path,
    runner_result_path_value: Path,
    console_path: Path | None,
) -> None:
    events = extract_framed_events(command_output)
    raw_trace = {
        "program_id": program_id,
        "side": "candidate",
        "run_id": run_id,
        "status": "ok",
        "events": events,
        "process_exit": {
            "status": "ok",
            "exit_code": exit_code,
            "timed_out": False,
        },
    }
    dump_json(raw_trace_path, raw_trace)
    dump_json(external_state_path, {"files": []})
    dump_json(
        runner_result_path_value,
        {
            "status": "ok",
            "exit_code": exit_code,
            "kernel_build": kernel_build,
        },
    )
    if console_path is not None:
        console_path.write_text(console_text, encoding="utf-8")


def run_case(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    binary = Path(str(args.binary)).resolve()
    guest_path = guest_binary_path(cfg)
    install_binary_into_rootfs(cfg, binary, guest_path)
    session = ShellSession(cfg)
    try:
        session.start()
        command = f"{env_assignments(cfg)} {shlex.quote(guest_path)}"
        section, exit_code = session.run_command(
            Path(binary).name,
            command,
            timeout_sec=int(target_config(cfg).get("command_timeout_sec", 30)),
        )
        raw_trace_path = env_path("SYZABI_RAW_TRACE_PATH")
        external_state_path = env_path("SYZABI_EXTERNAL_STATE_PATH")
        console_path = env_path("SYZABI_CONSOLE_LOG_PATH")
        runner_result = runner_result_path()
        if raw_trace_path is None or external_state_path is None or runner_result is None:
            raise RunnerError("missing SysABI output paths for StarryOS candidate run")
        write_case_outputs(
            cfg=cfg,
            program_id=str(os.environ.get("SYZABI_PROGRAM_ID", Path(binary).stem)),
            run_id=str(os.environ.get("SYZABI_RUN_ID", "")),
            console_text=session.console_text(),
            command_output=section,
            exit_code=exit_code,
            kernel_build=label,
            raw_trace_path=raw_trace_path,
            external_state_path=external_state_path,
            runner_result_path_value=runner_result,
            console_path=console_path,
        )
    finally:
        session.close()


def load_batch_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_batch(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    manifest = load_batch_manifest(Path(str(args.batch_manifest)))
    cases = list(manifest.get("cases", []))
    session = ShellSession(cfg)
    try:
        for index, case in enumerate(cases):
            install_binary_into_rootfs(cfg, Path(str(case["binary_path"])).resolve(), guest_binary_path(cfg, suffix=f"-{index}"))
        session.start()
        for index, case in enumerate(cases):
            guest_path = guest_binary_path(cfg, suffix=f"-{index}")
            section, exit_code = session.run_command(
                str(case["program_id"]),
                f"{env_assignments(cfg)} {shlex.quote(guest_path)}",
                timeout_sec=int(target_config(cfg).get("command_timeout_sec", 30)),
            )
            write_case_outputs(
                cfg=cfg,
                program_id=str(case["program_id"]),
                run_id=str(case["run_id"]),
                console_text=session.console_text(),
                command_output=section,
                exit_code=exit_code,
                kernel_build=label,
                raw_trace_path=Path(str(case["raw_trace_path"])),
                external_state_path=Path(str(case["external_state_path"])),
                runner_result_path_value=Path(str(case["runner_result_path"])),
                console_path=Path(str(case["console_path"])),
            )
    finally:
        session.close()


def main() -> None:
    args = parse_args()
    try:
        if args.healthcheck:
            healthcheck(args)
            return
        if args.batch_manifest:
            run_batch(args)
            return
        run_case(args)
    except RunnerError as exc:
        write_runner_result({"status": "infra_error", "exit_code": None, "detail": str(exc), "kernel_build": "unknown"})
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
