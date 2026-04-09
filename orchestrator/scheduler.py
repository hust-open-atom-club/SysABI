#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.classify import classify_result
from analyzer.compare import compare_canonical
from analyzer.normalize import canonicalize
from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl, report_path, reports_dir, runner_profiles
from orchestrator.stability import all_equal, canonical_trace_hash, build_status_ok
from orchestrator.vm_runner import build_root, execute_side


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--campaign", default="smoke")
    parser.add_argument("--eligible-file")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--program-id")
    parser.add_argument("--controlled-divergence", action="store_true")
    return parser.parse_args()


def next_run_id(program_id: str) -> str:
    return f"{int(time.time())}-{program_id[:12]}"


def load_canonical(result) -> dict[str, object]:
    raw = load_json(result.trace_json_path)
    external_state = load_json(result.external_state_path)
    canonical = canonicalize(raw, external_state)
    output_path = Path(result.trace_json_path).with_name("canonical-trace.json")
    dump_json(output_path, canonical)
    return canonical


def run_reference_once(program_id: str, run_id: str, suffix: str) -> tuple[object, dict[str, object] | None]:
    cfg = config()
    result = execute_side(program_id=program_id, side="reference", timeout_sec=cfg["stability"]["timeout_sec"], run_id=f"{run_id}-{suffix}")
    canonical = load_canonical(result) if result.status == "ok" else None
    return result, canonical


def run_candidate_once(program_id: str, run_id: str, suffix: str, inject_trace: dict[str, object] | None) -> tuple[object, dict[str, object] | None]:
    cfg = config()
    result = execute_side(
        program_id=program_id,
        side="candidate",
        timeout_sec=cfg["stability"]["timeout_sec"],
        run_id=f"{run_id}-{suffix}",
        inject_trace=inject_trace,
    )
    canonical = load_canonical(result) if result.status == "ok" else None
    return result, canonical


def controlled_divergence_spec(enabled: bool) -> dict[str, object] | None:
    candidate = runner_profiles()["candidate"]
    spec = candidate.get("controlled_divergence", {})
    if not enabled or not spec.get("enabled", False):
        return None
    return {
        "call_index": -1,
        "syscall_name": spec["match_syscall"],
        "field": spec["field"],
        "value": spec["value"],
    }


