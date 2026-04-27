#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.compare import compare_canonical
from analyzer.normalize import canonicalize
from core.capabilities import capabilities_from_config
from orchestrator.common import config, configure_runtime, dump_json, load_json, load_jsonl, path_resolver, read_text, report_path, runner_profiles, temp_dir, write_text
from orchestrator.syzkaller import inspect_program, mutate_drop_call
from orchestrator.vm_runner import execute_candidate_batch_with_context, execute_candidate_case_in_package, execute_side
from targets.asterinas.scml import AsterinasSCMLGate, AsterinasSCMLSource
from tools.prog2c_wrap import build_one


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--fixture", default="controlled_divergence")
    parser.add_argument("--program-id")
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


def scml_overlay_enabled(cfg: dict[str, object]) -> bool:
    capabilities = capabilities_from_config(cfg)
    return capabilities.supports_preflight


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


def select_scml_campaign_row(
    campaign_results: list[dict[str, object]],
    *,
    program_id: str | None = None,
) -> dict[str, object]:
    if program_id:
        campaign_results = [row for row in campaign_results if row["program_id"] == program_id]
    else:
        campaign_results = [
            row
            for row in campaign_results
            if row.get("scml_result_bucket") == "passed_scml_and_diverged"
        ]
    if not campaign_results:
        raise SystemExit(
            "asterinas_scml reduce_case requires a campaign result with "
            "`scml_result_bucket=passed_scml_and_diverged`"
        )
    selected = campaign_results[0]
    if selected.get("scml_preflight_status") != "passed":
        raise SystemExit("selected campaign result is not SCML-passed")
    if selected.get("scml_result_bucket") != "passed_scml_and_diverged":
        raise SystemExit("selected campaign result is not a passed_scml_and_diverged case")
    return selected


def scml_reduction_invariants_hold(
    comparison: dict[str, object],
    reference_canonical: dict[str, object],
    preflight_status: str,
) -> bool:
    if comparison.get("equivalent"):
        return False
    if preflight_status != "passed":
        return False
    return map_event_index_to_program_call(reference_canonical, comparison["first_divergence_index"]) is not None


def find_campaign_package_context(program_id: str, *, workflow: str) -> dict[str, object] | None:
    package_root = path_resolver(config(workflow=workflow)).candidate_initramfs_packages_dir()
    if not package_root.exists():
        return None
    matches: list[dict[str, object]] = []
    for package_dir in package_root.iterdir():
        manifest_path = package_dir / "package-manifest.json"
        if not manifest_path.exists():
            continue
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if payload.get("workflow") != workflow:
            continue
        cases = payload.get("cases", [])
        if not isinstance(cases, list):
            continue
        for case in cases:
            if case.get("program_id") != program_id:
                continue
            matches.append(
                {
                "package_dir": str(package_dir.resolve()),
                "slot": int(case["slot"]),
                "workflow": workflow,
                }
            )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(
            "multiple campaign package matches found for reducer replay; "
            "package provenance must be carried explicitly from campaign results"
        )
    return None


