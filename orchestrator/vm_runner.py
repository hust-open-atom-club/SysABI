from __future__ import annotations

import json
import os
import shutil
import shlex
import subprocess
import time
from pathlib import Path

from analyzer.schemas import validate_raw_trace
from orchestrator.common import clean_dir, config, dump_json, ensure_dir, env_with_temp, repo_root, resolve_repo_path, runner_profiles, sha256_text
from orchestrator.models import RunResult


def build_root(program_id: str) -> Path:
    return resolve_repo_path(config()["paths"]["build_dir"]) / program_id


def kernel_build(command: str) -> str:
    return subprocess.run(command, shell=True, text=True, check=True, capture_output=True, env=env_with_temp()).stdout.strip()


def safe_kernel_build(command: str) -> str:
    try:
        return kernel_build(command)
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def sample_external_state(work_dir: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for path in sorted(work_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(work_dir).as_posix()
        item: dict[str, object] = {
            "path": relative,
            "size": path.stat().st_size,
        }
        try:
            content = path.read_bytes()
            item["sha256"] = sha256_text(content.decode("latin1"))
        except PermissionError:
            item["sha256"] = None
            item["read_error"] = "permission_denied"
        files.append(item)
    return {"files": files}


def parse_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def classify_process_returncode(returncode: int) -> str:
    if returncode < 0:
        return "crash"
    return "ok"


def execution_context(
    *,
    program_id: str,
    side: str,
    run_id: str,
    timeout_sec: int,
    sandbox_root: Path,
    artifact_root: Path,
    binary_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    console_path: Path,
    events_path: Path,
    raw_trace_path: Path,
    external_state_path: Path,
    runner_result_path: Path,
) -> dict[str, str]:
    return {
        "program_id": program_id,
        "side": side,
        "run_id": run_id,
        "repo_root": str(repo_root()),
        "timeout_sec": str(timeout_sec),
        "sandbox_root": str(sandbox_root),
        "artifact_root": str(artifact_root),
        "binary_path": str(binary_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "console_path": str(console_path),
        "events_path": str(events_path),
        "raw_trace_path": str(raw_trace_path),
        "external_state_path": str(external_state_path),
        "runner_result_path": str(runner_result_path),
    }


def resolve_command(profile: dict[str, object], context: dict[str, str]) -> list[str]:
    command = profile.get("command")
    if not command:
        raise ValueError("command runner profile is missing `command`")
    if isinstance(command, str):
        return [token.format(**context) for token in shlex.split(command)]
    if isinstance(command, list):
        return [str(token).format(**context) for token in command]
    raise TypeError(f"unsupported command profile type: {type(command)!r}")


def load_runner_result(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def finalize_process_result(
    *,
    profile_kind: str,
    completed_returncode: int | None,
    runner_result: dict[str, object] | None,
    fallback_kernel_build: str,
) -> tuple[str, int | None, str | None, str]:
    exit_code = completed_returncode
    if profile_kind == "command":
        status = "ok" if completed_returncode == 0 else "infra_error"
    else:
        status = classify_process_returncode(completed_returncode or 0)
    status_detail = None
    kernel_build_value = fallback_kernel_build
    if runner_result:
        status = str(runner_result.get("status", status))
        exit_code = runner_result.get("exit_code", exit_code)
        status_detail = runner_result.get("status_detail") or runner_result.get("detail")
        kernel_build_value = str(runner_result.get("kernel_build", fallback_kernel_build))
    return status, exit_code, status_detail, kernel_build_value


def execute_side(
    *,
    program_id: str,
    side: str,
    timeout_sec: int,
    run_id: str,
    inject_trace: dict[str, object] | None = None,
) -> RunResult:
    cfg = config()
    profile = runner_profiles()[side]
    binary_name = str(profile.get("binary_name", "testcase.bin"))
    effective_timeout_sec = int(profile.get("timeout_sec", timeout_sec))
    artifact_root = ensure_dir(Path(cfg["paths"]["artifacts_dir"]) / program_id / run_id / side)
    build_artifacts = build_root(program_id)
    sandbox_root = clean_dir(Path(profile["work_root"]) / program_id / run_id)

    for name in ("testcase.c", "testcase.instrumented.c", "testcase.bin", "testcase.candidate.bin", "build-result.json"):
        source = build_artifacts / name
        if source.exists():
            shutil.copy2(source, artifact_root / name)

    stdout_path = artifact_root / "stdout.txt"
    stderr_path = artifact_root / "stderr.txt"
    console_path = artifact_root / "console.log"
    events_path = artifact_root / "raw-trace.events.jsonl"
    raw_trace_path = artifact_root / "raw-trace.json"
    external_state_path = artifact_root / "external-state.json"
    runner_result_path = artifact_root / "runner-result.json"
    binary_path = artifact_root / binary_name
    for stale_path in (events_path, raw_trace_path, external_state_path, stdout_path, stderr_path, console_path, runner_result_path):
        stale_path.unlink(missing_ok=True)

    env = env_with_temp()
    env["SYZABI_SIDE"] = side
    env["SYZABI_PROGRAM_ID"] = program_id
    env["SYZABI_RUN_ID"] = run_id
    env["SYZABI_TRACE_EVENTS_PATH"] = str(events_path)
    env["SYZABI_TRACE_PREVIEW_BYTES"] = str(cfg["normalization"]["preview_bytes"])
    env["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result_path)
    env["SYZABI_WORK_DIR"] = str(sandbox_root)
    env["SYZABI_BINARY_PATH"] = str(binary_path)
    env["SYZABI_STDOUT_PATH"] = str(stdout_path)
    env["SYZABI_STDERR_PATH"] = str(stderr_path)
    env["SYZABI_CONSOLE_LOG_PATH"] = str(console_path)
    env["SYZABI_RAW_TRACE_PATH"] = str(raw_trace_path)
    env["SYZABI_EXTERNAL_STATE_PATH"] = str(external_state_path)
    if inject_trace:
        env["SYZABI_INJECT_TRACE_ENABLED"] = "1"
        env["SYZABI_INJECT_TRACE_CALL_INDEX"] = str(inject_trace.get("call_index", -1))
        env["SYZABI_INJECT_TRACE_SYSCALL"] = str(inject_trace.get("syscall_name", ""))
        env["SYZABI_INJECT_TRACE_FIELD"] = str(inject_trace.get("field", "return"))
        env["SYZABI_INJECT_TRACE_VALUE"] = str(inject_trace.get("value", 0))

    runner_kind = profile.get("kind", "local")
    command_context = execution_context(
        program_id=program_id,
        side=side,
        run_id=run_id,
        timeout_sec=effective_timeout_sec,
        sandbox_root=sandbox_root,
        artifact_root=artifact_root,
        binary_path=binary_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        console_path=console_path,
        events_path=events_path,
        raw_trace_path=raw_trace_path,
        external_state_path=external_state_path,
        runner_result_path=runner_result_path,
    )
    if runner_kind == "command":
        command = resolve_command(profile, command_context)
    else:
        command = [str(binary_path)]

    start = time.monotonic()
    status = "ok"
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            command,
            cwd=sandbox_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=effective_timeout_sec,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        fallback_kernel_build = safe_kernel_build(profile["kernel_build_command"])
        status, exit_code, status_detail, kernel_build_value = finalize_process_result(
            profile_kind=runner_kind,
            completed_returncode=completed.returncode,
            runner_result=load_runner_result(runner_result_path),
            fallback_kernel_build=fallback_kernel_build,
        )
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        status_detail = None
        kernel_build_value = safe_kernel_build(profile["kernel_build_command"])
    except OSError as exc:
        status = "infra_error"
        stdout = ""
        stderr = str(exc)
        status_detail = str(exc)
        kernel_build_value = safe_kernel_build(profile["kernel_build_command"])
    elapsed_ms = int((time.monotonic() - start) * 1000)

    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")

    if not stdout_path.exists():
        stdout_path.write_text(stdout, encoding="utf-8")
    if not stderr_path.exists():
        stderr_path.write_text(stderr, encoding="utf-8")
    if not console_path.exists():
        console_path.write_text(
            json.dumps(
                {
                    "command": command,
                    "cwd": str(sandbox_root),
                    "runner_kind": runner_kind,
                    "status": status,
                    "elapsed_ms": elapsed_ms,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    if raw_trace_path.exists():
        validate_raw_trace(json.loads(raw_trace_path.read_text(encoding="utf-8")))
    else:
        raw_trace = {
            "program_id": program_id,
            "side": side,
            "run_id": run_id,
            "status": status,
            "events": parse_events(events_path),
            "process_exit": {
                "status": status,
                "exit_code": exit_code,
                "timed_out": status == "timeout",
            },
        }
        validate_raw_trace(raw_trace)
        dump_json(raw_trace_path, raw_trace)
    if not external_state_path.exists():
        dump_json(external_state_path, sample_external_state(sandbox_root))

    result = RunResult(
        program_id=program_id,
        side=side,
        status=status,
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        console_log_path=str(console_path),
        trace_json_path=str(raw_trace_path),
        external_state_path=str(external_state_path),
        elapsed_ms=elapsed_ms,
        role=profile["role"],
        snapshot_id=profile["snapshot_id"],
        kernel_build=kernel_build_value,
        run_id=run_id,
        status_detail=status_detail,
        runner_kind=runner_kind,
    )
    dump_json(artifact_root / "run-result.json", result.to_dict())
    return result
