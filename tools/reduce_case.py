#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.compare import compare_canonical
from analyzer.normalize import canonicalize
from orchestrator.common import config, configure_runtime, dump_json, load_json, load_jsonl, read_text, report_path, runner_profiles, temp_dir, write_text
from orchestrator.syzkaller import inspect_program, mutate_drop_call
from orchestrator.vm_runner import execute_side
from tools.prog2c_wrap import build_one


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--fixture", default="controlled_divergence")
    return parser.parse_args()


def divergence_spec() -> dict[str, object]:
    candidate = runner_profiles()["candidate"]
    spec = candidate.get("controlled_divergence", {}).copy()
    if not spec:
        raise SystemExit("controlled divergence is not configured for this workflow")
    spec["enabled"] = True
    spec["call_index"] = -1
    spec["syscall_name"] = spec.pop("match_syscall")
    return spec


def map_event_index_to_program_call(canonical_trace: dict[str, object], event_index: int | None) -> int | None:
    if event_index is None:
        return None
    runtime_syscalls = set(config()["normalization"]["runtime_syscalls"])
    call_index = 0
    for event in canonical_trace["events"]:
        if event["index"] == event_index:
            if event["syscall_name"] in runtime_syscalls:
                return None
            return call_index
        if event["syscall_name"] in runtime_syscalls:
            continue
        call_index += 1
    return None


def run_case(program_path: Path) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    info = inspect_program(program_path)
    entry = {
        "program_id": info["program_id"],
        "normalized_path": str(program_path),
    }
    build_one(entry)
    run_id = f"reduce-{info['program_id'][:12]}-{time.time_ns()}"
    reference = execute_side(program_id=info["program_id"], side="reference", timeout_sec=config()["stability"]["timeout_sec"], run_id=f"{run_id}-reference")
    candidate = execute_side(
        program_id=info["program_id"],
        side="candidate",
        timeout_sec=config()["stability"]["timeout_sec"],
        run_id=f"{run_id}-candidate",
        inject_trace=divergence_spec(),
    )
    reference_canonical = canonicalize(load_json(reference.trace_json_path), load_json(reference.external_state_path))
    candidate_canonical = canonicalize(load_json(candidate.trace_json_path), load_json(candidate.external_state_path))
    reference_canonical_path = Path(reference.trace_json_path).with_name("canonical-trace.json")
    candidate_canonical_path = Path(candidate.trace_json_path).with_name("canonical-trace.json")
    dump_json(reference_canonical_path, reference_canonical)
    dump_json(candidate_canonical_path, candidate_canonical)
    comparison = compare_canonical(reference_canonical, candidate_canonical)
    return info, comparison, {
        "reference": reference.to_dict(),
        "candidate": candidate.to_dict(),
        "reference_canonical": reference_canonical,
        "candidate_canonical": candidate_canonical,
        "reference_canonical_path": str(reference_canonical_path),
        "candidate_canonical_path": str(candidate_canonical_path),
    }


def seed_program(fixture_name: str) -> tuple[Path, dict[str, object] | None]:
    cfg = config()
    if cfg["workflow"] == "asterinas_scml":
        eligible = load_jsonl(cfg["paths"]["eligible_file"])
        if eligible:
            return Path(eligible[0]["normalized_path"]), eligible[0]
    fixture = Path("tests/fixtures/corpus") / f"{fixture_name}.syz"
    if fixture.exists():
        return fixture, None
    eligible = load_jsonl(cfg["paths"]["eligible_file"])
    if not eligible:
        raise SystemExit(f"{cfg['paths']['eligible_file']} is empty")
    return Path(eligible[0]["normalized_path"]), eligible[0]


