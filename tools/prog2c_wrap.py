#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, env_with_temp, load_jsonl, report_path, resolve_repo_path, resolved_config_path, runner_profiles, write_text
from orchestrator.syzkaller import build_prog2c


SYSCALL_PATTERN = re.compile(r"(?P<prefix>.*?)(?P<call>\bsyscall\s*\((?P<body>.*)\)\s*;)\s*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--eligible-file")
    parser.add_argument("--program-id")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    return parser.parse_args()


def split_args(body: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    depth = 0
    for char in body:
        if char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        current.append(char)
    if current:
        args.append("".join(current).strip())
    return args


def syscall_name(nr_expr: str) -> str:
    for prefix in ("__NR_", "SYS_"):
        if nr_expr.startswith(prefix):
            return nr_expr[len(prefix) :]
    return nr_expr


def inject_header(source: str) -> str:
    lines = source.splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        if line.startswith("#include"):
            insert_at = index + 1
    lines.insert(insert_at, '#include "trace.h"')
    return "\n".join(lines) + "\n"


def instrument_source(source: str) -> tuple[str, int]:
    lines = inject_header(source).splitlines()
    wrapped = 0
    output: list[str] = []
    for line in lines:
        match = SYSCALL_PATTERN.match(line)
        if not match:
            output.append(line)
            continue
        args = split_args(match.group("body"))
        nr_expr = args[0]
        call_args = args[1:]
        while len(call_args) < 6:
            call_args.append("0")
        if len(call_args) > 6:
            raise ValueError(f"unexpected syscall arity for {nr_expr}: {len(call_args)}")
        replacement = (
            f'{match.group("prefix")}traced_syscall("{syscall_name(nr_expr)}", {nr_expr}, {wrapped}, '
            + ", ".join(call_args[:6])
            + ");"
        )
        output.append(replacement)
        wrapped += 1
    instrumented = "\n".join(output) + "\n"
    if re.search(r"\bsyscall\s*\(", instrumented):
        raise ValueError("raw syscall invocation remains after instrumentation")
    return instrumented, wrapped


def compile_testcase(
    instrumented_path: Path,
    binary_path: Path,
    *,
    runner_source: str,
) -> subprocess.CompletedProcess[str]:
    extra_cflags = config().get("build", {}).get("cflags", [])
    cmd = [
        "gcc",
        "-O2",
        "-std=gnu11",
        "-Wall",
        "-Wextra",
        "-Wno-unused-result",
        "-Wno-unused-function",
        *extra_cflags,
        "-I",
        "agent/linux",
        str(instrumented_path),
        "agent/linux/trace.c",
        runner_source,
        "-o",
        str(binary_path),
    ]
    return subprocess.run(cmd, check=False, text=True, capture_output=True, env=env_with_temp())


def should_build_candidate_binary(candidate_profile: dict[str, object]) -> bool:
    candidate_binary_name = str(candidate_profile.get("binary_name", "testcase.bin"))
    return candidate_binary_name != "testcase.bin" or candidate_profile.get("kind") == "command"


def build_input_paths(
    normalized_path: Path,
    *,
    cfg: dict[str, object],
    should_build_candidate: bool,
) -> list[Path]:
    paths = [
        normalized_path,
        Path(__file__).resolve(),
        resolve_repo_path("orchestrator/syzkaller.py"),
        resolved_config_path(),
        resolve_repo_path(cfg.get("runner_profiles_path", "configs/runner_profiles.json")),
        resolve_repo_path(cfg["paths"]["syzkaller_dir"]) / "bin" / "syz-prog2c",
        resolve_repo_path("agent/linux/trace.c"),
        resolve_repo_path("agent/linux/runner.c"),
    ]
    if should_build_candidate:
        paths.append(resolve_repo_path("agent/asterinas/runner.c"))
    return paths


def input_fingerprints(
    paths: list[Path],
    *,
    extra_entries: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    fingerprints = [
        {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in paths
    ]
    if extra_entries:
        fingerprints.extend(extra_entries)
    return fingerprints


def syzkaller_revision_fingerprint(cfg: dict[str, object]) -> dict[str, str] | None:
    syzkaller_root = resolve_repo_path(cfg["paths"]["syzkaller_dir"])
    try:
        result = subprocess.run(
            ["git", "-C", str(syzkaller_root), "rev-parse", "HEAD"],
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    revision = result.stdout.strip()
    if not revision:
        return None
    return {
        "path": f"{syzkaller_root}::git-revision",
        "sha256": hashlib.sha256(revision.encode("utf-8")).hexdigest(),
    }


def build_input_fingerprints(
    input_paths: list[Path],
    *,
    cfg: dict[str, object],
) -> list[dict[str, str]]:
    extra_entries: list[dict[str, str]] = []
    revision_fingerprint = syzkaller_revision_fingerprint(cfg)
    if revision_fingerprint is not None:
        extra_entries.append(revision_fingerprint)
    return input_fingerprints(input_paths, extra_entries=extra_entries)


def load_cached_build_result(
    build_root: Path,
    *,
    input_paths: list[Path],
    should_build_candidate: bool,
    expected_fingerprints: list[dict[str, str]] | None = None,
) -> dict[str, object] | None:
    build_result_path = build_root / "build-result.json"
    if not build_result_path.exists():
        return None
    try:
        cached = json.loads(build_result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if cached.get("status") != "ok":
        return None
    output_paths = [
        Path(str(cached.get("testcase_c", ""))),
        Path(str(cached.get("testcase_instrumented_c", ""))),
        Path(str(cached.get("testcase_bin", ""))),
    ]
    if should_build_candidate:
        candidate_bin = cached.get("candidate_testcase_bin")
        if not candidate_bin:
            return None
        output_paths.append(Path(str(candidate_bin)))
    if any(not path.exists() for path in output_paths):
        return None
    if any(not path.exists() for path in input_paths):
        return None
    fingerprints = expected_fingerprints if expected_fingerprints is not None else input_fingerprints(input_paths)
    if cached.get("input_fingerprints") != fingerprints:
        return None
    newest_input = max(path.stat().st_mtime_ns for path in input_paths)
    oldest_output = min(path.stat().st_mtime_ns for path in [build_result_path, *output_paths])
    if oldest_output < newest_input:
        return None
    return cached


def build_one(entry: dict[str, object]) -> dict[str, object]:
    cfg = config()
    candidate_profile = runner_profiles()["candidate"]
    program_id = entry["program_id"]
    normalized_path = Path(entry["normalized_path"])
    build_root = Path(cfg["paths"]["build_dir"]) / program_id
    build_root.mkdir(parents=True, exist_ok=True)
    should_build_candidate = should_build_candidate_binary(candidate_profile)
    build_inputs = build_input_paths(
        normalized_path,
        cfg=cfg,
        should_build_candidate=should_build_candidate,
    )
    build_fingerprints = build_input_fingerprints(
        build_inputs,
        cfg=cfg,
    )
    cached_result = load_cached_build_result(
        build_root,
        input_paths=build_inputs,
        should_build_candidate=should_build_candidate,
        expected_fingerprints=build_fingerprints,
    )
    if cached_result is not None:
        return cached_result
    prog2c_result = build_prog2c(normalized_path)
    testcase_c = build_root / "testcase.c"
    testcase_instrumented = build_root / "testcase.instrumented.c"
    testcase_bin = build_root / "testcase.bin"
    candidate_bin = build_root / "testcase.candidate.bin"
    prog2c_stderr = build_root / "prog2c.stderr.txt"
    compile_stderr = build_root / "compile.reference.stderr.txt"
    candidate_compile_stderr = build_root / "compile.candidate.stderr.txt"
    prog2c_stderr.write_text(prog2c_result.stderr, encoding="utf-8")

    if prog2c_result.returncode != 0:
        result = {
            "program_id": program_id,
            "status": "build_failure",
            "stage": "prog2c",
            "returncode": prog2c_result.returncode,
            "stderr_path": str(prog2c_stderr),
        }
        dump_json(build_root / "build-result.json", result)
        return result

    testcase_c.write_text(prog2c_result.stdout, encoding="utf-8")
    instrumented_source, wrapped_count = instrument_source(prog2c_result.stdout)
    testcase_instrumented.write_text(instrumented_source, encoding="utf-8")
    compile_result = compile_testcase(
        testcase_instrumented,
        testcase_bin,
        runner_source="agent/linux/runner.c",
    )
    compile_stderr.write_text(compile_result.stderr, encoding="utf-8")
    candidate_compile_result: subprocess.CompletedProcess[str] | None = None
    if should_build_candidate:
        candidate_compile_result = compile_testcase(
            testcase_instrumented,
            candidate_bin,
            runner_source="agent/asterinas/runner.c",
        )
        candidate_compile_stderr.write_text(candidate_compile_result.stderr, encoding="utf-8")

    status = "ok" if compile_result.returncode == 0 else "build_failure"
    if candidate_compile_result is not None and candidate_compile_result.returncode != 0:
        status = "build_failure"
    result = {
        "program_id": program_id,
        "normalized_path": str(normalized_path),
        "status": status,
        "stage": "compile" if status != "ok" else "done",
        "returncode": compile_result.returncode if status == "ok" or candidate_compile_result is None else candidate_compile_result.returncode,
        "wrapped_syscalls": wrapped_count,
        "testcase_c": str(testcase_c),
        "testcase_instrumented_c": str(testcase_instrumented),
        "testcase_bin": str(testcase_bin),
        "candidate_testcase_bin": str(candidate_bin) if candidate_compile_result is not None else None,
        "prog2c_stderr": str(prog2c_stderr),
        "compile_stderr": str(compile_stderr),
        "candidate_compile_stderr": str(candidate_compile_stderr) if candidate_compile_result is not None else None,
        "input_fingerprints": build_fingerprints,
    }
    dump_json(build_root / "build-result.json", result)
    return result


def selected_entries(args: argparse.Namespace) -> list[dict[str, object]]:
    rows = load_jsonl(args.eligible_file)
    if args.program_id:
        rows = [row for row in rows if row["program_id"] == args.program_id]
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    if not args.eligible_file:
        args.eligible_file = cfg["paths"]["eligible_file"]
    entries = selected_entries(args)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        results = list(executor.map(build_one, entries))
    dump_json(
        report_path("build-summary.json", cfg=cfg),
        {
            "total": len(results),
            "success": sum(1 for result in results if result["status"] == "ok"),
            "failed": sum(1 for result in results if result["status"] != "ok"),
        },
    )


if __name__ == "__main__":
    main()