def schedule_one(entry: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    cfg = config()
    program_id = entry["program_id"]
    build_result_path = build_root(program_id) / "build-result.json"
    if not build_status_ok(build_result_path):
        return {
            "program_id": program_id,
            "classification": "build_failure",
            "build_result_path": str(build_result_path),
        }

    inject_trace = controlled_divergence_spec(args.controlled_divergence)
    run_id = next_run_id(program_id)
    reference_results = []
    reference_hashes = []
    candidate_results = []
    candidate_hashes = []

    reference_result, reference_canonical = run_reference_once(program_id, run_id, "ref0")
    reference_results.append(reference_result)
    current_reference_canonical = reference_canonical
    if reference_canonical is not None:
        reference_hashes.append(canonical_trace_hash(reference_canonical))

    if reference_result.status != "ok":
        for attempt in range(cfg["stability"]["rerun_count"]):
            rerun_result, rerun_canonical = run_reference_once(program_id, run_id, f"ref-triage{attempt}")
            reference_results.append(rerun_result)
            if rerun_canonical is not None:
                reference_hashes.append(canonical_trace_hash(rerun_canonical))
        return {
            "program_id": program_id,
            "classification": cfg["classification"]["baseline_invalid"],
            "reference_runs": [result.to_dict() for result in reference_results],
        }

    candidate_result, candidate_canonical = run_candidate_once(program_id, run_id, "candidate0", inject_trace)
    candidate_results.append(candidate_result)
    if candidate_canonical is not None:
        candidate_hashes.append(canonical_trace_hash(candidate_canonical))

    comparison = compare_canonical(reference_canonical, candidate_canonical) if candidate_canonical else None
    needs_triage = candidate_result.status != "ok" or (comparison is not None and not comparison["equivalent"])

    if needs_triage:
        for attempt in range(cfg["stability"]["rerun_count"]):
            ref_rerun, ref_canonical = run_reference_once(program_id, run_id, f"ref-triage{attempt}")
            reference_results.append(ref_rerun)
            if ref_canonical is not None:
                current_reference_canonical = ref_canonical
                reference_hashes.append(canonical_trace_hash(ref_canonical))
            cand_rerun, cand_canonical = run_candidate_once(program_id, run_id, f"candidate-triage{attempt}", inject_trace)
            candidate_results.append(cand_rerun)
            if cand_canonical is not None:
                candidate_hashes.append(canonical_trace_hash(cand_canonical))
                candidate_canonical = cand_canonical
                comparison = compare_canonical(current_reference_canonical, cand_canonical)

    reference_stable = all_equal(reference_hashes)
    if not reference_stable:
        return {
            "program_id": program_id,
            "classification": cfg["classification"]["baseline_invalid"],
            "reference_runs": [result.to_dict() for result in reference_results],
            "candidate_runs": [result.to_dict() for result in candidate_results],
        }

    candidate_status = candidate_results[-1].status
    if candidate_hashes and all_equal(candidate_hashes) and reference_hashes and candidate_hashes[-1] == reference_hashes[-1]:
        comparison = compare_canonical(current_reference_canonical, candidate_canonical) if candidate_canonical else comparison

    reference_status = reference_results[-1].status
    classification = classify_result(
        reference_stable=reference_stable,
        reference_status=reference_status,
        candidate_status=candidate_status,
        comparison=comparison,
    )
    result = {
        "program_id": program_id,
        "classification": classification,
        "reference_runs": [result.to_dict() for result in reference_results],
        "candidate_run": candidate_results[-1].to_dict(),
        "candidate_runs": [result.to_dict() for result in candidate_results],
        "comparison": comparison,
    }
    if candidate_canonical:
        result["reference_canonical_hash"] = reference_hashes[-1]
        result["candidate_canonical_hash"] = canonical_trace_hash(candidate_canonical)
    return result


def selected_entries(args: argparse.Namespace) -> list[dict[str, object]]:
    rows = load_jsonl(args.eligible_file)
    if args.program_id:
        rows = [row for row in rows if row["program_id"] == args.program_id]
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


def effective_jobs(args: argparse.Namespace, cfg: dict[str, object]) -> int:
    if args.jobs is not None:
        return max(1, args.jobs)
    parallel = cfg.get("parallel", {})
    if isinstance(parallel, dict):
        return max(1, int(parallel.get("jobs", 1)))
    return 1


def schedule_entries(entries: list[dict[str, object]], args: argparse.Namespace, jobs: int) -> list[dict[str, object]]:
    if jobs <= 1 or len(entries) <= 1:
        return [schedule_one(entry, args) for entry in entries]

    results: list[dict[str, object] | None] = [None] * len(entries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        future_map = {
            executor.submit(schedule_one, entry, args): index
            for index, entry in enumerate(entries)
        }
        for future in concurrent.futures.as_completed(future_map):
            results[future_map[future]] = future.result()
    return [result for result in results if result is not None]


def write_summary(results: list[dict[str, object]], campaign: str) -> None:
    cfg = config()
    classes = Counter(result["classification"] for result in results)
    summary = {
        "campaign": campaign,
        "total": len(results),
        "classification_counts": dict(classes),
        "baseline_invalid_rate": classes[cfg["classification"]["baseline_invalid"]] / len(results) if results else 0.0,
        "dual_execution_completion_rate": (
            sum(1 for result in results if result.get("candidate_run", {}).get("status") == "ok") / len(results)
            if results
            else 0.0
        ),
    }
    reports_dir(cfg).mkdir(parents=True, exist_ok=True)
    dump_json(report_path("summary.json", cfg=cfg), summary)
    lines = [
        f"# {cfg['workflow']} {campaign} summary",
        "",
        f"- total: {summary['total']}",
        f"- dual execution completion rate: {summary['dual_execution_completion_rate']:.3f}",
        f"- baseline-invalid rate: {summary['baseline_invalid_rate']:.3f}",
        "",
        "## classification counts",
    ]
    for key, value in sorted(classes.items()):
        lines.append(f"- {key}: {value}")
    report_path("summary.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")
    dump_jsonl(
        report_path("baseline-invalid.jsonl", cfg=cfg),
        [result for result in results if result["classification"] == cfg["classification"]["baseline_invalid"]],
    )
    dump_jsonl(
        report_path("divergence-index.jsonl", cfg=cfg),
        [result for result in results if result["classification"] in {cfg["classification"]["bug_likely"], cfg["classification"]["weak_spec_or_env_noise"]}],
    )
    dump_jsonl(
        report_path("unsupported-feature.jsonl", cfg=cfg),
        [result for result in results if result["classification"] == cfg["classification"]["unsupported_feature"]],
    )


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    if not args.eligible_file:
        args.eligible_file = cfg["paths"]["eligible_file"]
    entries = selected_entries(args)
    results = schedule_entries(entries, args, effective_jobs(args, cfg))
    dump_jsonl(report_path("campaign-results.jsonl", cfg=cfg), results)
    write_summary(results, args.campaign)


if __name__ == "__main__":
    main()