def greedy_reduce(initial_program: Path) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object]]:
    cfg = config()
    current_text = read_text(initial_program)
    current_info, current_comparison, current_runs = run_case(initial_program)
    changed = True
    with tempfile.TemporaryDirectory(dir=temp_dir()) as tempdir:
        tempdir_path = Path(tempdir)
        while changed:
            changed = False
            call_count = current_info["call_count"]
            for drop_index in range(call_count - 1, -1, -1):
                trial_path = tempdir_path / f"drop-{drop_index}.syz"
                write_text(trial_path, mutate_drop_call(initial_program, drop_index))
                trial_info, trial_comparison, trial_runs = run_case(trial_path)
                if not trial_comparison["equivalent"]:
                    current_text = read_text(trial_path)
                    current_info = trial_info
                    current_comparison = trial_comparison
                    current_runs = trial_runs
                    initial_program = trial_path
                    changed = True
                    break
        final_path = report_path(f"{current_info['program_id']}-minimized.syz", cfg=cfg)
        write_text(final_path, current_text)
        return final_path, current_info, current_comparison, current_runs


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    source_program, source_entry = seed_program(args.fixture)
    minimized_path, info, comparison, runs = greedy_reduce(source_program)
    original_text = read_text(source_program)
    minimized_text = read_text(minimized_path)
    divergence_event_index = comparison["first_divergence_index"]
    report = {
        "program_id": info["program_id"],
        "first_divergence_event_index": divergence_event_index,
        "first_divergence_syscall_index": map_event_index_to_program_call(runs["reference_canonical"], divergence_event_index),
        "original_length": len(original_text),
        "minimized_length": len(minimized_text),
        "original_testcase_path": str(source_program),
        "minimized_testcase_path": str(minimized_path),
        "reference_evidence_path": runs["reference"]["trace_json_path"],
        "candidate_evidence_path": runs["candidate"]["trace_json_path"],
        "reference_canonical_trace_path": runs["reference_canonical_path"],
        "candidate_canonical_trace_path": runs["candidate_canonical_path"],
        "reference_console_log_path": runs["reference"]["console_log_path"],
        "candidate_console_log_path": runs["candidate"]["console_log_path"],
        "run_command": f"python3 tools/reduce_case.py --workflow {cfg['workflow']} --fixture {args.fixture}",
        "scml_preflight_status": source_entry.get("scml_preflight_status", "unknown") if source_entry else "unknown",
        "scml_trace_log_path": source_entry.get("scml_trace_log_path", "") if source_entry else "",
        "scml_sctrace_output_path": source_entry.get("scml_sctrace_output_path", "") if source_entry else "",
    }
    json_path = report_path("minimized-report.json", cfg=cfg)
    md_path = report_path("minimized-report.md", cfg=cfg)
    dump_json(json_path, report)
    md_path.write_text(
        "\n".join(
            [
                "# Minimized divergence report",
                "",
                f"- program_id: {report['program_id']}",
                f"- first_divergence_event_index: {report['first_divergence_event_index']}",
                f"- first_divergence_syscall_index: {report['first_divergence_syscall_index']}",
                f"- original_length: {report['original_length']}",
                f"- minimized_length: {report['minimized_length']}",
                f"- original_testcase_path: {report['original_testcase_path']}",
                f"- minimized_testcase_path: {report['minimized_testcase_path']}",
                f"- reference_evidence_path: {report['reference_evidence_path']}",
                f"- candidate_evidence_path: {report['candidate_evidence_path']}",
                f"- reference_canonical_trace_path: {report['reference_canonical_trace_path']}",
                f"- candidate_canonical_trace_path: {report['candidate_canonical_trace_path']}",
                f"- reference_console_log_path: {report['reference_console_log_path']}",
                f"- candidate_console_log_path: {report['candidate_console_log_path']}",
                f"- run_command: {report['run_command']}",
                f"- scml_preflight_status: {report['scml_preflight_status']}",
                f"- scml_trace_log_path: {report['scml_trace_log_path']}",
                f"- scml_sctrace_output_path: {report['scml_sctrace_output_path']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
