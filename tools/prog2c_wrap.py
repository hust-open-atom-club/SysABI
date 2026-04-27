#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.constants import Classification, ExecutionStatus
from core.paths import resolve_compiler_path
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
    source = inject_header(source)
    calls: list[tuple[int, int, str, str]] = []
    i = 0
    while i < len(source):
        idx = source.find("syscall(", i)
        if idx == -1:
            break
        if idx > 0 and (source[idx - 1].isalnum() or source[idx - 1] == "_"):
            i = idx + 1
            continue
        start = idx
        paren_depth = 1
        j = idx + len("syscall(")
        while j < len(source) and paren_depth > 0:
            if source[j] == "(":
                paren_depth += 1
            elif source[j] == ")":
                paren_depth -= 1
            j += 1
        k = j
        while k < len(source) and source[k] in " \t\n":
            k += 1
        if k < len(source) and source[k] == ";":
            end = k + 1
            line_start = source.rfind("\n", 0, start) + 1
            prefix = source[line_start:start]
            body = source[start + len("syscall("):j - 1]
            calls.append((start, end, prefix, body))
            i = end
        else:
            i = j
    parts: list[str] = []
    last_end = 0
    for idx, (start, end, prefix, body) in enumerate(calls):
        parts.append(source[last_end:start])
        args = split_args(body)
        nr_expr = args[0]
        call_args = args[1:]
        while len(call_args) < 6:
            call_args.append("0")
        if len(call_args) > 6:
            raise ValueError(f"unexpected syscall arity for {nr_expr}: {len(call_args)}")
        replacement = (
            f'{prefix}traced_syscall("{syscall_name(nr_expr)}", {nr_expr}, {idx}, '
            + ", ".join(call_args[:6])
            + ");"
        )
        parts.append(replacement)
        last_end = end
    parts.append(source[last_end:])
    instrumented = "".join(parts)
    if re.search(r"\bsyscall\s*\(", instrumented):
        raise ValueError("raw syscall invocation remains after instrumentation")
    return instrumented, len(calls)


def build_side_config(cfg: dict[str, object], *, side: str) -> dict[str, object]:
    build_cfg = cfg.get("build", {})
    if not isinstance(build_cfg, dict):
        return {}
    side_cfg = build_cfg.get(side, {})
    if not isinstance(side_cfg, dict):
        return {}
    return side_cfg


def runner_source_for_side(cfg: dict[str, object], *, side: str) -> str:
    side_cfg = build_side_config(cfg, side=side)
    runner_source = side_cfg.get("runner_source")
    if isinstance(runner_source, str) and runner_source:
        return runner_source
    if side == "candidate" and str(cfg.get("target", "")) == "asterinas":
        return "agent/asterinas/runner.c"
    return "agent/linux/runner.c"


def compiler_for_side(cfg: dict[str, object], *, side: str) -> str:
    build_cfg = cfg.get("build", {})
    side_cfg = build_side_config(cfg, side=side)
    arch = str(cfg.get("arch", "amd64"))

    supported_arches = side_cfg.get("supported_arches")
    if isinstance(supported_arches, list) and supported_arches and arch not in {str(item) for item in supported_arches}:
        raise ValueError(f"{side} build does not support arch={arch}")

    compiler_by_arch = side_cfg.get("compiler_by_arch")
    if isinstance(compiler_by_arch, dict) and compiler_by_arch:
        compiler = compiler_by_arch.get(arch)
        if not isinstance(compiler, str) or not compiler:
            raise ValueError(f"{side} build is missing compiler_by_arch entry for arch={arch}")
        return compiler

    if isinstance(side_cfg.get("compiler"), str) and side_cfg["compiler"]:
        return str(side_cfg["compiler"])
    if isinstance(build_cfg, dict) and isinstance(build_cfg.get("compiler"), str) and build_cfg["compiler"]:
        return str(build_cfg["compiler"])
    return "gcc"


def cflags_for_side(cfg: dict[str, object], *, side: str) -> list[str]:
    build_cfg = cfg.get("build", {})
    base = list(build_cfg.get("cflags", [])) if isinstance(build_cfg, dict) else []
    side_cfg = build_side_config(cfg, side=side)
    side_flags = side_cfg.get("cflags", [])
    if isinstance(side_flags, list):
        base.extend(str(flag) for flag in side_flags)
    return [str(flag) for flag in base]


