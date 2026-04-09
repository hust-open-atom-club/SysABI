#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, current_phase, dump_json, load_json, load_jsonl, report_path, resolve_repo_path


def main() -> None:
    phase = "phase1"
    if len(sys.argv) == 3 and sys.argv[1] == "--phase":
        phase = sys.argv[2]
    configure_runtime(phase=phase)
    cfg = config()
    campaign_results = load_jsonl(report_path("campaign-results.jsonl", cfg=cfg))
    build_summary = load_json(report_path("build-summary.json", cfg=cfg))
    classification_counts: dict[str, int] = {}
    for result in campaign_results:
        classification_counts[result["classification"]] = classification_counts.get(result["classification"], 0) + 1

    total = len(campaign_results)
    candidate_ok = [result for result in campaign_results if result.get("candidate_run", {}).get("status") == "ok"]
    traces_ok = [
        result
        for result in candidate_ok
        if Path(result["candidate_run"]["trace_json_path"]).exists()
        and Path(result["reference_runs"][0]["trace_json_path"]).exists()
    ]
    canonical_ok = [
        result
        for result in candidate_ok
        if Path(result["candidate_run"]["trace_json_path"]).with_name("canonical-trace.json").exists()
        and Path(result["reference_runs"][0]["trace_json_path"]).with_name("canonical-trace.json").exists()
    ]
    candidate_runs = [result["candidate_run"] for result in campaign_results if "candidate_run" in result]
    summary = {
        "campaign": load_json(report_path("summary.json", cfg=cfg)).get("campaign", "full"),
        "phase": current_phase(cfg),
        "total": total,
        "classification_counts": classification_counts,
        "eligible_program_count": sum(1 for _ in resolve_repo_path(cfg["paths"]["eligible_file"]).open("r", encoding="utf-8")),
        "build_success_rate": build_summary["success"] / build_summary["total"] if build_summary["total"] else 0.0,
        "dual_execution_completion_rate": len(candidate_ok) / total if total else 0.0,
        "trace_generation_success_rate": len(traces_ok) / len(candidate_ok) if candidate_ok else 0.0,
        "canonicalization_success_rate": len(canonical_ok) / len(traces_ok) if traces_ok else 0.0,
        "baseline_invalid_rate": classification_counts.get(cfg["classification"]["baseline_invalid"], 0) / total if total else 0.0,
        "candidate_runner_kinds": sorted({run.get("runner_kind") for run in candidate_runs if run.get("runner_kind")}),
        "candidate_kernel_builds": sorted({run.get("kernel_build") for run in candidate_runs if run.get("kernel_build")}),
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

    dump_json(report_path("summary.json", cfg=cfg), summary)
    lines = [
        f"# {summary['phase']} {summary['campaign']} summary",
        "",
        f"- total: {summary['total']}",
        f"- eligible_program_count: {summary['eligible_program_count']}",
        f"- build_success_rate: {summary['build_success_rate']:.3f}",
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
    for key, value in sorted(summary["classification_counts"].items()):
        lines.append(f"- {key}: {value}")
    report_path("summary.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")
    signoff_lines = [
        f"# {summary['phase']} sign-off",
        "",
        f"- campaign: {summary['campaign']}",
        f"- eligible_program_count: {summary['eligible_program_count']}",
    ]
    if "import_success_rate" in summary:
        signoff_lines.append(f"- import_success_rate: {summary['import_success_rate']:.3f}")
    if "derivation_success_rate" in summary:
        signoff_lines.append(f"- derivation_success_rate: {summary['derivation_success_rate']:.3f}")
    signoff_lines.extend(
        [
            f"- build_success_rate: {summary['build_success_rate']:.3f}",
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


if __name__ == "__main__":
    main()