def run_case(
    program_path: Path,
    *,
    campaign_package_dir: Path | None = None,
    campaign_package_slot: int | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    cfg = config()
    info = inspect_program(program_path)
    entry = {
        "program_id": info["program_id"],
        "normalized_path": str(program_path),
    }
    build_result = build_one(entry)
    run_id = f"reduce-{info['program_id'][:12]}-{time.time_ns()}"
    reference = execute_side(
        program_id=info["program_id"],
        side="reference",
        timeout_sec=cfg["stability"]["timeout_sec"],
        run_id=f"{run_id}-reference",
    )
    reference_canonical = canonicalize(load_json(reference.trace_json_path), load_json(reference.external_state_path))
    package_dir = campaign_package_dir
    package_slot = campaign_package_slot
    if scml_overlay_enabled(cfg):
        if package_dir is not None and package_slot is not None and not package_binary_matches_current_build(
            package_dir,
            package_slot,
            build_result,
        ):
            package_dir = None
            package_slot = None
        if package_dir is not None and package_slot is not None:
            candidate = execute_candidate_case_in_package(
                program_id=info["program_id"],
                timeout_sec=cfg["stability"]["timeout_sec"],
                run_id=f"{run_id}-candidate",
                package_dir=package_dir,
                slot=package_slot,
                inject_trace=None,
            )
        else:
            candidate_results, package_dir, slot_by_program = execute_candidate_batch_with_context(
                batch_cases=[
                    {
                        "program_id": info["program_id"],
                        "run_id": f"{run_id}-candidate",
                        "inject_trace": None,
                    }
                ],
                timeout_sec=cfg["stability"]["timeout_sec"],
                max_workers=1,
            )
            candidate = candidate_results[info["program_id"]]
            package_slot = slot_by_program[info["program_id"]]
    else:
        candidate = execute_side(
            program_id=info["program_id"],
            side="candidate",
            timeout_sec=cfg["stability"]["timeout_sec"],
            run_id=f"{run_id}-candidate",
            inject_trace=divergence_spec(),
        )
    candidate_canonical = canonicalize(load_json(candidate.trace_json_path), load_json(candidate.external_state_path))
    reference_canonical_path = Path(reference.trace_json_path).with_name("canonical-trace.json")
    candidate_canonical_path = Path(candidate.trace_json_path).with_name("canonical-trace.json")
    dump_json(reference_canonical_path, reference_canonical)
    dump_json(candidate_canonical_path, candidate_canonical)
    comparison = compare_canonical(reference_canonical, candidate_canonical)
    if scml_overlay_enabled(cfg) and (candidate.status != "ok" or not comparison["equivalent"]):
        for attempt in range(cfg["stability"]["rerun_count"]):
            reference = execute_side(
                program_id=info["program_id"],
                side="reference",
                timeout_sec=cfg["stability"]["timeout_sec"],
                run_id=f"{run_id}-reference-triage{attempt}",
            )
            reference_canonical = canonicalize(load_json(reference.trace_json_path), load_json(reference.external_state_path))
            if package_dir is None or package_slot is None:
                raise SystemExit("missing packaged candidate context for asterinas_scml reducer replay")
            candidate = execute_candidate_case_in_package(
                program_id=info["program_id"],
                timeout_sec=cfg["stability"]["timeout_sec"],
                run_id=f"{run_id}-candidate-triage{attempt}",
                package_dir=package_dir,
                slot=package_slot,
                inject_trace=None,
            )
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


def package_binary_matches_current_build(
    package_dir: Path,
    slot: int,
    build_result: dict[str, object],
) -> bool:
    candidate_binary = build_result.get("candidate_testcase_bin") or build_result.get("testcase_bin")
    if not candidate_binary:
        return False
    candidate_binary_path = Path(str(candidate_binary))
    if not candidate_binary_path.exists():
        return False
    manifest_path = package_dir / "package-manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        return False
    for case in cases:
        if not isinstance(case, dict):
            continue
        if int(case.get("slot", -1)) != slot:
            continue
        expected_sha = case.get("binary_sha256")
        if not isinstance(expected_sha, str) or not expected_sha:
            return False
        actual_sha = hashlib.sha256(candidate_binary_path.read_bytes()).hexdigest()
        return actual_sha == expected_sha
    return False


def seed_program(fixture_name: str, program_id: str | None = None) -> tuple[Path, dict[str, object] | None]:
    cfg = config()
    if scml_overlay_enabled(cfg):
        campaign_results_path = report_path("campaign-results.jsonl", cfg=cfg)
        if not campaign_results_path.exists():
            raise SystemExit("missing campaign-results.jsonl for asterinas_scml reduce_case")
        campaign_results = load_jsonl(campaign_results_path)
        selected = select_scml_campaign_row(campaign_results, program_id=program_id)
        eligible_rows = load_jsonl(cfg["paths"]["eligible_file"])
        eligible_index = {row["program_id"]: row for row in eligible_rows}
        eligible_entry = eligible_index.get(selected["program_id"])
        selected_entry = {**selected, **eligible_entry} if eligible_entry is not None else dict(selected)
        normalized_path = selected_entry.get("normalized_path")
        if not normalized_path:
            raise SystemExit(
                "selected campaign result is not present in the final eligible corpus and does not carry its own testcase path"
            )
        package_context = None
        if selected.get("candidate_package_dir") and selected.get("candidate_package_slot") is not None:
            package_context = {
                "package_dir": selected["candidate_package_dir"],
                "slot": int(selected["candidate_package_slot"]),
            }
        else:
            package_context = find_campaign_package_context(
                selected["program_id"],
                workflow=cfg["workflow"],
            )
        return Path(str(normalized_path)), {
            **selected_entry,
            **(
                {
                    "campaign_package_dir": package_context["package_dir"],
                    "campaign_package_slot": package_context["slot"],
                }
                if package_context
                else {}
            ),
        }
    eligible = load_jsonl(cfg["paths"]["eligible_file"])
    if program_id:
        eligible = [row for row in eligible if row["program_id"] == program_id]
        if not eligible:
            raise SystemExit(f"{cfg['paths']['eligible_file']} is empty")
        return Path(eligible[0]["normalized_path"]), eligible[0]
    fixture = Path("tests/fixtures/corpus") / f"{fixture_name}.syz"
    if fixture.exists():
        return fixture, None
    if not eligible:
        raise SystemExit(f"{cfg['paths']['eligible_file']} is empty")
    return Path(eligible[0]["normalized_path"]), eligible[0]


def scml_preflight_for_program(
    program_path: Path,
    *,
    require_zero_exit: bool = False,
) -> dict[str, object]:
    cfg = config()
    taxonomy = cfg.get("preflight", {}).get("rejection_taxonomy", {})
    timeout_sec = int(cfg.get("preflight", {}).get("timeout_sec", cfg["stability"]["timeout_sec"]))
    source = AsterinasSCMLSource(cfg)
    gate = AsterinasSCMLGate(cfg=cfg, manifest_index=source.load_manifest_index())
    info = inspect_program(program_path)
    entry = {
        "program_id": info["program_id"],
        "normalized_path": str(program_path),
    }
    build_result = build_one(entry)
    if build_result["status"] != "ok":
        raise SystemExit("failed to build minimized testcase for SCML preflight")
    artifact_root = report_path("minimized-preflight", info["program_id"], cfg=cfg)
    artifact_root.mkdir(parents=True, exist_ok=True)
    strace_log_path = artifact_root / "preflight.strace.log"
    sctrace_output_path = artifact_root / "preflight.sctrace.txt"
    binary_path = Path(build_result["testcase_bin"])
    try:
        strace_run = subprocess.run(
            ["strace", "-yy", "-f", "-o", str(strace_log_path), str(binary_path)],
            cwd=artifact_root,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {
            "program_id": info["program_id"],
            "status": "rejected_by_scml",
            "reasons": [taxonomy.get("preflight_runtime_timeout", "preflight_runtime_timeout")],
            "trace_log_path": str(strace_log_path),
            "sctrace_output_path": str(sctrace_output_path),
            "reducer_replay_mode": "fresh_replay",
            "reducer_replay_recovery_reason": "",
        }
    try:
        sctrace_run = subprocess.run(
            source.sctrace_command(source.scml_files(), strace_log_path),
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {
            "program_id": info["program_id"],
            "status": "rejected_by_scml",
            "reasons": [taxonomy.get("preflight_matcher_timeout", "preflight_matcher_timeout")],
            "trace_log_path": str(strace_log_path),
            "sctrace_output_path": str(sctrace_output_path),
            "reducer_replay_mode": "fresh_replay",
            "reducer_replay_recovery_reason": "",
        }
    target_syscalls = {full_name.split("$", 1)[0] for full_name in info["full_syscall_list"]}
    output_lines = gate.parse_sctrace_lines(sctrace_run.stdout, sctrace_run.stderr)
    relevant_output_lines = gate.relevant_output_lines(output_lines, target_syscalls=target_syscalls)
    sctrace_output_path.write_text("\n".join(relevant_output_lines) + ("\n" if relevant_output_lines else ""), encoding="utf-8")
    reasons: list[str] = []
    for line in relevant_output_lines:
        reasons.extend(gate.classify_line(line, target_syscalls=target_syscalls))
    if strace_run.returncode != 0:
        reasons.append(taxonomy.get("preflight_runtime_failure", "preflight_runtime_failure"))
    if sctrace_run.returncode != 0:
        reasons.append(taxonomy.get("scml_parser_gap", "scml_parser_gap"))
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    return {
        "program_id": info["program_id"],
        "status": "passed" if not reasons else "rejected_by_scml",
        "reasons": reasons,
        "trace_log_path": str(strace_log_path),
        "sctrace_output_path": str(sctrace_output_path),
        "reducer_replay_mode": "fresh_replay",
        "reducer_replay_recovery_reason": "",
    }


def campaign_package_context(source_entry: dict[str, object] | None) -> tuple[Path | None, int | None]:
    if source_entry is None:
        return None, None
    package_dir = source_entry.get("campaign_package_dir")
    package_slot = source_entry.get("campaign_package_slot")
    if not package_dir or package_slot is None:
        return None, None
    return Path(str(package_dir)), int(package_slot)


def latest_saved_candidate_run(source_entry: dict[str, object]) -> dict[str, object] | None:
    candidate_run = source_entry.get("candidate_run")
    if isinstance(candidate_run, dict):
        return candidate_run
    candidate_runs = source_entry.get("candidate_runs", [])
    if isinstance(candidate_runs, list) and candidate_runs:
        saved = candidate_runs[-1]
        if isinstance(saved, dict):
            return saved
    return None


def latest_saved_reference_run(source_entry: dict[str, object]) -> dict[str, object] | None:
    reference_runs = source_entry.get("reference_runs", [])
    if isinstance(reference_runs, list) and reference_runs:
        saved = reference_runs[-1]
        if isinstance(saved, dict):
            return saved
    return None


def load_saved_canonical_trace(run: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(run, dict):
        return None
    trace_json_path = run.get("trace_json_path")
    if not trace_json_path:
        return None
    canonical_path = Path(str(trace_json_path)).with_name("canonical-trace.json")
    if not canonical_path.exists():
        return None
    return load_json(canonical_path)


def recorded_source_evidence(
    current_info: dict[str, object],
    source_entry: dict[str, object] | None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]] | None:
    if source_entry is None:
        return None
    comparison = source_entry.get("comparison")
    if not isinstance(comparison, dict):
        return None
    reference_run = latest_saved_reference_run(source_entry)
    candidate_run = latest_saved_candidate_run(source_entry)
    reference_canonical = load_saved_canonical_trace(reference_run)
    candidate_canonical = load_saved_canonical_trace(candidate_run)
    if reference_run is None or candidate_run is None or reference_canonical is None or candidate_canonical is None:
        return None
    preflight = {
        "program_id": current_info["program_id"],
        "status": source_entry.get("scml_preflight_status", "unknown"),
        "reasons": source_entry.get("scml_rejection_reasons", []),
        "trace_log_path": source_entry.get("scml_trace_log_path", ""),
        "sctrace_output_path": source_entry.get("scml_sctrace_output_path", ""),
        "reducer_replay_mode": "recovery_only",
        "reducer_replay_recovery_reason": "",
    }
    if not scml_reduction_invariants_hold(comparison, reference_canonical, str(preflight["status"])):
        return None
    runs = {
        "reference": reference_run,
        "candidate": candidate_run,
        "reference_canonical": reference_canonical,
        "candidate_canonical": candidate_canonical,
        "reference_canonical_path": str(Path(str(reference_run["trace_json_path"])).with_name("canonical-trace.json")),
        "candidate_canonical_path": str(Path(str(candidate_run["trace_json_path"])).with_name("canonical-trace.json")),
    }
    return current_info, comparison, runs, preflight


def require_recorded_source_evidence(
    current_info: dict[str, object],
    source_entry: dict[str, object] | None,
    *,
    reason: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    fallback = recorded_source_evidence(current_info, source_entry)
    if fallback is None:
        raise SystemExit(
            "asterinas_scml reduce_case requires the selected source testcase to already be "
            "a passed_scml_and_diverged case with a valid syscall divergence index"
        )
    fallback_info, comparison, runs, preflight = fallback
    tagged_preflight = dict(preflight)
    tagged_preflight["reducer_replay_mode"] = "recovery_only"
    tagged_preflight["reducer_replay_recovery_reason"] = reason
    return fallback_info, comparison, runs, tagged_preflight


def _confirm_divergence(
    program_path: Path,
    *,
    rerun_count: int = 3,
    campaign_package_dir: Path | None = None,
    campaign_package_slot: int | None = None,
) -> tuple[bool, list[dict[str, object]]]:
    """Rerun a trial program multiple times to confirm divergence stability.

    Returns (all_diverged, runs) where all_diverged is True only if every
    rerun produced a non-equivalent comparison.
    """
    confirmation_runs: list[dict[str, object]] = []
    for attempt in range(rerun_count):
        try:
            confirm_info, confirm_comparison, confirm_runs = run_case(
                program_path,
                campaign_package_dir=campaign_package_dir,
                campaign_package_slot=campaign_package_slot,
            )
        except SystemExit:
            return False, confirmation_runs
        confirmation_runs.append({
            "attempt": attempt,
            "equivalent": confirm_comparison["equivalent"],
            "first_divergence_index": confirm_comparison.get("first_divergence_index"),
            "reference_status": confirm_runs["reference"].get("status"),
            "candidate_status": confirm_runs["candidate"].get("status"),
        })
        if confirm_comparison["equivalent"]:
            return False, confirmation_runs
    return True, confirmation_runs


def greedy_reduce(
    initial_program: Path,
    *,
    source_entry: dict[str, object] | None = None,
) -> tuple[Path, dict[str, object], dict[str, object], dict[str, object], dict[str, object] | None, bool, list[dict[str, object]]]:
    cfg = config()
    current_text = read_text(initial_program)
    reduction_blocked = False
    confirmed = True
    current_info: dict[str, object]
    current_comparison: dict[str, object]
    current_runs: dict[str, object]
    current_preflight: dict[str, object] | None = None
    initial_package_dir, initial_package_slot = campaign_package_context(source_entry)
    try:
        current_info, current_comparison, current_runs = run_case(
            initial_program,
            campaign_package_dir=initial_package_dir,
            campaign_package_slot=initial_package_slot,
        )
    except SystemExit as exc:
        if not scml_overlay_enabled(cfg):
            raise
        current_info = inspect_program(initial_program)
        current_info, current_comparison, current_runs, current_preflight = require_recorded_source_evidence(
            current_info,
            source_entry,
            reason=f"fresh replay failed before reduction: {exc}",
        )
        reduction_blocked = True
        confirmed = False
    if scml_overlay_enabled(cfg):
        if current_preflight is None:
            try:
                current_preflight = scml_preflight_for_program(initial_program, require_zero_exit=False)
            except SystemExit as exc:
                current_info, current_comparison, current_runs, current_preflight = require_recorded_source_evidence(
                    current_info,
                    source_entry,
                    reason=f"fresh replay preflight failed: {exc}",
                )
                reduction_blocked = True
                confirmed = False
        if not scml_reduction_invariants_hold(
            current_comparison,
            current_runs["reference_canonical"],
            current_preflight["status"],
        ):
            current_info, current_comparison, current_runs, current_preflight = require_recorded_source_evidence(
                current_info,
                source_entry,
                reason="fresh replay did not preserve the recorded divergence or SCML pass status",
            )
            reduction_blocked = True
            confirmed = False
    confirmation_runs: list[dict[str, object]] = []
    changed = True
    with tempfile.TemporaryDirectory(dir=temp_dir()) as tempdir:
        tempdir_path = Path(tempdir)
        while changed and not reduction_blocked:
            changed = False
            call_count = current_info["call_count"]
            for drop_index in range(call_count - 1, -1, -1):
                trial_path = tempdir_path / f"drop-{drop_index}.syz"
                write_text(trial_path, mutate_drop_call(initial_program, drop_index))
                try:
                    trial_info, trial_comparison, trial_runs = run_case(trial_path)
                except SystemExit:
                    continue
                if trial_comparison["equivalent"]:
                    continue
                trial_preflight: dict[str, object] | None = None
                if scml_overlay_enabled(cfg):
                    try:
                        trial_preflight = scml_preflight_for_program(trial_path, require_zero_exit=True)
                    except SystemExit:
                        continue
                    if not scml_reduction_invariants_hold(
                        trial_comparison,
                        trial_runs["reference_canonical"],
                        trial_preflight["status"],
                    ):
                        continue
                # Confirmation: rerun the accepted step 3 times to verify stability
                all_diverged, step_confirmation_runs = _confirm_divergence(
                    trial_path,
                    rerun_count=3,
                    campaign_package_dir=initial_package_dir if scml_overlay_enabled(cfg) else None,
                    campaign_package_slot=initial_package_slot if scml_overlay_enabled(cfg) else None,
                )
                if not all_diverged:
                    continue
                current_text = read_text(trial_path)
                current_info = trial_info
                current_comparison = trial_comparison
                current_runs = trial_runs
                current_preflight = trial_preflight
                initial_program = trial_path
                changed = True
                confirmation_runs.extend(step_confirmation_runs)
                break
        final_path = report_path(f"{current_info['program_id']}-minimized.syz", cfg=cfg)
        write_text(final_path, current_text)
        return final_path, current_info, current_comparison, current_runs, current_preflight, confirmed, confirmation_runs


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    source_program, source_entry = seed_program(args.fixture, args.program_id)
    minimized_path, info, comparison, runs, minimized_preflight, confirmed, confirmation_runs = greedy_reduce(source_program, source_entry=source_entry)
    original_text = read_text(source_program)
    minimized_text = read_text(minimized_path)
    divergence_event_index = comparison["first_divergence_index"]
    divergence_syscall_index = map_event_index_to_program_call(runs["reference_canonical"], divergence_event_index)
    if scml_overlay_enabled(cfg) and divergence_syscall_index is None:
        raise SystemExit("asterinas_scml minimized report requires a non-null first_divergence_syscall_index")
    report = {
        "program_id": info["program_id"],
        "first_divergence_event_index": divergence_event_index,
        "first_divergence_syscall_index": divergence_syscall_index,
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
        "run_command": (
            f"python3 tools/reduce_case.py --workflow {cfg['workflow']} --program-id {source_entry['program_id']}"
            if scml_overlay_enabled(cfg) and source_entry
            else f"python3 tools/reduce_case.py --workflow {cfg['workflow']} --fixture {args.fixture}"
        ),
        "scml_preflight_status": minimized_preflight["status"] if minimized_preflight else (source_entry.get("scml_preflight_status", "unknown") if source_entry else "unknown"),
        "scml_trace_log_path": minimized_preflight["trace_log_path"] if minimized_preflight else (source_entry.get("scml_trace_log_path", "") if source_entry else ""),
        "scml_sctrace_output_path": minimized_preflight["sctrace_output_path"] if minimized_preflight else (source_entry.get("scml_sctrace_output_path", "") if source_entry else ""),
        "reducer_replay_mode": minimized_preflight["reducer_replay_mode"] if minimized_preflight else "not_applicable",
        "reducer_replay_recovery_reason": minimized_preflight["reducer_replay_recovery_reason"] if minimized_preflight else "",
        "confirmed": confirmed,
        "confirmation_runs": confirmation_runs,
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
                f"- reducer_replay_mode: {report['reducer_replay_mode']}",
                f"- reducer_replay_recovery_reason: {report['reducer_replay_recovery_reason']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