def compile_testcase(
    instrumented_path: Path,
    binary_path: Path,
    *,
    cfg: dict[str, object],
    side: str,
) -> subprocess.CompletedProcess[str]:
    compiler = compiler_for_side(cfg, side=side)
    resolved = resolve_compiler_path(compiler)
    if resolved is None:
        return subprocess.CompletedProcess(
            [compiler],
            127,
            "",
            f"missing compiler for {side} build: {compiler}",
        )
    compiler = resolved
    runner_source = runner_source_for_side(cfg, side=side)
    extra_cflags = cflags_for_side(cfg, side=side)
    cmd = [
        compiler,
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
    paths: list[Path] = [
        normalized_path,
        Path(__file__).resolve(),
        resolve_repo_path("orchestrator/syzkaller.py"),
        resolved_config_path(),
        resolve_repo_path(cfg.get("runner_profiles_path", "configs/runner_profiles.json")),
        resolve_repo_path(cfg["paths"]["syzkaller_dir"]) / "bin" / "syz-prog2c",
        resolve_repo_path("agent/linux/trace.c"),
    ]
    seen = {str(path.resolve()) for path in paths}
    for source in (
        runner_source_for_side(cfg, side="reference"),
        runner_source_for_side(cfg, side="candidate") if should_build_candidate else None,
    ):
        if source is None:
            continue
        resolved = resolve_repo_path(source)
        key = str(resolved.resolve())
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    if should_build_candidate:
        side_cfg = build_side_config(cfg, side="candidate")
        compiler = side_cfg.get("compiler")
        if isinstance(compiler, str) and compiler and (compiler.startswith("/") or Path(compiler).exists()):
            paths.append(Path(compiler))
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
    if cached.get("status") != ExecutionStatus.OK:
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
            "status": Classification.BUILD_FAILURE,
            "stage": "prog2c",
            "returncode": prog2c_result.returncode,
            "stderr_path": str(prog2c_stderr),
        }
        dump_json(build_root / "build-result.json", result)
        return result

    testcase_c.write_text(prog2c_result.stdout, encoding="utf-8")
    instrumented_source, wrapped_count = instrument_source(prog2c_result.stdout)
    testcase_instrumented.write_text(instrumented_source, encoding="utf-8")
    try:
        compile_result = compile_testcase(
            testcase_instrumented,
            testcase_bin,
            cfg=cfg,
            side="reference",
        )
        compile_stderr.write_text(compile_result.stderr, encoding="utf-8")
        candidate_compile_result: subprocess.CompletedProcess[str] | None = None
        if should_build_candidate:
            candidate_compile_result = compile_testcase(
                testcase_instrumented,
                candidate_bin,
                cfg=cfg,
                side="candidate",
            )
            candidate_compile_stderr.write_text(candidate_compile_result.stderr, encoding="utf-8")
    except ValueError as exc:
        compile_stderr.write_text(str(exc), encoding="utf-8")
        result = {
            "program_id": program_id,
            "normalized_path": str(normalized_path),
            "status": Classification.BUILD_FAILURE,
            "stage": "compile",
            "returncode": 1,
            "wrapped_syscalls": wrapped_count,
            "testcase_c": str(testcase_c),
            "testcase_instrumented_c": str(testcase_instrumented),
            "testcase_bin": str(testcase_bin),
            "candidate_testcase_bin": str(candidate_bin) if should_build_candidate else None,
            "prog2c_stderr": str(prog2c_stderr),
            "compile_stderr": str(compile_stderr),
            "candidate_compile_stderr": str(candidate_compile_stderr) if should_build_candidate else None,
            "input_fingerprints": build_fingerprints,
        }
        dump_json(build_root / "build-result.json", result)
        return result

    status = ExecutionStatus.OK if compile_result.returncode == 0 else Classification.BUILD_FAILURE
    if candidate_compile_result is not None and candidate_compile_result.returncode != 0:
        status = Classification.BUILD_FAILURE
    result = {
        "program_id": program_id,
        "normalized_path": str(normalized_path),
        "status": status,
        "stage": "compile" if status != ExecutionStatus.OK else "done",
        "returncode": compile_result.returncode if status == ExecutionStatus.OK or candidate_compile_result is None else candidate_compile_result.returncode,
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
            "success": sum(1 for result in results if result["status"] == ExecutionStatus.OK),
            "failed": sum(1 for result in results if result["status"] != ExecutionStatus.OK),
        },
    )


if __name__ == "__main__":
    main()
