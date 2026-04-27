#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import threading
import time
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.classify import classify_result
from analyzer.compare import compare_canonical
from analyzer.normalize import canonicalize
from core.capabilities import capabilities_from_config
from core.workflow_contract import WorkflowContractError
from orchestrator.common import clean_dir, config, configure_runtime, dump_json, dump_jsonl, ensure_dir, load_json, load_jsonl, report_path, reports_dir, runner_profiles, set_vm_concurrency_limit
from orchestrator.stability import all_equal, canonical_trace_hash, build_status_ok
from orchestrator.vm_runner import build_root, execute_candidate_batch, execute_candidate_batch_with_context, execute_candidate_case_in_package, execute_side
from runners import build_runner
from targets.base import canonical_execution_mode
from targets.registry import get_target_adapter
from tools.render_summary import render_summary_reports


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--campaign", default="smoke")
    parser.add_argument("--eligible-file")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--candidate-batch-size", type=int)
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


def maybe_load_canonical(result) -> dict[str, object] | None:
    trace_json_path = getattr(result, "trace_json_path", None)
    external_state_path = getattr(result, "external_state_path", None)
    if not trace_json_path or not external_state_path:
        return None
    if not Path(str(trace_json_path)).exists() or not Path(str(external_state_path)).exists():
        return None
    return load_canonical(result)


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
    canonical = maybe_load_canonical(result)
    return result, canonical


def run_candidate_once_with_package(
    program_id: str,
    run_id: str,
    suffix: str,
    inject_trace: dict[str, object] | None,
    *,
    package_dir: Path | None = None,
    package_slot: int | None = None,
) -> tuple[object, dict[str, object] | None]:
    if package_dir is None or package_slot is None:
        return run_candidate_once(program_id, run_id, suffix, inject_trace)
    cfg = config()
    result = execute_candidate_case_in_package(
        program_id=program_id,
        timeout_sec=cfg["stability"]["timeout_sec"],
        run_id=f"{run_id}-{suffix}",
        package_dir=package_dir,
        slot=package_slot,
        inject_trace=inject_trace,
    )
    canonical = maybe_load_canonical(result)
    return result, canonical


