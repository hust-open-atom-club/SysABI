#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.capability import AsterinasSCMLGate, AsterinasSCMLSource, parse_sctrace_lines as capability_parse_sctrace_lines
from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, ensure_dir, env_with_temp, load_json, load_jsonl, report_path, resolve_repo_path
from orchestrator.models import EligibleProgram
from tools.prog2c_wrap import build_one


def parse_sctrace_lines(stdout: str, stderr: str) -> list[str]:
    return capability_parse_sctrace_lines(stdout, stderr)


def classify_sctrace_line(
    line: str,
    manifest_index: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    *,
    target_syscalls: set[str] | None = None,
) -> list[str]:
    gate = AsterinasSCMLGate(cfg=cfg, manifest_index=manifest_index)
    return gate.classify_line(line, target_syscalls=target_syscalls)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    parser.add_argument("--source-eligible-file")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--program-id")
    parser.add_argument("--jobs", type=int)
    return parser.parse_args()


def selected_entries(args: argparse.Namespace, source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = source_rows
    if args.program_id:
        rows = [row for row in rows if row["program_id"] == args.program_id]
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


def is_filtered_run(args: argparse.Namespace) -> bool:
    return bool(args.program_id or args.limit is not None)


def filtered_run_label(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.program_id:
        parts.append(f"program-{args.program_id[:16]}")
    if args.limit is not None:
        parts.append(f"limit-{args.limit}")
    return "-".join(parts) or "filtered"


def output_targets(args: argparse.Namespace, cfg: dict[str, Any]) -> dict[str, Path]:
    if not is_filtered_run(args):
        return {
            "eligible_file": resolve_repo_path(cfg["paths"]["eligible_file"]),
            "rejections_file": report_path("scml-rejections.jsonl", cfg=cfg),
            "summary_file": report_path("preflight-summary.json", cfg=cfg),
        }
    debug_root = ensure_dir(report_path("debug-preflight", filtered_run_label(args), cfg=cfg))
    return {
        "eligible_file": debug_root / "eligible.jsonl",
        "rejections_file": debug_root / "scml-rejections.jsonl",
        "summary_file": debug_root / "preflight-summary.json",
    }


def evidence_root(args: argparse.Namespace, cfg: dict[str, Any], program_id: str) -> Path:
    base = resolve_repo_path(cfg["preflight"]["artifact_dir"])
    if not is_filtered_run(args):
        return ensure_dir(base / program_id)
    return ensure_dir(base / "debug" / filtered_run_label(args) / program_id)


def restore_artifact_root_permissions(path: Path) -> None:
    # Some generated programs intentionally mutate cwd metadata; make sure the
    # evidence directory remains writable so preflight can persist its outputs.
    try:
        path.chmod(0o755)
    except FileNotFoundError:
        return


def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_sec: int,
) -> dict[str, Any]:
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_sec)
        return {
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return {
            "returncode": None,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "timed_out": True,
        }


def run_preflight(
    entry: dict[str, Any],
    *,
    args: argparse.Namespace,
    cfg: dict[str, Any],
    gate: AsterinasSCMLGate,
    source: AsterinasSCMLSource,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    taxonomy = cfg["preflight"]["rejection_taxonomy"]
    timeout_sec = int(cfg["preflight"].get("timeout_sec", cfg["stability"]["timeout_sec"]))
    build_result = build_one(entry)
    artifact_root = evidence_root(args, cfg, entry["program_id"])
    strace_log_path = artifact_root / "preflight.strace.log"
    sctrace_output_path = artifact_root / "preflight.sctrace.txt"
    strace_stdout_path = artifact_root / "preflight.strace.stdout.txt"
    strace_stderr_path = artifact_root / "preflight.strace.stderr.txt"
    sctrace_stdout_path = artifact_root / "preflight.sctrace.stdout.txt"
    sctrace_stderr_path = artifact_root / "preflight.sctrace.stderr.txt"

    if build_result["status"] != "ok":
        rejected = {
            "program_id": entry["program_id"],
            "workflow": cfg["workflow"],
            "source_workflow": entry.get("source_workflow", ""),
            "source_program_id": entry.get("source_program_id", ""),
            "normalized_path": entry["normalized_path"],
            "meta_path": entry["meta_path"],
            "scml_preflight_status": "rejected_by_scml",
            "scml_rejection_reasons": [taxonomy["preflight_build_failure"]],
            "scml_trace_log_path": str(strace_log_path),
            "scml_sctrace_output_path": str(sctrace_output_path),
            "scml_preflight_run_root": str(artifact_root),
        }
        return None, rejected

    binary_path = resolve_repo_path(build_result["testcase_bin"])
    preflight_run = run_command(
        ["strace", "-yy", "-f", "-o", str(strace_log_path), str(binary_path)],
        cwd=artifact_root,
        env=env_with_temp(cfg=cfg),
        timeout_sec=timeout_sec,
    )
    restore_artifact_root_permissions(artifact_root)
    strace_stdout_path.write_text(preflight_run["stdout"], encoding="utf-8")
    strace_stderr_path.write_text(preflight_run["stderr"], encoding="utf-8")
    if preflight_run["timed_out"]:
        rejected = {
            "program_id": entry["program_id"],
            "workflow": cfg["workflow"],
            "source_workflow": entry.get("source_workflow", ""),
            "source_program_id": entry.get("source_program_id", ""),
            "normalized_path": entry["normalized_path"],
            "meta_path": entry["meta_path"],
            "scml_preflight_status": "rejected_by_scml",
            "scml_rejection_reasons": [taxonomy["preflight_runtime_timeout"]],
            "scml_trace_log_path": str(strace_log_path),
            "scml_sctrace_output_path": str(sctrace_output_path),
            "scml_preflight_run_root": str(artifact_root),
        }
        return None, rejected
    if preflight_run["returncode"] != 0:
        rejected = {
            "program_id": entry["program_id"],
            "workflow": cfg["workflow"],
            "source_workflow": entry.get("source_workflow", ""),
            "source_program_id": entry.get("source_program_id", ""),
            "normalized_path": entry["normalized_path"],
            "meta_path": entry["meta_path"],
            "scml_preflight_status": "rejected_by_scml",
            "scml_rejection_reasons": [taxonomy.get("preflight_runtime_failure", "preflight_runtime_failure")],
            "scml_trace_log_path": str(strace_log_path),
            "scml_sctrace_output_path": str(sctrace_output_path),
            "scml_preflight_run_root": str(artifact_root),
        }
        return None, rejected

    sctrace_run = run_command(
        source.sctrace_command(source.scml_files(), strace_log_path),
        cwd=resolve_repo_path("."),
        env=env_with_temp(cfg=cfg),
        timeout_sec=timeout_sec,
    )
    sctrace_stdout_path.write_text(sctrace_run["stdout"], encoding="utf-8")
    sctrace_stderr_path.write_text(sctrace_run["stderr"], encoding="utf-8")
    if sctrace_run["timed_out"]:
        rejected = {
            "program_id": entry["program_id"],
            "workflow": cfg["workflow"],
            "source_workflow": entry.get("source_workflow", ""),
            "source_program_id": entry.get("source_program_id", ""),
            "normalized_path": entry["normalized_path"],
            "meta_path": entry["meta_path"],
            "scml_preflight_status": "rejected_by_scml",
            "scml_rejection_reasons": [taxonomy["preflight_matcher_timeout"]],
            "scml_trace_log_path": str(strace_log_path),
            "scml_sctrace_output_path": str(sctrace_output_path),
            "scml_preflight_run_root": str(artifact_root),
        }
        return None, rejected
    meta = load_json(entry["meta_path"])
    target_syscalls = {full_name.split("$", 1)[0] for full_name in meta["full_syscall_list"]}
    output_lines = gate.parse_sctrace_lines(sctrace_run["stdout"], sctrace_run["stderr"])
    relevant_output_lines = gate.relevant_output_lines(output_lines, target_syscalls=target_syscalls)
    sctrace_output_path.write_text(
        "\n".join(relevant_output_lines) + ("\n" if relevant_output_lines else ""),
        encoding="utf-8",
    )

    reasons: list[str] = []
    for line in relevant_output_lines:
        reasons.extend(gate.classify_line(line, target_syscalls=target_syscalls))
    if sctrace_run["returncode"] != 0:
        reasons.append(taxonomy["scml_parser_gap"])
    reasons = list(dict.fromkeys(reasons))

    if reasons:
        rejected = {
            "program_id": entry["program_id"],
            "workflow": cfg["workflow"],
            "source_workflow": entry.get("source_workflow", ""),
            "source_program_id": entry.get("source_program_id", ""),
            "normalized_path": entry["normalized_path"],
            "meta_path": entry["meta_path"],
            "scml_preflight_status": "rejected_by_scml",
            "scml_rejection_reasons": reasons,
            "scml_trace_log_path": str(strace_log_path),
            "scml_sctrace_output_path": str(sctrace_output_path),
            "scml_preflight_run_root": str(artifact_root),
        }
        return None, rejected

    accepted = EligibleProgram(
        program_id=entry["program_id"],
        workflow=cfg["workflow"],
        reason=list(entry.get("reason", [])) + ["runtime_scml_preflight_passed"],
        normalized_path=entry["normalized_path"],
        meta_path=entry["meta_path"],
        source_workflow=entry.get("source_workflow", ""),
        source_program_id=entry.get("source_program_id", ""),
        scml_preflight_status="passed",
        scml_rejection_reasons=[],
        scml_trace_log_path=str(strace_log_path),
        scml_sctrace_output_path=str(sctrace_output_path),
        scml_preflight_run_root=str(artifact_root),
    ).to_dict()
    return accepted, None


def effective_jobs(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    if args.jobs is not None:
        return max(1, args.jobs)
    parallel = cfg.get("parallel", {})
    if isinstance(parallel, dict):
        return max(1, int(parallel.get("jobs", 1)))
    return 1


def run_preflight_entries(
    entries: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    cfg: dict[str, Any],
    gate: AsterinasSCMLGate,
    source: AsterinasSCMLSource,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    jobs = effective_jobs(args, cfg)

    def task(entry: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        return run_preflight(entry, args=args, cfg=cfg, gate=gate, source=source)

    if jobs <= 1 or len(entries) <= 1:
        for entry in entries:
            accepted, rejected = task(entry)
            if accepted is not None:
                accepted_rows.append(accepted)
            if rejected is not None:
                rejected_rows.append(rejected)
        return accepted_rows, rejected_rows

    results: list[tuple[dict[str, Any] | None, dict[str, Any] | None] | None] = [None] * len(entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        future_map = {
            executor.submit(task, entry): index
            for index, entry in enumerate(entries)
        }
        for future in concurrent.futures.as_completed(future_map):
            results[future_map[future]] = future.result()

    for result in results:
        if result is None:
            continue
        accepted, rejected = result
        if accepted is not None:
            accepted_rows.append(accepted)
        if rejected is not None:
            rejected_rows.append(rejected)
    return accepted_rows, rejected_rows


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    source = AsterinasSCMLSource(cfg)
    gate = AsterinasSCMLGate(cfg=cfg, manifest_index=source.load_manifest_index())
    source_eligible_file = args.source_eligible_file or cfg["preflight"]["source_eligible_file"]
    source_rows = selected_entries(args, load_jsonl(source_eligible_file))
    targets = output_targets(args, cfg)
    accepted_rows, rejected_rows = run_preflight_entries(
        source_rows,
        args=args,
        cfg=cfg,
        gate=gate,
        source=source,
    )

    accepted_rows.sort(key=lambda row: row["program_id"])
    rejected_rows.sort(key=lambda row: row["program_id"])
    dump_jsonl(targets["eligible_file"], accepted_rows)
    dump_jsonl(targets["rejections_file"], rejected_rows)
    dump_json(
        targets["summary_file"],
        {
            "workflow": cfg["workflow"],
            "source_eligible_file": source_eligible_file,
            "source_total": len(source_rows),
            "eligible": len(accepted_rows),
            "rejected": len(rejected_rows),
            "filtered_run": is_filtered_run(args),
        },
    )


if __name__ == "__main__":
    main()
