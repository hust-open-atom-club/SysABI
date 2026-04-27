#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, current_workflow, dump_json, load_json, load_jsonl, report_path, resolve_repo_path
from targets.registry import active_target_name


def merge_scml_result_counts(
    campaign_results: list[dict[str, object]],
    scml_rejections: list[dict[str, object]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in campaign_results:
        bucket = result.get("scml_result_bucket")
        if bucket:
            counts[bucket] = counts.get(bucket, 0) + 1
    if scml_rejections:
        counts["rejected_by_scml"] = counts.get("rejected_by_scml", 0) + len(scml_rejections)
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--config-path")
    parser.add_argument("--campaign")
    return parser.parse_args()


def selected_campaign(cfg: dict[str, object], campaign: str | None = None) -> str:
    if campaign is not None:
        return campaign
    summary_path = report_path("summary.json", cfg=cfg)
    if summary_path.exists():
        return str(load_json(summary_path).get("campaign", "full"))
    return "full"


def count_eligible_programs(cfg: dict[str, object]) -> int:
    eligible_path = resolve_repo_path(cfg["paths"]["eligible_file"])
    with eligible_path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def candidate_trace_completed(result: dict[str, object]) -> bool:
    candidate_run = result.get("candidate_run", {})
    candidate_trace = candidate_run.get("trace_json_path")
    if not candidate_trace or not Path(str(candidate_trace)).exists():
        return False
    reference_run = latest_reference_run(result)
    if reference_run is None:
        return False
    reference_trace = reference_run.get("trace_json_path")
    return bool(reference_trace and Path(str(reference_trace)).exists())


def should_include_generation_summary(
    cfg: dict[str, object],
    generation_summary: dict[str, object],
) -> bool:
    paths = cfg.get("paths", {})
    derivation = cfg.get("derivation", {})
    generated_file = paths.get("generated_file")
    generated_source_eligible_file = (
        derivation.get("generated_source_eligible_file")
        or derivation.get("source_eligible_file")
    )
    if not generated_file or not generated_source_eligible_file:
        return False
    return resolve_repo_path(generated_source_eligible_file) == resolve_repo_path(generated_file)


def latest_reference_run(result: dict[str, object]) -> dict[str, object] | None:
    reference_runs = result.get("reference_runs", [])
    if not reference_runs:
        return None
    return reference_runs[-1]


def latest_candidate_run(result: dict[str, object]) -> dict[str, object] | None:
    candidate_run = result.get("candidate_run")
    if candidate_run:
        return candidate_run
    candidate_runs = result.get("candidate_runs", [])
    if not candidate_runs:
        return None
    return candidate_runs[-1]


def canonical_trace_for_run(run: dict[str, object] | None) -> dict[str, object] | None:
    if run is None:
        return None
    trace_json_path = run.get("trace_json_path")
    if not trace_json_path:
        return None
    canonical_path = Path(str(trace_json_path)).with_name("canonical-trace.json")
    if not canonical_path.exists():
        return None
    return load_json(canonical_path)


def event_by_index(canonical_trace: dict[str, object] | None, event_index: int | None) -> dict[str, object] | None:
    if canonical_trace is None or event_index is None:
        return None
    for event in canonical_trace.get("events", []):
        if event.get("index") == event_index:
            return event
    return None


def first_divergence_details(result: dict[str, object]) -> tuple[int | None, str | None]:
    comparison = result.get("comparison") or {}
    first_divergence_index = comparison.get("first_divergence_index")
    if result.get("first_divergence_syscall_name") is not None:
        return first_divergence_index, str(result["first_divergence_syscall_name"])

    reference_canonical = canonical_trace_for_run(latest_reference_run(result))
    candidate_canonical = canonical_trace_for_run(latest_candidate_run(result))
    reference_event = event_by_index(reference_canonical, first_divergence_index)
    candidate_event = event_by_index(candidate_canonical, first_divergence_index)
    if reference_event is not None:
        return first_divergence_index, str(reference_event["syscall_name"])
    if candidate_event is not None:
        return first_divergence_index, str(candidate_event["syscall_name"])
    if comparison.get("final_state_equal") is False:
        return first_divergence_index, "final_state_only"
    if comparison.get("process_exit_equal") is False:
        return first_divergence_index, "process_exit_only"
    return first_divergence_index, None


def workflow_side_labels(cfg: dict[str, object]) -> tuple[str, str]:
    presentation = cfg.get("presentation", {})
    if isinstance(presentation, dict):
        reference_label = presentation.get("reference_label")
        candidate_label = presentation.get("candidate_label")
        if isinstance(reference_label, str) and isinstance(candidate_label, str):
            return reference_label, candidate_label
    return "Reference", "Candidate"


def event_difference_fields(
    reference_event: dict[str, object] | None,
    candidate_event: dict[str, object] | None,
) -> list[str]:
    if reference_event is None or candidate_event is None:
        return []
    fields: list[str] = []
    field_labels = (
        ("syscall", "syscall_name"),
        ("args", "args"),
        ("return_value", "return_value"),
        ("errno", "errno"),
        ("outputs", "outputs"),
    )
    for label, key in field_labels:
        if reference_event.get(key) != candidate_event.get(key):
            fields.append(label)
    return fields


def summarize_event(event: dict[str, object] | None) -> str:
    if event is None:
        return "n/a"
    args = json.dumps(event.get("args", []), ensure_ascii=False)
    return (
        f"{event.get('syscall_name', 'unknown')}(args={args}) -> "
        f"ret={event.get('return_value', 'n/a')} errno={event.get('errno', 'n/a')} "
        f"outputs={len(event.get('outputs', [])) if isinstance(event.get('outputs'), list) else 'n/a'}"
    )


def summarize_process_exit(canonical_trace: dict[str, object] | None) -> str:
    if canonical_trace is None:
        return "n/a"
    process_exit = canonical_trace.get("process_exit")
    if not isinstance(process_exit, dict):
        return "n/a"
    return (
        "process_exit("
        f"status={process_exit.get('status', 'n/a')}, "
        f"exit_code={process_exit.get('exit_code', 'n/a')}, "
        f"timed_out={process_exit.get('timed_out', 'n/a')})"
    )


def summarize_final_state(canonical_trace: dict[str, object] | None) -> str:
    if canonical_trace is None:
        return "n/a"
    final_state = canonical_trace.get("final_state")
    if not isinstance(final_state, dict):
        return "n/a"
    files = final_state.get("files", [])
    file_count = len(files) if isinstance(files, list) else "n/a"
    return f"final_state(files={file_count})"


def summarize_run_status(run: dict[str, object] | None) -> str:
    if run is None:
        return "no_run"
    parts = [f"status={run.get('status', 'n/a')}"]
    if run.get("status_detail"):
        parts.append(f"detail={run['status_detail']}")
    return "run(" + ", ".join(parts) + ")"


def describe_comparison(
    result: dict[str, object],
    *,
    reference_run: dict[str, object] | None,
    candidate_run: dict[str, object] | None,
) -> tuple[str, list[str], str, str]:
    comparison = result.get("comparison") or {}
    first_divergence_index = comparison.get("first_divergence_index")
    reference_canonical = canonical_trace_for_run(reference_run)
    candidate_canonical = canonical_trace_for_run(candidate_run)
    reference_event = event_by_index(reference_canonical, first_divergence_index)
    candidate_event = event_by_index(candidate_canonical, first_divergence_index)
    diff_fields = event_difference_fields(reference_event, candidate_event)
    if diff_fields:
        reference_result = summarize_event(reference_event)
        candidate_result = summarize_event(candidate_event)
        if "outputs" in diff_fields:
            reference_result = reference_result.rsplit(" outputs=", 1)[0] + " outputs=content_different"
            candidate_result = candidate_result.rsplit(" outputs=", 1)[0] + " outputs=content_different"
        reason = f"syscall_result_mismatch({', '.join(diff_fields)})"
        return reason, diff_fields, reference_result, candidate_result
    if comparison.get("final_state_equal") is False:
        return "final_state_mismatch", [], summarize_final_state(reference_canonical), summarize_final_state(candidate_canonical)
    if comparison.get("process_exit_equal") is False:
        return "process_exit_mismatch", [], summarize_process_exit(reference_canonical), summarize_process_exit(candidate_canonical)
    if comparison.get("reason") == "event_count_mismatch":
        return "event_count_mismatch", [], summarize_run_status(reference_run), summarize_run_status(candidate_run)
    return str(comparison.get("reason", "unknown")), [], summarize_run_status(reference_run), summarize_run_status(candidate_run)


def build_syscall_summary(
    cfg: dict[str, object],
    campaign_results: list[dict[str, object]],
    *,
    campaign: str | None = None,
) -> dict[str, object]:
    reference_label, candidate_label = workflow_side_labels(cfg)
    problem_results = [
        result
        for result in campaign_results
        if result.get("classification") != cfg["classification"]["no_diff"]
    ]
    grouped_cases: dict[str, list[dict[str, object]]] = defaultdict(list)
    for result in sorted(problem_results, key=lambda row: str(row["program_id"])):
        first_divergence_index, syscall_name = first_divergence_details(result)
        reference_run = latest_reference_run(result)
        candidate_run = latest_candidate_run(result)
        comparison_reason, difference_fields, reference_result, candidate_result = describe_comparison(
            result,
            reference_run=reference_run,
            candidate_run=candidate_run,
        )
        grouped_cases[syscall_name or "unknown"].append(
            {
                "program_id": result["program_id"],
                "classification": result["classification"],
                "first_divergence_index": first_divergence_index,
                "normalized_path": result.get("normalized_path", ""),
                "reference_status": reference_run.get("status") if reference_run else None,
                "candidate_status": candidate_run.get("status") if candidate_run else None,
                "scml_result_bucket": result.get("scml_result_bucket"),
                "comparison_reason": comparison_reason,
                "difference_fields": difference_fields,
                "reference_result": reference_result,
                "candidate_result": candidate_result,
            }
        )

    syscall_rows = [
        {
            "syscall_name": syscall_name,
            "case_count": len(cases),
            "cases": cases,
        }
        for syscall_name, cases in grouped_cases.items()
    ]
    syscall_rows.sort(key=lambda row: (-int(row["case_count"]), str(row["syscall_name"])))
    return {
        "workflow": current_workflow(cfg),
        "campaign": selected_campaign(cfg, campaign),
        "reference_label": reference_label,
        "candidate_label": candidate_label,
        "total_problem_cases": len(problem_results),
        "syscall_bucket_count": len(syscall_rows),
        "syscalls": syscall_rows,
    }


def build_rendered_summary(cfg: dict[str, object], campaign: str | None = None) -> dict[str, object]:
    campaign_results = load_jsonl(report_path("campaign-results.jsonl", cfg=cfg))
    build_summary_path = report_path("build-summary.json", cfg=cfg)
    if build_summary_path.exists():
        build_summary = load_json(build_summary_path)
    else:
        build_failures = sum(1 for result in campaign_results if result.get("classification") == "build_failure")
        build_summary = {
            "success": max(0, len(campaign_results) - build_failures),
            "total": len(campaign_results),
        }
    classification_counts: dict[str, int] = {}
    for result in campaign_results:
        classification_counts[result["classification"]] = classification_counts.get(result["classification"], 0) + 1

    total = len(campaign_results)
    scml_rejections_path = report_path("scml-rejections.jsonl", cfg=cfg)
    scml_rejections = load_jsonl(scml_rejections_path) if scml_rejections_path.exists() else []
    scml_result_counts = merge_scml_result_counts(campaign_results, scml_rejections)
    candidate_completed = [result for result in campaign_results if candidate_trace_completed(result)]
    traces_ok = [
        result
        for result in candidate_completed
        if Path(result["candidate_run"]["trace_json_path"]).exists()
        and (
            (reference_run := latest_reference_run(result)) is not None
            and Path(str(reference_run["trace_json_path"])).exists()
        )
    ]
    canonical_ok = [
        result
        for result in candidate_completed
        if Path(result["candidate_run"]["trace_json_path"]).with_name("canonical-trace.json").exists()
        and (
            (reference_run := latest_reference_run(result)) is not None
            and Path(str(reference_run["trace_json_path"])).with_name("canonical-trace.json").exists()
        )
    ]
    candidate_runs = [result["candidate_run"] for result in campaign_results if "candidate_run" in result]

    # Load scheduler-level summary for concurrency metadata
    scheduler_summary_path = report_path("summary.json", cfg=cfg)
    scheduler_summary = load_json(scheduler_summary_path) if scheduler_summary_path.exists() else {}

    # Concurrency breakdown: candidate run status distribution
    candidate_status_counts: dict[str, int] = {}
    for run in candidate_runs:
        status = str(run.get("status", "unknown"))
        candidate_status_counts[status] = candidate_status_counts.get(status, 0) + 1

    concurrency_breakdown = {
        "jobs": scheduler_summary.get("jobs"),
        "max_concurrent_vms": scheduler_summary.get("max_concurrent_vms"),
        "total_cases": total,
        "completed_cases": candidate_status_counts.get("ok", 0),
        "timeout_cases": candidate_status_counts.get("timeout", 0),
        "infra_error_cases": candidate_status_counts.get("infra_error", 0),
        "crash_cases": candidate_status_counts.get("crash", 0),
        "candidate_bug_cases": candidate_status_counts.get("candidate_bug", 0),
    }

    # Infra error breakdown: finer-grained classification of non-ok runs
    infra_error_breakdown: dict[str, int] = {
        "timeout": 0,
        "infra_error": 0,
        "crash": 0,
        "candidate_bug": 0,
        "other": 0,
    }
    for run in candidate_runs:
        status = str(run.get("status", "unknown"))
        if status == "timeout":
            infra_error_breakdown["timeout"] += 1
        elif status == "infra_error":
            infra_error_breakdown["infra_error"] += 1
        elif status == "crash":
            infra_error_breakdown["crash"] += 1
        elif status == "candidate_bug":
            infra_error_breakdown["candidate_bug"] += 1
        elif status != "ok":
            infra_error_breakdown["other"] += 1

    summary = {
        "campaign": selected_campaign(cfg, campaign),
        "workflow": current_workflow(cfg),
        "total": total,
        "classification_counts": classification_counts,
        "scml_result_counts": scml_result_counts,
        "scml_rejected_count": len(scml_rejections),
        "eligible_program_count": count_eligible_programs(cfg),
        "build_success_rate": build_summary["success"] / build_summary["total"] if build_summary["total"] else 0.0,
        "dual_execution_completion_rate": len(candidate_completed) / total if total else 0.0,
        "trace_generation_success_rate": len(traces_ok) / len(candidate_completed) if candidate_completed else 0.0,
        "canonicalization_success_rate": len(canonical_ok) / len(traces_ok) if traces_ok else 0.0,
        "baseline_invalid_rate": classification_counts.get(cfg["classification"]["baseline_invalid"], 0) / total if total else 0.0,
        "scml_preflight_pass_rate": (
            total / (total + len(scml_rejections))
            if (total + len(scml_rejections))
            else 0.0
        ),
        "candidate_runner_kinds": sorted({run.get("runner_kind") for run in candidate_runs if run.get("runner_kind")}),
        "candidate_kernel_builds": sorted({run.get("kernel_build") for run in candidate_runs if run.get("kernel_build")}),
        "concurrency_breakdown": concurrency_breakdown,
        "infra_error_breakdown": infra_error_breakdown,
    }
    import_summary_path = report_path("import-summary.json", cfg=cfg)
    if import_summary_path.exists():
        import_summary = load_json(import_summary_path)
        summary["import_success_rate"] = import_summary["imported"] / import_summary["total_files"] if import_summary["total_files"] else 0.0
    derivation_summary_path = report_path("derivation-summary.json", cfg=cfg)
    if derivation_summary_path.exists():
        derivation_summary = load_json(derivation_summary_path)
        source_total = derivation_summary["source_total"]
        summary["derivation_success_rate"] = derivation_summary["eligible"] / source_total if source_total else 0.0
    generation_summary_path = report_path("generation-summary.json", cfg=cfg)
    if generation_summary_path.exists():
        generation_summary = load_json(generation_summary_path)
        if should_include_generation_summary(cfg, generation_summary):
            summary["profile_enabled_total"] = generation_summary.get("profile_enabled_total", 0)
            summary["targets_with_candidates"] = generation_summary.get("targets_with_candidates", 0)
            summary["targets_without_candidates"] = generation_summary.get("targets_without_candidates", 0)
            summary["generation_candidate_count"] = generation_summary.get("unique_candidate_count", 0)
    thresholds = cfg["thresholds"]["signoff" if summary["campaign"] == "full" else "smoke"]
    checks = [
        summary["build_success_rate"] >= thresholds.get("build_success_rate", 0.0),
        summary["dual_execution_completion_rate"] >= thresholds.get("dual_execution_completion_rate", 0.0),
        summary["trace_generation_success_rate"] >= thresholds.get("trace_success_rate", 0.0),
        summary["canonicalization_success_rate"] >= thresholds.get("canonical_success_rate", 0.0),
        summary["baseline_invalid_rate"] < thresholds.get("baseline_invalid_rate", 1.0),
        summary["total"] >= thresholds.get("total_min", 0),
        summary["eligible_program_count"] >= thresholds.get("eligible_program_count_min", 0),
    ]
    if "import_success_rate" in thresholds:
        checks.append(summary.get("import_success_rate", 0.0) >= thresholds["import_success_rate"])
    if thresholds.get("require_minimized_report"):
        checks.append(report_path("minimized-report.json", cfg=cfg).exists())
    summary["signoff_pass"] = all(checks)
    return summary


def write_rendered_summary(summary: dict[str, object], cfg: dict[str, object]) -> None:
    dump_json(report_path("summary.json", cfg=cfg), summary)
    lines = [
        f"# {summary['workflow']} {summary['campaign']} summary",
        "",
        f"- total: {summary['total']}",
        f"- eligible_program_count: {summary['eligible_program_count']}",
        f"- build_success_rate: {summary['build_success_rate']:.3f}",
        f"- scml_preflight_pass_rate: {summary['scml_preflight_pass_rate']:.3f}",
        f"- scml_rejected_count: {summary['scml_rejected_count']}",
        f"- dual execution completion rate: {summary['dual_execution_completion_rate']:.3f}",
        f"- trace_generation_success_rate: {summary['trace_generation_success_rate']:.3f}",
        f"- canonicalization_success_rate: {summary['canonicalization_success_rate']:.3f}",
        f"- baseline-invalid rate: {summary['baseline_invalid_rate']:.3f}",
        f"- candidate_runner_kinds: {', '.join(summary['candidate_runner_kinds']) or 'n/a'}",
        f"- candidate_kernel_builds: {', '.join(summary['candidate_kernel_builds']) or 'n/a'}",
        f"- signoff_pass: {summary['signoff_pass']}",
        "",
        "## classification counts",
    ]
    if "import_success_rate" in summary:
        lines.insert(4, f"- import_success_rate: {summary['import_success_rate']:.3f}")
    if "derivation_success_rate" in summary:
        lines.insert(4, f"- derivation_success_rate: {summary['derivation_success_rate']:.3f}")
    if "profile_enabled_total" in summary:
        lines.insert(4, f"- profile_enabled_total: {summary['profile_enabled_total']}")
        lines.insert(5, f"- targets_with_candidates: {summary['targets_with_candidates']}")
        lines.insert(6, f"- targets_without_candidates: {summary['targets_without_candidates']}")
        lines.insert(7, f"- generation_candidate_count: {summary['generation_candidate_count']}")
    for key, value in sorted(summary["classification_counts"].items()):
        lines.append(f"- {key}: {value}")
    if summary["scml_result_counts"]:
        lines.extend(["", "## scml result counts"])
        for key, value in sorted(summary["scml_result_counts"].items()):
            lines.append(f"- {key}: {value}")
    cb = summary.get("concurrency_breakdown")
    if cb:
        lines.extend(["", "## concurrency breakdown"])
        lines.append(f"- jobs: {cb.get('jobs', 'n/a')}")
        lines.append(f"- max_concurrent_vms: {cb.get('max_concurrent_vms', 'n/a')}")
        lines.append(f"- total_cases: {cb.get('total_cases', 0)}")
        lines.append(f"- completed_cases: {cb.get('completed_cases', 0)}")
        lines.append(f"- timeout_cases: {cb.get('timeout_cases', 0)}")
        lines.append(f"- infra_error_cases: {cb.get('infra_error_cases', 0)}")
        lines.append(f"- crash_cases: {cb.get('crash_cases', 0)}")
        lines.append(f"- candidate_bug_cases: {cb.get('candidate_bug_cases', 0)}")
    ieb = summary.get("infra_error_breakdown")
    if ieb:
        lines.extend(["", "## infra error breakdown"])
        for key, value in sorted(ieb.items()):
            lines.append(f"- {key}: {value}")
    report_path("summary.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")
    signoff_lines = [
        f"# {summary['workflow']} sign-off",
        "",
        f"- campaign: {summary['campaign']}",
        f"- eligible_program_count: {summary['eligible_program_count']}",
    ]
    if "import_success_rate" in summary:
        signoff_lines.append(f"- import_success_rate: {summary['import_success_rate']:.3f}")
    if "derivation_success_rate" in summary:
        signoff_lines.append(f"- derivation_success_rate: {summary['derivation_success_rate']:.3f}")
    if "profile_enabled_total" in summary:
        signoff_lines.append(f"- profile_enabled_total: {summary['profile_enabled_total']}")
        signoff_lines.append(f"- targets_with_candidates: {summary['targets_with_candidates']}")
        signoff_lines.append(f"- targets_without_candidates: {summary['targets_without_candidates']}")
        signoff_lines.append(f"- generation_candidate_count: {summary['generation_candidate_count']}")
    signoff_lines.extend(
        [
            f"- build_success_rate: {summary['build_success_rate']:.3f}",
            f"- scml_preflight_pass_rate: {summary['scml_preflight_pass_rate']:.3f}",
            f"- scml_rejected_count: {summary['scml_rejected_count']}",
            f"- dual_execution_completion_rate: {summary['dual_execution_completion_rate']:.3f}",
            f"- trace_generation_success_rate: {summary['trace_generation_success_rate']:.3f}",
            f"- canonicalization_success_rate: {summary['canonicalization_success_rate']:.3f}",
            f"- baseline_invalid_rate: {summary['baseline_invalid_rate']:.3f}",
            f"- candidate_runner_kinds: {', '.join(summary['candidate_runner_kinds']) or 'n/a'}",
            f"- candidate_kernel_builds: {', '.join(summary['candidate_kernel_builds']) or 'n/a'}",
            f"- signoff_pass: {summary['signoff_pass']}",
        ]
    )
    report_path("signoff.md", cfg=cfg).write_text(
        "\n".join(
            signoff_lines
        )
        + "\n",
        encoding="utf-8",
    )


def write_syscall_summary(summary: dict[str, object], cfg: dict[str, object]) -> None:
    dump_json(report_path("syscall-summary.json", cfg=cfg), summary)
    reference_label = str(summary.get("reference_label", "Reference"))
    candidate_label = str(summary.get("candidate_label", "Candidate"))
    lines = [
        f"# {summary['workflow']} {summary['campaign']} syscall summary",
        "",
        f"- total_problem_cases: {summary['total_problem_cases']}",
        f"- syscall_bucket_count: {summary['syscall_bucket_count']}",
        f"- reference_label: {reference_label}",
        f"- candidate_label: {candidate_label}",
    ]
    if not summary["syscalls"]:
        lines.extend(["", "## syscalls", "- none: 0"])
    else:
        for row in summary["syscalls"]:
            lines.extend(
                [
                    "",
                    f"## {row['syscall_name']}",
                    f"- case_count: {row['case_count']}",
                ]
            )
            for case in row["cases"]:
                lines.append(
                    "- "
                    f"{case['program_id']}: "
                    f"classification={case['classification']}, "
                    f"comparison_reason={case['comparison_reason']}, "
                    f"first_divergence_index={case['first_divergence_index'] if case['first_divergence_index'] is not None else 'n/a'}, "
                    f"reference_status={case['reference_status'] or 'n/a'}, "
                    f"candidate_status={case['candidate_status'] or 'n/a'}, "
                    f"scml_result_bucket={case['scml_result_bucket'] or 'n/a'}, "
                    f"testcase={case['normalized_path'] or 'n/a'}"
                )
                lines.append(f"  {reference_label}: {case['reference_result']}")
                lines.append(f"  {candidate_label}: {case['candidate_result']}")
    report_path("syscall-summary.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_summary_reports(
    *,
    workflow: str | None = None,
    config_path: str | Path | None = None,
    campaign: str | None = None,
) -> dict[str, object]:
    configure_runtime(workflow=workflow, config_path=config_path)
    cfg = config(workflow=workflow, config_path=config_path)
    campaign_results = load_jsonl(report_path("campaign-results.jsonl", cfg=cfg))
    summary = build_rendered_summary(cfg, campaign=campaign)
    syscall_summary = build_syscall_summary(cfg, campaign_results, campaign=campaign)
    write_rendered_summary(summary, cfg)
    write_syscall_summary(syscall_summary, cfg)
    return summary


def main() -> None:
    args = parse_args()
    render_summary_reports(workflow=args.workflow, config_path=args.config_path, campaign=args.campaign)


if __name__ == "__main__":
    main()