def prepare_case(entry: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    cfg = config()
    adapter = get_target_adapter(cfg)
    program_id = entry["program_id"]
    prepared_case = adapter.prepare_case(entry, cfg)
    scml_preflight_status = entry.get("scml_preflight_status", "not_run")
    build_result_path = build_root(program_id) / "build-result.json"
    if not build_status_ok(build_result_path):
        result = {
            "kind": "final",
            "result": adapter.finalize_result({
                "program_id": program_id,
                "classification": "build_failure",
                "build_result_path": str(build_result_path),
                "normalized_path": entry.get("normalized_path", ""),
                "meta_path": entry.get("meta_path", ""),
                "scml_preflight_status": scml_preflight_status,
                "scml_rejection_reasons": entry.get("scml_rejection_reasons", []),
                "scml_trace_log_path": entry.get("scml_trace_log_path", ""),
                "scml_sctrace_output_path": entry.get("scml_sctrace_output_path", ""),
                "scml_preflight_run_root": entry.get("scml_preflight_run_root", ""),
                "scml_result_bucket": scml_result_bucket(
                    preflight_status=scml_preflight_status,
                    candidate_status=None,
                    classification="build_failure",
                    cfg=cfg,
                ),
            }, cfg),
        }
        return result

    inject_trace = controlled_divergence_spec(getattr(args, "controlled_divergence", False))
    run_id = next_run_id(program_id)
    reference_results = []
    reference_hashes = []

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
        result = {
            "kind": "final",
            "result": adapter.finalize_result({
                "program_id": program_id,
                "classification": cfg["classification"]["baseline_invalid"],
                "normalized_path": entry.get("normalized_path", ""),
                "meta_path": entry.get("meta_path", ""),
                "reference_runs": [result.to_dict() for result in reference_results],
                "scml_preflight_status": scml_preflight_status,
                "scml_rejection_reasons": entry.get("scml_rejection_reasons", []),
                "scml_trace_log_path": entry.get("scml_trace_log_path", ""),
                "scml_sctrace_output_path": entry.get("scml_sctrace_output_path", ""),
                "scml_preflight_run_root": entry.get("scml_preflight_run_root", ""),
                "scml_result_bucket": scml_result_bucket(
                    preflight_status=scml_preflight_status,
                    candidate_status=None,
                    classification=cfg["classification"]["baseline_invalid"],
                    cfg=cfg,
                ),
            }, cfg),
        }
        return result

    return {
        "kind": "candidate_ready",
        "entry": entry,
        "prepared_case": prepared_case,
        "program_id": program_id,
        "run_id": run_id,
        "inject_trace": inject_trace,
        "reference_results": reference_results,
        "reference_hashes": reference_hashes,
        "current_reference_canonical": current_reference_canonical,
    }


def finalize_prepared_case(
    prepared: dict[str, object],
    args: argparse.Namespace,
    candidate_result,
    candidate_canonical: dict[str, object] | None,
    *,
    candidate_package_dir: Path | None = None,
    candidate_package_slot: int | None = None,
) -> dict[str, object]:
    cfg = config()
    adapter = get_target_adapter(cfg)
    entry = prepared["entry"]
    program_id = prepared["program_id"]
    run_id = prepared["run_id"]
    inject_trace = prepared["inject_trace"]
    reference_results = list(prepared["reference_results"])
    reference_hashes = list(prepared["reference_hashes"])
    current_reference_canonical = prepared["current_reference_canonical"]
    scml_preflight_status = entry.get("scml_preflight_status", "not_run")

    candidate_results = [candidate_result]
    candidate_hashes = []
    if candidate_canonical is not None:
        candidate_hashes.append(canonical_trace_hash(candidate_canonical))

    comparison = compare_canonical(current_reference_canonical, candidate_canonical) if candidate_canonical else None
    needs_triage = candidate_result.status != "ok" or (comparison is not None and not comparison["equivalent"])

    if needs_triage:
        for attempt in range(cfg["stability"]["rerun_count"]):
            ref_rerun, ref_canonical = run_reference_once(program_id, run_id, f"ref-triage{attempt}")
            reference_results.append(ref_rerun)
            if ref_canonical is not None:
                current_reference_canonical = ref_canonical
                reference_hashes.append(canonical_trace_hash(ref_canonical))
            cand_rerun, cand_canonical = run_candidate_once_with_package(
                program_id,
                run_id,
                f"candidate-triage{attempt}",
                inject_trace,
                package_dir=candidate_package_dir,
                package_slot=candidate_package_slot,
            )
            candidate_results.append(cand_rerun)
            if cand_canonical is not None:
                candidate_hashes.append(canonical_trace_hash(cand_canonical))
                candidate_canonical = cand_canonical
                comparison = compare_canonical(current_reference_canonical, cand_canonical)

    reference_stable = all_equal(reference_hashes)
    if not reference_stable:
        return adapter.finalize_result({
            "program_id": program_id,
            "classification": cfg["classification"]["baseline_invalid"],
            "normalized_path": entry.get("normalized_path", ""),
            "meta_path": entry.get("meta_path", ""),
            "reference_runs": [result.to_dict() for result in reference_results],
            "candidate_runs": [result.to_dict() for result in candidate_results],
            "scml_preflight_status": scml_preflight_status,
            "scml_rejection_reasons": entry.get("scml_rejection_reasons", []),
            "scml_trace_log_path": entry.get("scml_trace_log_path", ""),
            "scml_sctrace_output_path": entry.get("scml_sctrace_output_path", ""),
            "scml_preflight_run_root": entry.get("scml_preflight_run_root", ""),
            "scml_result_bucket": scml_result_bucket(
                preflight_status=scml_preflight_status,
                candidate_status=candidate_results[-1].status if candidate_results else None,
                classification=cfg["classification"]["baseline_invalid"],
                comparison=None,
                cfg=cfg,
            ),
        }, cfg)

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
        "normalized_path": entry.get("normalized_path", ""),
        "meta_path": entry.get("meta_path", ""),
        "reference_runs": [result.to_dict() for result in reference_results],
        "candidate_run": candidate_results[-1].to_dict(),
        "candidate_runs": [result.to_dict() for result in candidate_results],
        "comparison": comparison,
        "scml_preflight_status": scml_preflight_status,
        "scml_rejection_reasons": entry.get("scml_rejection_reasons", []),
        "scml_trace_log_path": entry.get("scml_trace_log_path", ""),
        "scml_sctrace_output_path": entry.get("scml_sctrace_output_path", ""),
        "scml_preflight_run_root": entry.get("scml_preflight_run_root", ""),
        "candidate_package_dir": str(candidate_package_dir) if candidate_package_dir is not None else "",
        "candidate_package_slot": candidate_package_slot,
        "candidate_package_workflow": str(cfg.get("workflow", "")) if candidate_package_dir is not None else "",
        "scml_result_bucket": scml_result_bucket(
            preflight_status=scml_preflight_status,
            candidate_status=candidate_status,
            classification=classification,
            comparison=comparison,
            cfg=cfg,
        ),
    }
    if candidate_canonical:
        result["reference_canonical_hash"] = reference_hashes[-1]
        result["candidate_canonical_hash"] = canonical_trace_hash(candidate_canonical)
    result["candidate_collection"] = adapter.collect_result(candidate_results[-1].to_dict(), cfg)
    return adapter.finalize_result(result, cfg)


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


