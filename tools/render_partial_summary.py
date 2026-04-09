#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.classify import classify_result
from analyzer.compare import compare_canonical
from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl, report_path, resolve_repo_path
from orchestrator.scheduler import build_failure_case_summary
from orchestrator.stability import all_equal, canonical_trace_hash
from tools.render_summary import build_syscall_summary


RUN_DIR_RE = re.compile(
    r"^(?P<base>.+)-(?P<suffix>ref0|ref-triage(?P<ref_triage>\d+)|candidate0|candidate-triage(?P<candidate_triage>\d+))$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    return parser.parse_args()


def suffix_sort_key(suffix: str) -> tuple[int, int]:
    if suffix == "ref0" or suffix == "candidate0":
        return (0, 0)
    if suffix.startswith("ref-triage"):
        return (1, int(suffix.removeprefix("ref-triage")))
    if suffix.startswith("candidate-triage"):
        return (1, int(suffix.removeprefix("candidate-triage")))
    return (2, 0)


def load_run_result(run_dir: Path, side: str) -> dict[str, Any] | None:
    result_path = run_dir / side / "run-result.json"
    if not result_path.exists():
        return None
    return load_json(result_path)


def load_canonical_hash(run: dict[str, Any]) -> str | None:
    trace_json_path = run.get("trace_json_path")
    if not trace_json_path:
        return None
    canonical_path = Path(str(trace_json_path)).with_name("canonical-trace.json")
    if not canonical_path.exists():
        return None
    return canonical_trace_hash(load_json(canonical_path))


def load_canonical(run: dict[str, Any]) -> dict[str, Any] | None:
    trace_json_path = run.get("trace_json_path")
    if not trace_json_path:
        return None
    canonical_path = Path(str(trace_json_path)).with_name("canonical-trace.json")
    if not canonical_path.exists():
        return None
    return load_json(canonical_path)


def run_ready_for_partial_summary(run_dir: Path, side: str) -> bool:
    run = load_run_result(run_dir, side)
    if run is None:
        return False
    return str(run.get("status", "")) != "ok" or load_canonical(run) is not None


def latest_complete_group(program_dir: Path) -> tuple[str, list[tuple[str, Path]], list[tuple[str, Path]]] | None:
    grouped: dict[str, dict[str, list[tuple[str, Path]]]] = defaultdict(lambda: {"reference": [], "candidate": []})
    for child in program_dir.iterdir():
        if not child.is_dir():
            continue
        match = RUN_DIR_RE.match(child.name)
        if match is None:
            continue
        base = match.group("base")
        suffix = match.group("suffix")
        side = "reference" if suffix.startswith("ref") else "candidate"
        grouped[base][side].append((suffix, child))
    for base in sorted(grouped.keys(), reverse=True):
        reference_dirs = sorted(grouped[base]["reference"], key=lambda item: suffix_sort_key(item[0]))
        candidate_dirs = sorted(grouped[base]["candidate"], key=lambda item: suffix_sort_key(item[0]))
        if not reference_dirs or not candidate_dirs:
            continue
        if not run_ready_for_partial_summary(reference_dirs[-1][1], "reference"):
            continue
        if not run_ready_for_partial_summary(candidate_dirs[-1][1], "candidate"):
            continue
        return base, reference_dirs, candidate_dirs
    return None


def reconstruct_completed_results(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    eligible_index = {row["program_id"]: row for row in load_jsonl(cfg["paths"]["eligible_file"])}
    artifacts_root = resolve_repo_path(cfg["paths"]["artifacts_dir"])
    results: list[dict[str, Any]] = []
    for program_id in sorted(eligible_index):
        program_dir = artifacts_root / program_id
        if not program_dir.is_dir():
            continue
        group = latest_complete_group(program_dir)
        if group is None:
            continue
        _, reference_dirs, candidate_dirs = group
        entry = eligible_index.get(program_id, {"program_id": program_id})
        reference_runs = []
        reference_hashes: list[str] = []
        for _, run_dir in reference_dirs:
            run = load_run_result(run_dir, "reference")
            if run is None:
                continue
            reference_runs.append(run)
            canonical_hash = load_canonical_hash(run)
            if canonical_hash is not None:
                reference_hashes.append(canonical_hash)
        candidate_runs = []
        candidate_hashes: list[str] = []
        for _, run_dir in candidate_dirs:
            run = load_run_result(run_dir, "candidate")
            if run is None:
                continue
            candidate_runs.append(run)
            canonical_hash = load_canonical_hash(run)
            if canonical_hash is not None:
                candidate_hashes.append(canonical_hash)
        if not reference_runs or not candidate_runs:
            continue

        latest_reference = reference_runs[-1]
        latest_candidate = candidate_runs[-1]
        reference_canonical = load_canonical(latest_reference)
        candidate_canonical = load_canonical(latest_candidate)
        comparison = (
            compare_canonical(reference_canonical, candidate_canonical)
            if reference_canonical is not None and candidate_canonical is not None
            else None
        )
        reference_stable = all_equal(reference_hashes)
        classification = classify_result(
            reference_stable=reference_stable,
            reference_status=str(latest_reference.get("status", "")),
            candidate_status=str(latest_candidate.get("status", "")),
            comparison=comparison,
        )
        result = {
            "program_id": program_id,
            "classification": classification,
            "normalized_path": entry.get("normalized_path", ""),
            "meta_path": entry.get("meta_path", ""),
            "reference_runs": reference_runs,
            "candidate_run": latest_candidate,
            "candidate_runs": candidate_runs,
            "comparison": comparison,
            "scml_preflight_status": entry.get("scml_preflight_status", "not_run"),
            "scml_rejection_reasons": entry.get("scml_rejection_reasons", []),
            "scml_trace_log_path": entry.get("scml_trace_log_path", ""),
            "scml_sctrace_output_path": entry.get("scml_sctrace_output_path", ""),
            "scml_preflight_run_root": entry.get("scml_preflight_run_root", ""),
            "scml_result_bucket": (
                "passed_scml_and_no_diff"
                if comparison is not None and comparison.get("equivalent")
                else "passed_scml_and_diverged"
            )
            if entry.get("scml_preflight_status", "not_run") == "passed"
            else ("" if entry.get("scml_preflight_status", "not_run") == "not_run" else "rejected_by_scml"),
        }
        if comparison is None and classification == cfg["classification"]["baseline_invalid"]:
            result["scml_result_bucket"] = "passed_scml_but_reference_failed"
        elif comparison is None and latest_candidate.get("status") != "ok":
            result["scml_result_bucket"] = "passed_scml_but_candidate_failed"
        results.append(result)
    return results


def write_partial_summary(results: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    classification_counts = Counter(result["classification"] for result in results)
    candidate_status_counts = Counter(result["candidate_run"]["status"] for result in results if result.get("candidate_run"))
    payload = {
        "workflow": cfg["workflow"],
        "campaign": "partial",
        "completed_results": len(results),
        "classification_counts": dict(classification_counts),
        "candidate_status_counts": dict(candidate_status_counts),
    }
    dump_json(report_path("partial-summary.json", cfg=cfg), payload)
    lines = [
        f"# {cfg['workflow']} partial summary",
        "",
        f"- completed_results: {payload['completed_results']}",
        "",
        "## classification counts",
    ]
    if classification_counts:
        for key, value in sorted(classification_counts.items()):
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- none: 0")
    lines.extend(["", "## candidate status counts"])
    if candidate_status_counts:
        for key, value in sorted(candidate_status_counts.items()):
            lines.append(f"- {key}: {value}")
    else:
        lines.append("- none: 0")
    report_path("partial-summary.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_partial_syscall_summary(summary: dict[str, Any], cfg: dict[str, Any]) -> None:
    dump_json(report_path("partial-syscall-summary.json", cfg=cfg), summary)
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
            lines.extend(["", f"## {row['syscall_name']}", f"- case_count: {row['case_count']}"])
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
    report_path("partial-syscall-summary.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_partial_failure_report(results: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
    failure_results = [result for result in results if result["classification"] != cfg["classification"]["no_diff"]]
    classification_counts = Counter(result["classification"] for result in failure_results)
    grouped_rows = {
        classification: [
            build_failure_case_summary(result)
            for result in sorted(
                (row for row in failure_results if row["classification"] == classification),
                key=lambda row: row["program_id"],
            )
        ]
        for classification in sorted(classification_counts)
    }
    payload = {
        "workflow": cfg["workflow"],
        "campaign": "partial",
        "total_results": len(results),
        "failed_results": len(failure_results),
        "classification_counts": dict(classification_counts),
        "failures_by_classification": grouped_rows,
    }
    dump_json(report_path("partial-failure-report.json", cfg=cfg), payload)
    lines = [
        f"# {cfg['workflow']} partial failure report",
        "",
        f"- total_results: {payload['total_results']}",
        f"- failed_results: {payload['failed_results']}",
        "",
        "## classification counts",
    ]
    if classification_counts:
        for classification, count in sorted(classification_counts.items()):
            lines.append(f"- {classification}: {count}")
    else:
        lines.append("- none: 0")
    report_path("partial-failure-report.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    results = reconstruct_completed_results(cfg)
    dump_jsonl(report_path("partial-campaign-results.jsonl", cfg=cfg), results)
    write_partial_summary(results, cfg)
    write_partial_failure_report(results, cfg)
    syscall_results = [
        result
        for result in results
        if result["classification"]
        in {
            cfg["classification"]["bug_likely"],
            cfg["classification"]["unsupported_feature"],
        }
    ]
    write_partial_syscall_summary(build_syscall_summary(cfg, syscall_results, campaign="partial"), cfg)


if __name__ == "__main__":
    main()
