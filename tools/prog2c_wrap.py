#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, load_jsonl, report_path, write_text
from orchestrator.syzkaller import build_prog2c


SYSCALL_PATTERN = re.compile(r"(?P<prefix>.*?)(?P<call>\bsyscall\s*\((?P<body>.*)\)\s*;)\s*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="phase1")
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
        "agent/linux/runner.c",
        "-o",
        str(binary_path),
    ]
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def build_one(entry: dict[str, object]) -> dict[str, object]:
    cfg = config()
    program_id = entry["program_id"]
    normalized_path = Path(entry["normalized_path"])
    build_root = Path(cfg["paths"]["build_dir"]) / program_id
    build_root.mkdir(parents=True, exist_ok=True)
    prog2c_result = build_prog2c(normalized_path)
    testcase_c = build_root / "testcase.c"
    testcase_instrumented = build_root / "testcase.instrumented.c"
    testcase_bin = build_root / "testcase.bin"
    prog2c_stderr = build_root / "prog2c.stderr.txt"
    compile_stderr = build_root / "compile.reference.stderr.txt"
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
    compile_result = compile_testcase(testcase_instrumented, testcase_bin)
    compile_stderr.write_text(compile_result.stderr, encoding="utf-8")
    status = "ok" if compile_result.returncode == 0 else "build_failure"
    result = {
        "program_id": program_id,
        "status": status,
        "stage": "compile" if status != "ok" else "done",
        "returncode": compile_result.returncode,
        "wrapped_syscalls": wrapped_count,
        "testcase_c": str(testcase_c),
        "testcase_instrumented_c": str(testcase_instrumented),
        "testcase_bin": str(testcase_bin),
        "prog2c_stderr": str(prog2c_stderr),
        "compile_stderr": str(compile_stderr),
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
    configure_runtime(phase=args.phase)
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