def serialize_runs(runs: list[object]) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for run in runs:
        if hasattr(run, "to_dict"):
            serialized.append(run.to_dict())
        elif isinstance(run, dict):
            serialized.append(run)
    return serialized


def infra_error_result(
    *,
    entry: dict[str, object],
    stage: str,
    exc: Exception,
    reference_results: list[object] | None = None,
    candidate_results: list[object] | None = None,
) -> dict[str, object]:
    cfg = config()
    adapter = get_target_adapter(cfg)
    scml_preflight_status = str(entry.get("scml_preflight_status", "not_run"))
    candidate_runs = serialize_runs(candidate_results or [])
    candidate_run = candidate_runs[-1] if candidate_runs else None
    result = {
        "program_id": entry["program_id"],
        "classification": "infra_error",
        "normalized_path": entry.get("normalized_path", ""),
        "meta_path": entry.get("meta_path", ""),
        "reference_runs": serialize_runs(reference_results or []),
        "candidate_runs": candidate_runs,
        "scml_preflight_status": scml_preflight_status,
        "scml_rejection_reasons": entry.get("scml_rejection_reasons", []),
        "scml_trace_log_path": entry.get("scml_trace_log_path", ""),
        "scml_sctrace_output_path": entry.get("scml_sctrace_output_path", ""),
        "scml_preflight_run_root": entry.get("scml_preflight_run_root", ""),
        "error_stage": stage,
        "error_detail": f"{type(exc).__name__}: {exc}",
        "scml_result_bucket": scml_result_bucket(
            preflight_status=scml_preflight_status,
            candidate_status=str(candidate_run.get("status")) if candidate_run else None,
            classification="infra_error",
            comparison=None,
            cfg=cfg,
        ),
    }
    if candidate_run is not None:
        result["candidate_run"] = candidate_run
    return adapter.finalize_result(result, cfg)


