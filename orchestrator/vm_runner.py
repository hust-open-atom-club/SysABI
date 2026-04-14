from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import shlex
import subprocess
import time
from pathlib import Path

from analyzer.schemas import validate_raw_trace
from core.workflow_contract import trace_events_transport
from orchestrator.common import clean_dir, config, dump_json, ensure_dir, env_with_temp, path_resolver, repo_root, resolve_repo_path, runner_profiles, sha256_text
from orchestrator.models import RunResult
from runners import build_runner
from targets.base import PACKAGED_PER_CASE_EXECUTION_MODE, SHARED_RUNTIME_BATCH_EXECUTION_MODE, canonical_execution_mode
from targets.registry import get_target_adapter

TRACE_EVENT_STDOUT_PREFIX = "__SYZABI_TRACE_EVENT__ "


def build_root(program_id: str) -> Path:
    return path_resolver(config()).build_dir() / program_id


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
        try:
            if not path.is_file():
                continue
            relative = path.relative_to(work_dir).as_posix()
            size = path.stat().st_size
        except OSError:
            continue
        item: dict[str, object] = {
            "path": relative,
            "size": size,
        }
        try:
            content = path.read_bytes()
            item["sha256"] = sha256_text(content.decode("latin1"))
        except (PermissionError, OSError):
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


def extract_framed_events(text: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for raw_line in text.splitlines():
        if not raw_line.startswith(TRACE_EVENT_STDOUT_PREFIX):
            continue
        payload = raw_line[len(TRACE_EVENT_STDOUT_PREFIX) :].strip()
        if not payload:
            continue
        events.append(json.loads(payload))
    return events


def persisted_trace_events(
    *,
    cfg: dict[str, object],
    events_path: Path,
    stdout_text: str,
    stderr_text: str,
    console_path: Path,
) -> list[dict[str, object]]:
    try:
        file_events = parse_events(events_path)
    except json.JSONDecodeError:
        file_events = []
    if file_events:
        return file_events
    if trace_events_transport(cfg) != "stdout":
        return []

    extracted: list[dict[str, object]] = []
    seen: set[str] = set()
    candidates = [stdout_text, stderr_text]
    if console_path.exists():
        candidates.append(console_path.read_text(encoding="utf-8", errors="replace"))
    for candidate in candidates:
        for event in extract_framed_events(candidate):
            digest = json.dumps(event, ensure_ascii=False, sort_keys=True)
            if digest in seen:
                continue
            seen.add(digest)
            extracted.append(event)
    if extracted and not events_path.exists():
        events_path.write_text(
            "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in extracted),
            encoding="utf-8",
        )
    return extracted


def trace_events_destination(*, cfg: dict[str, object], events_path: Path) -> str:
    transport = trace_events_transport(cfg)
    if transport == "stdout":
        return "stdout"
    return str(events_path)


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
    batch_manifest_path: Path | None = None,
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
        "batch_manifest_path": str(batch_manifest_path) if batch_manifest_path is not None else "",
    }


def resolve_command(profile: dict[str, object], context: dict[str, str], *, key: str = "command") -> list[str]:
    command = profile.get(key)
    if not command:
        raise ValueError(f"command runner profile is missing `{key}`")
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


