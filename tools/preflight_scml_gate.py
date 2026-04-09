#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, ensure_dir, env_with_temp, load_json, load_jsonl, report_path, resolve_repo_path
from orchestrator.models import EligibleProgram
from tools.derive_scml_allowed_sequences import load_manifest_index
from tools.prog2c_wrap import build_one


UNSUPPORTED_PREFIX = "Unsupported syscall: "
PARSE_ERROR_PREFIX = "Strace Parse Error: "
SYSCALL_NAME_RE = re.compile(r"^\s*(?:\d+\s+)?(?P<name>[A-Za-z0-9_]+)\(")
FIELD_REASON_HINTS = {
    "flags": "unsupported_flag_pattern",
    "flags_in_events": "unsupported_flag_pattern",
    "mount_flags": "unsupported_flag_pattern",
    "event_flags": "unsupported_flag_pattern",
    "control_flags": "unsupported_flag_pattern",
    "codes": "unsupported_flag_pattern",
    "masks": "unsupported_flag_pattern",
    "who_flags": "unsupported_flag_pattern",
    "op_flags": "unsupported_flag_pattern",
}
PATH_PATTERN_SYSCALLS = {
    "mount",
    "umount",
    "umount2",
    "open",
    "openat",
    "rename",
    "renameat",
    "renameat2",
    "mkdir",
    "mkdirat",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "unlink",
    "unlinkat",
    "newfstatat",
    "faccessat",
    "faccessat2",
    "readlinkat",
    "utimensat",
}
STRUCT_PATTERN_SYSCALLS = {
    "clone3",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    parser.add_argument("--source-eligible-file")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--program-id")
    return parser.parse_args()


def scml_files(scml_root: Path) -> list[Path]:
    return sorted(path for path in scml_root.rglob("*.scml") if path.is_file())


def sctrace_command(scml_paths: list[Path], input_path: Path) -> list[str]:
    installed = shutil.which("sctrace")
    if installed:
        return [installed, *(str(path) for path in scml_paths), "--quiet", "--input", str(input_path)]
    manifest_path = resolve_repo_path("third_party/asterinas/tools/sctrace/Cargo.toml")
    return [
        "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str(manifest_path),
        "--",
        *(str(path) for path in scml_paths),
        "--quiet",
        "--input",
        str(input_path),
    ]


def parse_sctrace_lines(stdout: str, stderr: str) -> list[str]:
    matched: list[str] = []
    for line in (stdout.splitlines() + stderr.splitlines()):
        stripped = line.strip()
        if stripped.startswith(UNSUPPORTED_PREFIX) or stripped.startswith(PARSE_ERROR_PREFIX):
            matched.append(stripped)
    return matched


def parse_syscall_name(strace_line: str) -> str | None:
    match = SYSCALL_NAME_RE.match(strace_line)
    if match:
        return match.group("name")
    return None


def classify_reason_from_entry(
    strace_line: str,
    entry: dict[str, Any],
    cfg: dict[str, Any],
) -> str:
    taxonomy = cfg["preflight"]["rejection_taxonomy"]
    for key, values in entry.items():
        if not key.startswith("unsupported_") or not isinstance(values, list):
            continue
        field_name = key[len("unsupported_") :]
        if any(value and value in strace_line for value in values):
            return taxonomy[FIELD_REASON_HINTS.get(field_name, "unsupported_flag_pattern")]
    syscall_name = entry["name"]
    if syscall_name in STRUCT_PATTERN_SYSCALLS:
        return taxonomy["unsupported_struct_pattern"]
    if syscall_name in PATH_PATTERN_SYSCALLS:
        return taxonomy["unsupported_path_pattern"]
    return taxonomy["unsupported_flag_pattern"]


def classify_sctrace_line(
    line: str,
    manifest_index: dict[str, dict[str, Any]],
    cfg: dict[str, Any],
    *,
    target_syscalls: set[str] | None = None,
) -> list[str]:
    taxonomy = cfg["preflight"]["rejection_taxonomy"]
    if line.startswith(PARSE_ERROR_PREFIX):
        return [taxonomy["scml_parser_gap"]]
    if not line.startswith(UNSUPPORTED_PREFIX):
        return [taxonomy["scml_parser_gap"]]
    strace_line = line[len(UNSUPPORTED_PREFIX) :].strip()
    syscall_name = parse_syscall_name(strace_line)
    if syscall_name is None:
        return [taxonomy["scml_parser_gap"]]
    if target_syscalls is not None and syscall_name not in target_syscalls:
        return []
    entry = manifest_index.get(syscall_name)
    if entry is None:
        return [taxonomy["syscall_not_in_manifest"]]
    if not entry.get("generation_enabled", True):
        return [taxonomy["deferred_category"]]
    return [classify_reason_from_entry(strace_line, entry, cfg)]


def selected_entries(args: argparse.Namespace, source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = source_rows
    if args.program_id:
        rows = [row for row in rows if row["program_id"] == args.program_id]
    if args.limit is not None:
        rows = rows[: args.limit]
    return rows


def run_preflight(
    entry: dict[str, Any],
    *,
    cfg: dict[str, Any],
    manifest_index: dict[str, dict[str, Any]],
    scml_paths: list[Path],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    taxonomy = cfg["preflight"]["rejection_taxonomy"]
    build_result = build_one(entry)
    artifact_root = ensure_dir(Path(cfg["preflight"]["artifact_dir"]) / entry["program_id"])
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
    preflight_run = subprocess.run(
        ["strace", "-yy", "-f", "-o", str(strace_log_path), str(binary_path)],
        cwd=artifact_root,
        text=True,
        capture_output=True,
        check=False,
        env=env_with_temp(cfg=cfg),
    )
    strace_stdout_path.write_text(preflight_run.stdout, encoding="utf-8")
    strace_stderr_path.write_text(preflight_run.stderr, encoding="utf-8")

    sctrace_run = subprocess.run(
        sctrace_command(scml_paths, strace_log_path),
        cwd=resolve_repo_path("."),
        text=True,
        capture_output=True,
        check=False,
        env=env_with_temp(cfg=cfg),
    )
    sctrace_stdout_path.write_text(sctrace_run.stdout, encoding="utf-8")
    sctrace_stderr_path.write_text(sctrace_run.stderr, encoding="utf-8")
    meta = load_json(entry["meta_path"])
    target_syscalls = {full_name.split("$", 1)[0] for full_name in meta["full_syscall_list"]}
    output_lines = parse_sctrace_lines(sctrace_run.stdout, sctrace_run.stderr)
    relevant_output_lines = [
        line
        for line in output_lines
        if not line.startswith(UNSUPPORTED_PREFIX)
        or (
            parse_syscall_name(line[len(UNSUPPORTED_PREFIX) :].strip()) in target_syscalls
        )
    ]
    sctrace_output_path.write_text(
        "\n".join(relevant_output_lines) + ("\n" if relevant_output_lines else ""),
        encoding="utf-8",
    )

    reasons: list[str] = []
    for line in relevant_output_lines:
        reasons.extend(
            classify_sctrace_line(
                line,
                manifest_index,
                cfg,
                target_syscalls=target_syscalls,
            )
        )
    if sctrace_run.returncode != 0:
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


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    manifest = load_json(cfg["compat_manifest_path"])
    profile = load_json(cfg["generation_profile_path"])
    manifest_index = load_manifest_index(manifest, profile)
    source_eligible_file = args.source_eligible_file or cfg["preflight"]["source_eligible_file"]
    source_rows = selected_entries(args, load_jsonl(source_eligible_file))
    scml_root = resolve_repo_path(cfg["preflight"]["scml_root"])
    scml_paths = scml_files(scml_root)

    accepted_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    for entry in source_rows:
        accepted, rejected = run_preflight(
            entry,
            cfg=cfg,
            manifest_index=manifest_index,
            scml_paths=scml_paths,
        )
        if accepted is not None:
            accepted_rows.append(accepted)
        if rejected is not None:
            rejected_rows.append(rejected)

    accepted_rows.sort(key=lambda row: row["program_id"])
    rejected_rows.sort(key=lambda row: row["program_id"])
    dump_jsonl(cfg["paths"]["eligible_file"], accepted_rows)
    dump_jsonl(report_path("scml-rejections.jsonl", cfg=cfg), rejected_rows)
    dump_json(
        report_path("preflight-summary.json", cfg=cfg),
        {
            "workflow": cfg["workflow"],
            "source_eligible_file": source_eligible_file,
            "source_total": len(source_rows),
            "eligible": len(accepted_rows),
            "rejected": len(rejected_rows),
        },
    )


if __name__ == "__main__":
    main()