def prepare_case_safe(entry: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    try:
        return prepare_case(entry, args)
    except Exception as exc:
        return {
            "kind": "final",
            "result": infra_error_result(entry=entry, stage="prepare_case", exc=exc),
        }


def finalize_prepared_case_safe(
    prepared: dict[str, object],
    args: argparse.Namespace,
    candidate_result,
    candidate_canonical: dict[str, object] | None,
    *,
    candidate_package_dir: Path | None = None,
    candidate_package_slot: int | None = None,
) -> dict[str, object]:
    try:
        return finalize_prepared_case(
            prepared,
            args,
            candidate_result,
            candidate_canonical,
            candidate_package_dir=candidate_package_dir,
            candidate_package_slot=candidate_package_slot,
        )
    except Exception as exc:
        return infra_error_result(
            entry=prepared["entry"],
            stage="finalize_prepared_case",
            exc=exc,
            reference_results=list(prepared.get("reference_results", [])),
            candidate_results=[candidate_result] if candidate_result is not None else [],
        )


def scml_result_bucket(
    *,
    preflight_status: str,
    candidate_status: str | None,
    classification: str,
    comparison: dict[str, object] | None = None,
    cfg: dict[str, object],
) -> str:
    if preflight_status == "not_run":
        return ""
    if preflight_status != "passed":
        return "rejected_by_scml"
    if classification == "build_failure":
        return "passed_scml_but_candidate_failed"
    if comparison is None:
        if classification == cfg["classification"]["baseline_invalid"]:
            return "passed_scml_but_reference_failed"
        return "passed_scml_but_candidate_failed" if candidate_status != "ok" else "passed_scml_but_reference_failed"
    if comparison.get("equivalent"):
        return "passed_scml_and_no_diff"
    return "passed_scml_and_diverged"


def canonical_trace_for_run(run: dict[str, object]) -> dict[str, object] | None:
    trace_json_path = run.get("trace_json_path")
    if not trace_json_path:
        return None
    canonical_path = Path(str(trace_json_path)).with_name("canonical-trace.json")
    if not canonical_path.exists():
        return None
    return load_json(canonical_path)


def canonical_trace_path_for_run(run: dict[str, object] | None) -> str:
    if not run:
        return ""
    trace_json_path = run.get("trace_json_path")
    if not trace_json_path:
        return ""
    return str(Path(str(trace_json_path)).with_name("canonical-trace.json"))


def event_by_index(canonical_trace: dict[str, object] | None, event_index: int | None) -> dict[str, object] | None:
    if canonical_trace is None or event_index is None:
        return None
    for event in canonical_trace["events"]:
        if event["index"] == event_index:
            return event
    return None


def load_full_syscall_list(result: dict[str, object]) -> list[str]:
    meta_path = result.get("meta_path")
    if not meta_path:
        return []
    path = Path(str(meta_path))
    if not path.exists():
        return []
    return list(load_json(path).get("full_syscall_list", []))


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


def first_divergence_details(result: dict[str, object]) -> tuple[int | None, str | None]:
    reference_run = latest_reference_run(result)
    candidate_run = latest_candidate_run(result)
    reference_canonical = canonical_trace_for_run(reference_run) if reference_run else None
    candidate_canonical = canonical_trace_for_run(candidate_run) if candidate_run else None
    comparison = result.get("comparison") or {}
    first_divergence_index = comparison.get("first_divergence_index")
    reference_event = event_by_index(reference_canonical, first_divergence_index)
    candidate_event = event_by_index(candidate_canonical, first_divergence_index)
    first_divergence_syscall = None
    if reference_event is not None:
        first_divergence_syscall = reference_event["syscall_name"]
    elif candidate_event is not None:
        first_divergence_syscall = candidate_event["syscall_name"]
    elif comparison.get("final_state_equal") is False:
        first_divergence_syscall = "final_state_only"
    elif comparison.get("process_exit_equal") is False:
        first_divergence_syscall = "process_exit_only"
    return first_divergence_index, first_divergence_syscall


def write_bug_likely_reports(results: list[dict[str, object]], cfg: dict[str, object]) -> None:
    bug_results = [result for result in results if result["classification"] == cfg["classification"]["bug_likely"]]
    bug_root = clean_dir(report_path("bug_likely", cfg=cfg))
    testcase_root = ensure_dir(bug_root / "testcases")
    case_root = ensure_dir(bug_root / "cases")

    index_rows: list[dict[str, object]] = []
    syscall_counts: Counter[str] = Counter()
    for result in sorted(bug_results, key=lambda row: row["program_id"]):
        program_id = result["program_id"]
        reference_run = result["reference_runs"][-1]
        candidate_run = result["candidate_run"]
        reference_canonical = canonical_trace_for_run(reference_run)
        candidate_canonical = canonical_trace_for_run(candidate_run)
        comparison = result.get("comparison") or {}
        first_divergence_index, first_divergence_syscall = first_divergence_details(result)
        reference_event = event_by_index(reference_canonical, first_divergence_index)
        candidate_event = event_by_index(candidate_canonical, first_divergence_index)
        if first_divergence_syscall is not None:
            syscall_counts[first_divergence_syscall] += 1

        testcase_source = Path(str(result.get("normalized_path", "")))
        testcase_copy_path = testcase_root / f"{program_id}.syz"
        if testcase_source.exists():
            shutil.copy2(testcase_source, testcase_copy_path)

        case_summary = {
            "program_id": program_id,
            "classification": result["classification"],
            "scml_result_bucket": result.get("scml_result_bucket"),
            "normalized_path": result.get("normalized_path", ""),
            "testcase_copy_path": str(testcase_copy_path) if testcase_copy_path.exists() else "",
            "full_syscall_list": load_full_syscall_list(result),
            "first_divergence_index": first_divergence_index,
            "first_divergence_syscall_name": first_divergence_syscall,
            "reference_event": reference_event,
            "candidate_event": candidate_event,
            "reference_console_log_path": reference_run.get("console_log_path", ""),
            "candidate_console_log_path": candidate_run.get("console_log_path", ""),
            "reference_trace_json_path": reference_run.get("trace_json_path", ""),
            "candidate_trace_json_path": candidate_run.get("trace_json_path", ""),
            "reference_canonical_trace_path": (
                str(Path(reference_run["trace_json_path"]).with_name("canonical-trace.json"))
                if reference_run.get("trace_json_path")
                else ""
            ),
            "candidate_canonical_trace_path": (
                str(Path(candidate_run["trace_json_path"]).with_name("canonical-trace.json"))
                if candidate_run.get("trace_json_path")
                else ""
            ),
            "comparison": comparison,
        }
        dump_json(case_root / program_id / "summary.json", case_summary)
        index_rows.append(case_summary)

    summary = {
        "workflow": cfg["workflow"],
        "bug_likely_count": len(bug_results),
        "first_divergence_syscall_counts": dict(syscall_counts),
    }
    dump_json(bug_root / "summary.json", summary)
    dump_jsonl(bug_root / "index.jsonl", index_rows)
    lines = [
        f"# {cfg['workflow']} bug-likely summary",
        "",
        f"- bug_likely_count: {summary['bug_likely_count']}",
        "",
        "## first divergence syscall counts",
    ]
    if syscall_counts:
        for name, count in sorted(syscall_counts.items()):
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none: 0")
    (bug_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_failure_case_summary(result: dict[str, object]) -> dict[str, object]:
    reference_run = latest_reference_run(result)
    candidate_run = latest_candidate_run(result)
    first_divergence_index, first_divergence_syscall_name = first_divergence_details(result)
    return {
        "program_id": result["program_id"],
        "classification": result["classification"],
        "scml_preflight_status": result.get("scml_preflight_status", "not_run"),
        "normalized_path": result.get("normalized_path", ""),
        "meta_path": result.get("meta_path", ""),
        "reference_status": reference_run.get("status") if reference_run else None,
        "candidate_status": candidate_run.get("status") if candidate_run else None,
        "first_divergence_index": first_divergence_index,
        "first_divergence_syscall_name": first_divergence_syscall_name,
        "reference_console_log_path": reference_run.get("console_log_path", "") if reference_run else "",
        "candidate_console_log_path": candidate_run.get("console_log_path", "") if candidate_run else "",
        "reference_trace_json_path": reference_run.get("trace_json_path", "") if reference_run else "",
        "candidate_trace_json_path": candidate_run.get("trace_json_path", "") if candidate_run else "",
        "reference_canonical_trace_path": canonical_trace_path_for_run(reference_run),
        "candidate_canonical_trace_path": canonical_trace_path_for_run(candidate_run),
        "comparison": result.get("comparison"),
    }


def write_failure_reports(results: list[dict[str, object]], campaign: str) -> None:
    cfg = config()
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
        "campaign": campaign,
        "total_results": len(results),
        "failed_results": len(failure_results),
        "classification_counts": dict(classification_counts),
        "failures_by_classification": grouped_rows,
    }
    dump_json(report_path("failure-report.json", cfg=cfg), payload)

    lines = [
        f"# {cfg['workflow']} {campaign} failure report",
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
    for classification, cases in grouped_rows.items():
        lines.extend(["", f"## {classification}"])
        for case in cases:
            lines.append(
                "- "
                f"{case['program_id']}: "
                f"reference_status={case['reference_status'] or 'n/a'}, "
                f"candidate_status={case['candidate_status'] or 'n/a'}, "
                f"first_divergence_syscall={case['first_divergence_syscall_name'] or 'n/a'}, "
                f"testcase={case['normalized_path'] or 'n/a'}, "
                f"reference_console={case['reference_console_log_path'] or 'n/a'}, "
                f"candidate_console={case['candidate_console_log_path'] or 'n/a'}, "
                f"reference_trace={case['reference_trace_json_path'] or 'n/a'}, "
                f"candidate_trace={case['candidate_trace_json_path'] or 'n/a'}"
            )
    report_path("failure-report.md", cfg=cfg).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_post_run_reports(results: list[dict[str, object]], campaign: str, *, jobs: int | None = None) -> None:
    write_summary(results, campaign, jobs=jobs)
    render_summary_reports(campaign=campaign)
    write_failure_reports(results, campaign)


def schedule_one(entry: dict[str, object], args: argparse.Namespace) -> dict[str, object]:
    prepared = prepare_case_safe(entry, args)
    if prepared["kind"] == "final":
        return prepared["result"]
    try:
        candidate_result, candidate_canonical = run_candidate_once(
            prepared["program_id"],
            prepared["run_id"],
            "candidate0",
            prepared["inject_trace"],
        )
    except Exception as exc:
        return infra_error_result(
            entry=prepared["entry"],
            stage="run_candidate_once",
            exc=exc,
            reference_results=list(prepared.get("reference_results", [])),
        )
    return finalize_prepared_case_safe(prepared, args, candidate_result, candidate_canonical)


def selected_entries(args: argparse.Namespace) -> list[dict[str, object]]:
    rows = load_jsonl(args.eligible_file)
    if args.program_id:
        rows = [row for row in rows if row["program_id"] == args.program_id]
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


def ensure_entries_built(entries: list[dict[str, object]]) -> None:
    missing_program_ids: list[str] = []
    for entry in entries:
        program_id = str(entry["program_id"])
        if not (build_root(program_id) / "build-result.json").exists():
            missing_program_ids.append(program_id)
    if not missing_program_ids:
        return
    preview = ", ".join(missing_program_ids[:5])
    if len(missing_program_ids) > 5:
        preview += ", ..."
    raise SystemExit(
        "missing testcase builds for "
        f"{len(missing_program_ids)} program(s); run "
        f"`python3 tools/prog2c_wrap.py --workflow {config()['workflow']} --limit {len(entries)}` "
        f"or `make build-workflow WORKFLOW={config()['workflow']}` first. "
        f"sample program_ids: {preview}"
    )


def effective_jobs(args: argparse.Namespace, cfg: dict[str, object]) -> int:
    if args.jobs is not None:
        return max(1, args.jobs)
    parallel = cfg.get("parallel", {})
    if isinstance(parallel, dict):
        return max(1, int(parallel.get("jobs", 1)))
    return 1


def effective_candidate_batch_size(args: argparse.Namespace, cfg: dict[str, object]) -> int:
    explicit = getattr(args, "candidate_batch_size", None)
    if explicit is not None:
        return max(1, explicit)
    parallel = cfg.get("parallel", {})
    if isinstance(parallel, dict):
        return max(1, int(parallel.get("candidate_batch_size", 1)))
    return 1


def candidate_batching_enabled(args: argparse.Namespace, cfg: dict[str, object]) -> bool:
    capabilities = capabilities_from_config(cfg)
    if not capabilities.supports_batch_execution:
        return False
    if effective_candidate_batch_size(args, cfg) <= 1:
        return False
    profile = runner_profiles()["candidate"]
    if profile.get("kind") != "command":
        raise WorkflowContractError(
            "candidate batch execution requires a command runner profile"
        )
    adapter = get_target_adapter(cfg)
    batching_mode = canonical_execution_mode(str(profile.get("command_batching_mode")) if profile.get("command_batching_mode") is not None else None)
    if batching_mode is None:
        raise WorkflowContractError("candidate batch execution requires an explicit command_batching_mode")
    if batching_mode not in adapter.execution_modes(cfg):
        raise WorkflowContractError(
            f"candidate runner batching mode {batching_mode!r} is not supported by target {cfg.get('target')!r}"
        )
    return True


def _max_concurrent_vms() -> int:
    cfg = config()
    return int(cfg.get("parallel", {}).get("max_concurrent_vms", cfg.get("parallel", {}).get("jobs", 1)))


def schedule_entries_with_candidate_batch(entries: list[dict[str, object]], args: argparse.Namespace, jobs: int) -> list[dict[str, object]]:
    if not entries:
        return []

    cfg = config()
    prepared_results: list[dict[str, object] | None] = [None] * len(entries)
    if jobs <= 1 or len(entries) <= 1:
        for index, entry in enumerate(entries):
            prepared_results[index] = prepare_case_safe(entry, args)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
            future_map = {
                executor.submit(prepare_case_safe, entry, args): index
                for index, entry in enumerate(entries)
            }
            for future in concurrent.futures.as_completed(future_map):
                prepared_results[future_map[future]] = future.result()

    results: list[dict[str, object] | None] = [None] * len(entries)
    pending: list[tuple[int, dict[str, object]]] = []
    for index, prepared in enumerate(prepared_results):
        if prepared is None:
            continue
        if prepared["kind"] == "final":
            results[index] = prepared["result"]
        else:
            pending.append((index, prepared))

    batch_size = effective_candidate_batch_size(args, cfg)
    for offset in range(0, len(pending), batch_size):
        chunk = pending[offset : offset + batch_size]
        try:
            batch_results, package_dir, slot_by_program = execute_candidate_batch_with_context(
                batch_cases=[
                    {
                        "program_id": prepared["program_id"],
                        "run_id": f"{prepared['run_id']}-candidate0",
                        "inject_trace": prepared["inject_trace"],
                        "prepared_case": prepared.get("prepared_case"),
                    }
                    for _, prepared in chunk
                ],
                timeout_sec=cfg["stability"]["timeout_sec"],
                max_workers=jobs,
            )
        except Exception as exc:
            for index, prepared in chunk:
                results[index] = infra_error_result(
                    entry=prepared["entry"],
                    stage="execute_candidate_batch_with_context",
                    exc=exc,
                    reference_results=list(prepared.get("reference_results", [])),
                )
            continue
        finalized_chunk: list[tuple[int, dict[str, object]] | None] = [None] * len(chunk)

        def finalize_chunk_case(chunk_item: tuple[int, dict[str, object]]) -> tuple[int, dict[str, object]]:
            index, prepared = chunk_item
            try:
                candidate_result = batch_results[prepared["program_id"]]
                candidate_canonical = maybe_load_canonical(candidate_result)
            except Exception as exc:
                return index, infra_error_result(
                    entry=prepared["entry"],
                    stage="load_candidate_batch_result",
                    exc=exc,
                    reference_results=list(prepared.get("reference_results", [])),
                )
            return index, finalize_prepared_case_safe(
                prepared,
                args,
                candidate_result,
                candidate_canonical,
                candidate_package_dir=package_dir,
                candidate_package_slot=slot_by_program.get(prepared["program_id"]),
            )

        if jobs <= 1 or len(chunk) <= 1:
            for chunk_index, chunk_item in enumerate(chunk):
                finalized_chunk[chunk_index] = finalize_chunk_case(chunk_item)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(jobs, len(chunk))) as executor:
                future_map = {
                    executor.submit(finalize_chunk_case, chunk_item): chunk_index
                    for chunk_index, chunk_item in enumerate(chunk)
                }
                for future in concurrent.futures.as_completed(future_map):
                    finalized_chunk[future_map[future]] = future.result()

        for finalized in finalized_chunk:
            if finalized is None:
                continue
            index, finalized_result = finalized
            results[index] = finalized_result
    return [result for result in results if result is not None]


def schedule_entries(entries: list[dict[str, object]], args: argparse.Namespace, jobs: int) -> list[dict[str, object]]:
    cfg = config()
    if candidate_batching_enabled(args, cfg):
        return schedule_entries_with_candidate_batch(entries, args, jobs)
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


def write_summary(results: list[dict[str, object]], campaign: str, *, jobs: int | None = None) -> None:
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
        "jobs": jobs,
        "max_concurrent_vms": _max_concurrent_vms(),
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
    write_bug_likely_reports(results, cfg)


def _run_healthchecks(cfg: dict[str, Any]) -> None:
    """Run runner and target adapter healthchecks before campaign start."""
    profiles = runner_profiles()
    for side in ("reference", "candidate"):
        profile = profiles.get(side)
        if profile is None:
            continue
        runner = build_runner(profile)
        result = runner.healthcheck()
        if result.get("status") != "ok":
            raise WorkflowContractError(
                f"{side} runner healthcheck failed: {result}"
            )
    adapter = get_target_adapter(cfg)
    if adapter.requires_campaign_healthcheck(cfg):
        adapter.healthcheck(SimpleNamespace(healthcheck=True))


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    _run_healthchecks(cfg)
    set_vm_concurrency_limit(_max_concurrent_vms())
    if not args.eligible_file:
        args.eligible_file = cfg["paths"]["eligible_file"]
    entries = selected_entries(args)
    ensure_entries_built(entries)
    jobs = effective_jobs(args, cfg)
    results = schedule_entries(entries, args, jobs)
    dump_jsonl(report_path("campaign-results.jsonl", cfg=cfg), results)
    write_post_run_reports(results, args.campaign, jobs=jobs)


if __name__ == "__main__":
    main()