def prepare_candidate_batch_case(
    *,
    program_id: str,
    timeout_sec: int,
    run_id: str,
    inject_trace: dict[str, object] | None = None,
) -> dict[str, object]:
    cfg = config()
    profile = runner_profiles()["candidate"]
    binary_name = str(profile.get("binary_name", "testcase.bin"))
    effective_timeout_sec = int(profile.get("timeout_sec", timeout_sec))
    artifact_root = ensure_dir(Path(cfg["paths"]["artifacts_dir"]) / program_id / run_id / "candidate")
    sandbox_root = clean_dir(Path(profile["work_root"]) / program_id / run_id)
    build_artifacts = build_root(program_id)

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

    return {
        "program_id": program_id,
        "run_id": run_id,
        "effective_timeout_sec": effective_timeout_sec,
        "artifact_root": str(artifact_root),
        "sandbox_root": str(sandbox_root),
        "binary_path": str(binary_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "console_path": str(console_path),
        "events_path": str(events_path),
        "raw_trace_path": str(raw_trace_path),
        "external_state_path": str(external_state_path),
        "runner_result_path": str(runner_result_path),
        "inject_trace": inject_trace,
        "role": profile["role"],
        "snapshot_id": profile["snapshot_id"],
        "runner_kind": profile.get("kind", "command"),
    }


def candidate_initramfs_package_root() -> Path:
    return ensure_dir(path_resolver(config()).candidate_initramfs_packages_dir())


def packaged_initramfs_template_inputs(cfg: dict[str, object]) -> dict[str, object]:
    return get_target_adapter(cfg).compose_template_inputs(cfg)


def package_case_descriptor(case: dict[str, object]) -> dict[str, object]:
    binary_path = Path(str(case["binary_path"]))
    digest = hashlib.sha256(binary_path.read_bytes()).hexdigest()
    return {
        "program_id": str(case["program_id"]),
        "binary_sha256": digest,
    }


def prepare_candidate_initramfs_package(
    cases: list[dict[str, object]],
    cfg: dict[str, object],
    *,
    batch_metadata: dict[str, object] | None = None,
) -> tuple[Path, dict[str, int]]:
    template_inputs = packaged_initramfs_template_inputs(cfg)
    package_descriptor = {
        "workflow": cfg["workflow"],
        "preview_bytes": int(cfg["normalization"]["preview_bytes"]),
        "template_inputs": template_inputs,
        "batch_metadata": batch_metadata or {},
        "cases": [package_case_descriptor(case) for case in cases],
    }
    package_id = sha256_text(json.dumps(package_descriptor, ensure_ascii=False, sort_keys=True))
    package_dir = ensure_dir(candidate_initramfs_package_root() / package_id)
    manifest_path = package_dir / "package-manifest.json"
    manifest_payload = {
        "package_id": package_id,
        "workflow": cfg["workflow"],
        "preview_bytes": int(cfg["normalization"]["preview_bytes"]),
        "template_inputs": template_inputs,
        "batch_metadata": batch_metadata or {},
        "cases": [
            {
                "slot": slot,
                "program_id": str(case["program_id"]),
                "binary_path": str(case["binary_path"]),
                "binary_sha256": package_descriptor["cases"][slot]["binary_sha256"],
            }
            for slot, case in enumerate(cases)
        ],
    }
    dump_json(manifest_path, manifest_payload)
    return package_dir, {str(case["program_id"]): slot for slot, case in enumerate(cases)}


def prepare_shared_batch_manifest(
    cases: list[dict[str, object]],
    cfg: dict[str, object],
    *,
    batch_metadata: dict[str, object] | None = None,
) -> Path:
    payload = {
        "workflow": str(cfg.get("workflow", "")),
        "target": str(cfg.get("target", "")),
        "arch": str(cfg.get("arch", "")),
        "preview_bytes": int(cfg["normalization"]["preview_bytes"]),
        "batch_metadata": batch_metadata or {},
        "cases": [
            {
                "program_id": str(case["program_id"]),
                "run_id": str(case["run_id"]),
                "binary_path": str(case["binary_path"]),
                "stdout_path": str(case["stdout_path"]),
                "stderr_path": str(case["stderr_path"]),
                "console_path": str(case["console_path"]),
                "events_path": str(case["events_path"]),
                "raw_trace_path": str(case["raw_trace_path"]),
                "external_state_path": str(case["external_state_path"]),
                "runner_result_path": str(case["runner_result_path"]),
            }
            for case in cases
        ],
    }
    manifest_dir = ensure_dir(path_resolver(cfg).temp_dir() / "target-batches")
    batch_id = sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    manifest_path = manifest_dir / f"{batch_id}.json"
    dump_json(manifest_path, payload)
    return manifest_path


def prewarm_packaged_candidate_bundle(
    prepared_cases: list[dict[str, object]],
    package_dir: Path,
    cfg: dict[str, object],
) -> None:
    get_target_adapter(cfg).prewarm_candidate_batch(
        prepared_cases=prepared_cases,
        package_dir=package_dir,
        cfg=cfg,
    )


def execute_prepared_candidate_case(
    *,
    case: dict[str, object],
    package_dir: Path,
    slot: int,
) -> RunResult:
    cfg = config()
    profile = runner_profiles()["candidate"]
    sandbox_root = Path(str(case["sandbox_root"]))
    artifact_root = Path(str(case["artifact_root"]))
    stdout_path = Path(str(case["stdout_path"]))
    stderr_path = Path(str(case["stderr_path"]))
    console_path = Path(str(case["console_path"]))
    events_path = Path(str(case["events_path"]))
    raw_trace_path = Path(str(case["raw_trace_path"]))
    external_state_path = Path(str(case["external_state_path"]))
    runner_result_path = Path(str(case["runner_result_path"]))
    binary_path = Path(str(case["binary_path"]))
    effective_timeout_sec = int(case["effective_timeout_sec"])

    env = env_with_temp(cfg=cfg)
    env["SYZABI_SIDE"] = "candidate"
    env["SYZABI_PROGRAM_ID"] = str(case["program_id"])
    env["SYZABI_RUN_ID"] = str(case["run_id"])
    env["SYZABI_TRACE_EVENTS_PATH"] = trace_events_destination(cfg=cfg, events_path=events_path)
    env["SYZABI_TRACE_PREVIEW_BYTES"] = str(cfg["normalization"]["preview_bytes"])
    env["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result_path)
    env["SYZABI_WORK_DIR"] = str(sandbox_root)
    env["SYZABI_BINARY_PATH"] = str(binary_path)
    env["SYZABI_STDOUT_PATH"] = str(stdout_path)
    env["SYZABI_STDERR_PATH"] = str(stderr_path)
    env["SYZABI_CONSOLE_LOG_PATH"] = str(console_path)
    env["SYZABI_RAW_TRACE_PATH"] = str(raw_trace_path)
    env["SYZABI_EXTERNAL_STATE_PATH"] = str(external_state_path)
    env.update(get_target_adapter(cfg).packaged_candidate_env(package_dir, slot))
    inject_trace = case.get("inject_trace")
    if inject_trace:
        env["SYZABI_INJECT_TRACE_ENABLED"] = "1"
        env["SYZABI_INJECT_TRACE_CALL_INDEX"] = str(inject_trace.get("call_index", -1))
        env["SYZABI_INJECT_TRACE_SYSCALL"] = str(inject_trace.get("syscall_name", ""))
        env["SYZABI_INJECT_TRACE_FIELD"] = str(inject_trace.get("field", "return"))
        env["SYZABI_INJECT_TRACE_VALUE"] = str(inject_trace.get("value", 0))

    command_context = execution_context(
        program_id=str(case["program_id"]),
        side="candidate",
        run_id=str(case["run_id"]),
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
    command = resolve_command(profile, command_context)

    start = time.monotonic()
    status = "ok"
    exit_code: int | None = None
    stdout = ""
    stderr = ""
    runner = build_runner(profile)
    execution = runner.run_case(
        command=command,
        cwd=str(sandbox_root),
        env=env,
        timeout_sec=effective_timeout_sec,
    )
    try:
        stdout = execution.stdout
        stderr = execution.stderr
        fallback_kernel_build = safe_kernel_build(profile["kernel_build_command"])
        status, exit_code, status_detail, kernel_build_value = finalize_process_result(
            profile_kind=str(case["runner_kind"]),
            completed_returncode=execution.returncode,
            runner_result=load_runner_result(runner_result_path),
            fallback_kernel_build=fallback_kernel_build,
        )
        if execution.timed_out:
            raise subprocess.TimeoutExpired(command, effective_timeout_sec, output=stdout, stderr=stderr)
        if execution.os_error is not None:
            raise OSError(execution.os_error)
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
                    "runner_kind": str(case["runner_kind"]),
                    "status": status,
                    "elapsed_ms": elapsed_ms,
                    "initramfs_package_dir": str(package_dir),
                    "initramfs_package_slot": slot,
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
        events = persisted_trace_events(
            cfg=cfg,
            events_path=events_path,
            stdout_text=stdout,
            stderr_text=stderr,
            console_path=console_path,
        )
        raw_trace = {
            "program_id": str(case["program_id"]),
            "side": "candidate",
            "run_id": str(case["run_id"]),
            "status": status,
            "events": events,
            "process_exit": {
                "status": status,
                "exit_code": exit_code,
                "timed_out": status == "timeout",
            },
        }
        validate_raw_trace(raw_trace)
        dump_json(raw_trace_path, raw_trace)
    if not external_state_path.exists():
        dump_json(external_state_path, {"files": []})

    result = RunResult(
        program_id=str(case["program_id"]),
        side="candidate",
        status=status,
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        console_log_path=str(console_path),
        trace_json_path=str(raw_trace_path),
        external_state_path=str(external_state_path),
        elapsed_ms=elapsed_ms,
        role=str(case["role"]),
        snapshot_id=str(case["snapshot_id"]),
        kernel_build=kernel_build_value,
        run_id=str(case["run_id"]),
        status_detail=status_detail,
        runner_kind=str(case["runner_kind"]),
    )
    dump_json(Path(str(case["artifact_root"])) / "run-result.json", result.to_dict())
    return result


def execute_candidate_case_in_package(
    *,
    program_id: str,
    timeout_sec: int,
    run_id: str,
    package_dir: Path,
    slot: int,
    inject_trace: dict[str, object] | None = None,
) -> RunResult:
    case = prepare_candidate_batch_case(
        program_id=program_id,
        timeout_sec=timeout_sec,
        run_id=run_id,
        inject_trace=inject_trace,
    )
    return execute_prepared_candidate_case(
        case=case,
        package_dir=package_dir,
        slot=slot,
    )


def finalize_batch_case_result(
    *,
    case: dict[str, object],
    elapsed_ms: int,
) -> RunResult:
    cfg = config()
    raw_trace_path = Path(str(case["raw_trace_path"]))
    external_state_path = Path(str(case["external_state_path"]))
    stdout_path = Path(str(case["stdout_path"]))
    stderr_path = Path(str(case["stderr_path"]))
    console_path = Path(str(case["console_path"]))
    runner_result_path = Path(str(case["runner_result_path"]))
    runner_result = load_runner_result(runner_result_path)

    if runner_result is None:
        status = "infra_error"
        exit_code = None
        status_detail = "missing candidate batch runner result"
        kernel_build_value = safe_kernel_build(runner_profiles()["candidate"]["kernel_build_command"])
    else:
        status, exit_code, status_detail, kernel_build_value = finalize_process_result(
            profile_kind=str(case["runner_kind"]),
            completed_returncode=0,
            runner_result=runner_result,
            fallback_kernel_build=safe_kernel_build(runner_profiles()["candidate"]["kernel_build_command"]),
        )

    if raw_trace_path.exists():
        validate_raw_trace(json.loads(raw_trace_path.read_text(encoding="utf-8")))
    else:
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
        events = persisted_trace_events(
            cfg=cfg,
            events_path=Path(str(case["events_path"])),
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            console_path=console_path,
        )
        raw_trace = {
            "program_id": str(case["program_id"]),
            "side": "candidate",
            "run_id": str(case["run_id"]),
            "status": status,
            "events": events,
            "process_exit": {
                "status": status,
                "exit_code": exit_code,
                "timed_out": status == "timeout",
            },
        }
        validate_raw_trace(raw_trace)
        dump_json(raw_trace_path, raw_trace)
    if not external_state_path.exists():
        dump_json(external_state_path, {"files": []})
    if not stdout_path.exists():
        stdout_path.write_text("", encoding="utf-8")
    if not stderr_path.exists():
        stderr_path.write_text("", encoding="utf-8")
    if not console_path.exists():
        console_path.write_text("", encoding="utf-8")

    result = RunResult(
        program_id=str(case["program_id"]),
        side="candidate",
        status=status,
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        console_log_path=str(console_path),
        trace_json_path=str(raw_trace_path),
        external_state_path=str(external_state_path),
        elapsed_ms=elapsed_ms,
        role=str(case["role"]),
        snapshot_id=str(case["snapshot_id"]),
        kernel_build=kernel_build_value,
        run_id=str(case["run_id"]),
        status_detail=status_detail,
        runner_kind=str(case["runner_kind"]),
    )
    dump_json(Path(str(case["artifact_root"])) / "run-result.json", result.to_dict())
    return result


def execute_candidate_batch(
    *,
    batch_cases: list[dict[str, object]],
    timeout_sec: int,
    max_workers: int | None = None,
) -> dict[str, RunResult]:
    results, _, _ = execute_candidate_batch_with_context(
        batch_cases=batch_cases,
        timeout_sec=timeout_sec,
        max_workers=max_workers,
    )
    return results


def execute_candidate_batch_with_context(
    *,
    batch_cases: list[dict[str, object]],
    timeout_sec: int,
    max_workers: int | None = None,
) -> tuple[dict[str, RunResult], Path | None, dict[str, int | None]]:
    if not batch_cases:
        return {}, None, {}

    cfg = config()
    profile = runner_profiles()["candidate"]
    if profile.get("kind") != "command":
        raise ValueError("candidate batch execution requires a command runner profile")
    adapter = get_target_adapter(cfg)
    command_batching_mode = canonical_execution_mode(
        str(profile.get("command_batching_mode") or PACKAGED_PER_CASE_EXECUTION_MODE)
    )
    if command_batching_mode not in set(adapter.execution_modes(cfg)):
        raise ValueError(
            f"candidate runner batching mode {command_batching_mode!r} is not supported by target {cfg.get('target')!r}"
        )

    prepared_cases = [
        prepare_candidate_batch_case(
            program_id=str(case["program_id"]),
            timeout_sec=timeout_sec,
            run_id=str(case["run_id"]),
            inject_trace=case.get("inject_trace"),
        )
        for case in batch_cases
    ]
    batch_metadata = adapter.prepare_batch(prepared_cases, cfg) or {}
    if command_batching_mode == SHARED_RUNTIME_BATCH_EXECUTION_MODE:
        manifest_path = prepare_shared_batch_manifest(prepared_cases, cfg, batch_metadata=batch_metadata)
        manifest_dir = manifest_path.parent
        command_context = execution_context(
            program_id="candidate-batch",
            side="candidate",
            run_id=manifest_path.stem,
            timeout_sec=timeout_sec,
            sandbox_root=manifest_dir,
            artifact_root=manifest_dir,
            binary_path=manifest_dir / "unused.bin",
            stdout_path=manifest_dir / "stdout.txt",
            stderr_path=manifest_dir / "stderr.txt",
            console_path=manifest_dir / "console.log",
            events_path=manifest_dir / "events.jsonl",
            raw_trace_path=manifest_dir / "raw-trace.json",
            external_state_path=manifest_dir / "external-state.json",
            runner_result_path=manifest_dir / "runner-result.json",
            batch_manifest_path=manifest_path,
        )
        command = resolve_command(profile, command_context, key="batch_command")
        runner = build_runner(profile)
        execution = runner.run_case(
            command=command,
            cwd=str(repo_root()),
            env=env_with_temp(cfg=cfg),
            timeout_sec=max(timeout_sec, len(prepared_cases) * timeout_sec),
        )
        if execution.timed_out or execution.os_error is not None:
            detail = execution.os_error or "shared batch execution timed out"
            status = "timeout" if execution.timed_out else "infra_error"
            for case in prepared_cases:
                dump_json(
                    Path(str(case["runner_result_path"])),
                    {
                        "status": status,
                        "exit_code": None,
                        "detail": detail,
                        "kernel_build": str(profile.get("snapshot_id", "unknown")),
                    },
                )
                Path(str(case["stdout_path"])).write_text(
                    execution.stdout if isinstance(execution.stdout, str) else "",
                    encoding="utf-8",
                )
                Path(str(case["stderr_path"])).write_text(
                    execution.stderr if isinstance(execution.stderr, str) else "",
                    encoding="utf-8",
                )
        elapsed_ms = 0
        results = {
            str(case["program_id"]): finalize_batch_case_result(case=case, elapsed_ms=elapsed_ms)
            for case in prepared_cases
        }
        return results, None, {str(case["program_id"]): None for case in prepared_cases}

    package_dir, slot_by_program = prepare_candidate_initramfs_package(prepared_cases, cfg, batch_metadata=batch_metadata)
    prewarm_packaged_candidate_bundle(prepared_cases, package_dir, cfg)
    if max_workers is None:
        selected_workers = int(cfg.get("parallel", {}).get("jobs", 1))
    else:
        selected_workers = max_workers
    max_workers = max(1, min(len(prepared_cases), selected_workers))
    if max_workers == 1:
        return (
            {
                str(case["program_id"]): execute_prepared_candidate_case(
                    case=case,
                    package_dir=package_dir,
                    slot=slot_by_program[str(case["program_id"])],
                )
                for case in prepared_cases
            },
            package_dir,
            slot_by_program,
        )

    results: dict[str, RunResult] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(
                execute_prepared_candidate_case,
                case=case,
                package_dir=package_dir,
                slot=slot_by_program[str(case["program_id"])],
            ): str(case["program_id"])
            for case in prepared_cases
        }
        for future in concurrent.futures.as_completed(future_map):
            results[future_map[future]] = future.result()
    return results, package_dir, slot_by_program


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
    env["SYZABI_TRACE_EVENTS_PATH"] = trace_events_destination(cfg=cfg, events_path=events_path)
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
    runner = build_runner(profile)
    execution = runner.run_case(
        command=command,
        cwd=str(sandbox_root),
        env=env,
        timeout_sec=effective_timeout_sec,
    )
    try:
        stdout = execution.stdout
        stderr = execution.stderr
        fallback_kernel_build = safe_kernel_build(profile["kernel_build_command"])
        status, exit_code, status_detail, kernel_build_value = finalize_process_result(
            profile_kind=runner_kind,
            completed_returncode=execution.returncode,
            runner_result=load_runner_result(runner_result_path),
            fallback_kernel_build=fallback_kernel_build,
        )
        if execution.timed_out:
            raise subprocess.TimeoutExpired(command, effective_timeout_sec, output=stdout, stderr=stderr)
        if execution.os_error is not None:
            raise OSError(execution.os_error)
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
        events = persisted_trace_events(
            cfg=cfg,
            events_path=events_path,
            stdout_text=stdout,
            stderr_text=stderr,
            console_path=console_path,
        )
        raw_trace = {
            "program_id": program_id,
            "side": side,
            "run_id": run_id,
            "status": status,
            "events": events,
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
