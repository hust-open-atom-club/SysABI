from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from analyzer.schemas import validate_raw_trace
from orchestrator.common import clean_dir, config, dump_json, ensure_dir, resolve_repo_path, runner_profiles, sha256_text
from orchestrator.models import RunResult


def build_root(program_id: str) -> Path:
    return resolve_repo_path(config()["paths"]["build_dir"]) / program_id


def kernel_build(command: str) -> str:
    return subprocess.run(command, shell=True, text=True, check=True, capture_output=True).stdout.strip()


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
    effective_timeout_sec = int(profile.get("timeout_sec", timeout_sec))
    artifact_root = ensure_dir(Path(cfg["paths"]["artifacts_dir"]) / program_id / run_id / side)
    build_artifacts = build_root(program_id)
    sandbox_root = clean_dir(Path(profile["work_root"]) / program_id / run_id)

    for name in ("testcase.c", "testcase.instrumented.c", "testcase.bin", "build-result.json"):
        source = build_artifacts / name
        if source.exists():
            shutil.copy2(source, artifact_root / name)

    stdout_path = artifact_root / "stdout.txt"
    stderr_path = artifact_root / "stderr.txt"
    console_path = artifact_root / "console.log"
    events_path = artifact_root / "raw-trace.events.jsonl"
    raw_trace_path = artifact_root / "raw-trace.json"
    external_state_path = artifact_root / "external-state.json"
    binary_path = artifact_root / "testcase.bin"
    for stale_path in (events_path, raw_trace_path, external_state_path, stdout_path, stderr_path, console_path):
        stale_path.unlink(missing_ok=True)

    env = os.environ.copy()
    env["SYZABI_SIDE"] = side
    env["SYZABI_PROGRAM_ID"] = program_id
    env["SYZABI_RUN_ID"] = run_id
    env["SYZABI_TRACE_EVENTS_PATH"] = str(events_path)
    env["SYZABI_TRACE_PREVIEW_BYTES"] = str(cfg["normalization"]["preview_bytes"])
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

    runner_kind = "local"
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
        exit_code = completed.returncode
        status = classify_process_returncode(completed.returncode)
        status_detail = None
        kernel_build_value = safe_kernel_build(profile["kernel_build_command"])
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
