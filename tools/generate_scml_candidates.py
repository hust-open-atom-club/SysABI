#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import shutil
import subprocess
import tempfile
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import (
    config,
    configure_runtime,
    dump_json,
    dump_jsonl,
    env_with_temp,
    ensure_dir,
    load_json,
    load_jsonl,
    read_text,
    report_path,
    resolve_repo_path,
    temp_dir,
    write_text,
)
from orchestrator.capability import load_manifest_index
from orchestrator.models import ProgramMeta
from orchestrator.syzkaller import inspect_program, project_bin
from tools.derive_scml_allowed_sequences import derive_rejection


DEFAULT_BATCH_SIZE = 1
DEFAULT_PER_TARGET_BUDGET = 8
DEFAULT_EXISTING_CORPUS_LIMIT = 4
GENERATED_STAGING_PREFIX = "scml-syzgen-"


class GeneratorExecutionError(RuntimeError):
    def __init__(self, syscall_name: str, returncode: int | None, stdout: str, stderr: str) -> None:
        self.syscall_name = syscall_name
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = stderr.strip() or stdout.strip() or "unknown generator failure"
        super().__init__(f"syzabi_generate failed for {syscall_name}: returncode={returncode}: {detail}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    parser.add_argument("--targets-file")
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--per-target-budget", type=int)
    parser.add_argument("--existing-corpus-source-file")
    return parser.parse_args()


def generation_settings(
    cfg: dict[str, Any],
    profile: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    generation = dict(profile.get("generation", {}))
    sequence = dict(generation.get("sequence_length", {}))
    min_len = int(sequence.get("min", 1))
    max_len = int(sequence.get("max", 4))
    preferred_length = min(max(min_len, 4), max_len)
    return {
        "source_modes": list(generation.get("source_modes", [])),
        "jobs": max(1, int(args.jobs or generation.get("jobs") or cfg.get("parallel", {}).get("jobs", 1))),
        "batch_size": max(1, int(args.batch_size or generation.get("batch_size", DEFAULT_BATCH_SIZE))),
        "per_target_budget": max(
            1,
            int(args.per_target_budget or generation.get("per_target_budget", DEFAULT_PER_TARGET_BUDGET)),
        ),
        "existing_corpus_limit": max(
            1,
            int(generation.get("existing_corpus_limit", DEFAULT_EXISTING_CORPUS_LIMIT)),
        ),
        "preferred_length": preferred_length,
        "existing_corpus_source_file": args.existing_corpus_source_file
        or generation.get("existing_corpus_source_file")
        or cfg.get("derivation", {}).get("legacy_source_eligible_file"),
        "template_dir": generation.get("template_dir"),
        "allow_variant_templates": set(generation.get("allow_variant_templates", [])),
    }


def target_batches(rows: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + batch_size] for index in range(0, len(rows), batch_size)]


def target_syscall_set(target_rows: list[dict[str, Any]]) -> set[str]:
    return {row["syscall_name"] for row in target_rows}


def candidate_target_coverage(meta: dict[str, Any], targets: set[str]) -> list[str]:
    return sorted(set(meta.get("syscall_list", [])) & targets)


def candidate_row_from_existing(
    row: dict[str, Any],
    *,
    workflow: str,
    covered_targets: list[str],
    source_mode: str,
) -> dict[str, Any]:
    return {
        "program_id": row["program_id"],
        "workflow": workflow,
        "source_mode": source_mode,
        "source_modes": [source_mode],
        "source_workflow": row.get("workflow", ""),
        "source_program_id": row.get("program_id", ""),
        "normalized_path": row["normalized_path"],
        "meta_path": row["meta_path"],
        "covered_target_syscalls": covered_targets,
    }


def merge_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        existing = merged.get(row["program_id"])
        if existing is None:
            merged[row["program_id"]] = {
                **row,
                "source_modes": sorted(set(row.get("source_modes", []))),
                "covered_target_syscalls": sorted(set(row.get("covered_target_syscalls", []))),
            }
            continue
        existing["source_modes"] = sorted(
            set(existing.get("source_modes", [])) | set(row.get("source_modes", []))
        )
        existing["covered_target_syscalls"] = sorted(
            set(existing.get("covered_target_syscalls", [])) | set(row.get("covered_target_syscalls", []))
        )
    return sorted(merged.values(), key=lambda row: row["program_id"])


def load_existing_corpus_index(
    source_file: str | None,
    *,
    workflow: str,
    target_rows: list[dict[str, Any]],
    limit_per_target: int,
    validate_meta_fn=None,
) -> dict[str, list[dict[str, Any]]]:
    if not source_file:
        return {}
    source_path = resolve_repo_path(source_file)
    if not source_path.exists():
        return {}
    targets = target_syscall_set(target_rows)
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_jsonl(source_path):
        meta = load_json(row["meta_path"])
        if validate_meta_fn is not None and not validate_meta_fn(meta):
            continue
        covered = candidate_target_coverage(meta, targets)
        if not covered:
            continue
        candidate = candidate_row_from_existing(
            row,
            workflow=workflow,
            covered_targets=covered,
            source_mode="existing_corpus",
        )
        for syscall_name in covered:
            if len(index[syscall_name]) >= limit_per_target:
                continue
            index[syscall_name].append(candidate)
    return dict(index)


def template_paths_for_target(template_root: Path, syscall_name: str) -> list[Path]:
    direct = template_root / f"{syscall_name}.syz"
    files: list[Path] = []
    if direct.exists():
        files.append(direct)
    nested_root = template_root / syscall_name
    if nested_root.exists():
        files.extend(sorted(path for path in nested_root.rglob("*.syz") if path.is_file()))
    wildcard = sorted(path for path in template_root.glob(f"{syscall_name}_*.syz") if path.is_file())
    files.extend(wildcard)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(path)
    return deduped


def persist_inspected_program(
    program_path: Path,
    *,
    cfg: dict[str, Any],
    source_mode: str,
    inspect_program_fn=inspect_program,
) -> tuple[dict[str, Any], dict[str, Any]]:
    info = inspect_program_fn(program_path)
    program_id = info["program_id"]
    raw_path = resolve_repo_path(cfg["paths"]["generated_raw_dir"]) / f"{program_id}.syz"
    normalized_path = resolve_repo_path(cfg["paths"]["generated_normalized_dir"]) / f"{program_id}.syz"
    meta_path = resolve_repo_path(cfg["paths"]["generated_meta_dir"]) / f"{program_id}.json"
    if not raw_path.exists():
        write_text(raw_path, read_text(program_path))
    if not normalized_path.exists():
        write_text(normalized_path, info["normalized_syz"])
    if not meta_path.exists():
        meta = ProgramMeta(
            program_id=program_id,
            source=source_mode,
            target_os=info["target_os"],
            arch=info["arch"],
            syscall_list=info["syscall_list"],
            full_syscall_list=info["full_syscall_list"],
            resource_classes=info["resource_classes"],
            uses_pseudo_syscalls=info["uses_pseudo_syscalls"],
            uses_threading_sensitive_features=info["uses_threading_sensitive_features"],
            original_path=str(program_path),
            raw_path=str(raw_path),
            normalized_path=str(normalized_path),
            call_count=int(info["call_count"]),
        )
        meta_payload = meta.to_dict()
        dump_json(meta_path, meta_payload)
    else:
        meta_payload = load_json(meta_path)
    row = {
        "program_id": program_id,
        "workflow": cfg["workflow"],
        "source_mode": source_mode,
        "source_modes": [source_mode],
        "source_workflow": cfg["workflow"],
        "source_program_id": program_id,
        "normalized_path": str(normalized_path),
        "meta_path": str(meta_path),
        "covered_target_syscalls": [],
    }
    return row, meta_payload


def load_template_index(
    template_dir: str | None,
    *,
    settings: dict[str, Any],
    cfg: dict[str, Any],
    target_rows: list[dict[str, Any]],
    inspect_program_fn=inspect_program,
) -> dict[str, list[dict[str, Any]]]:
    if not template_dir:
        return {}
    root = resolve_repo_path(template_dir)
    if not root.exists():
        return {}
    targets = target_syscall_set(target_rows)
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for target in target_rows:
        syscall_name = target["syscall_name"]
        for path in template_paths_for_target(root, syscall_name):
            row, meta = persist_inspected_program(
                path,
                cfg=cfg,
                source_mode="seed_templates",
                inspect_program_fn=inspect_program_fn,
            )
            if any(
                full_name.startswith(f"{syscall_name}$")
                for full_name in meta.get("full_syscall_list", [])
            ) and syscall_name not in settings["allow_variant_templates"]:
                continue
            covered = candidate_target_coverage(meta, targets)
            if syscall_name not in covered:
                continue
            row["covered_target_syscalls"] = covered
            index[syscall_name].append(row)
    return dict(index)


def run_syzabi_generate(
    *,
    syscall_name: str,
    cfg: dict[str, Any],
    budget: int,
    preferred_length: int,
    seed: int,
) -> list[Path]:
    binary = project_bin("syzabi_generate")
    if not binary.exists():
        raise FileNotFoundError(f"missing build/bin/syzabi_generate, run `make bootstrap` first")
    output_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{GENERATED_STAGING_PREFIX}{syscall_name}-",
            dir=str(temp_dir(cfg)),
        )
    )
    cmd = [
        str(binary),
        "-os",
        cfg["target_os"],
        "-arch",
        cfg["arch"],
        "-output-dir",
        str(output_dir),
        "-count",
        str(budget),
        "-seed",
        str(seed),
        "-length",
        str(preferred_length),
        "-allow",
        syscall_name,
    ]
    result = subprocess.run(cmd, check=False, text=True, capture_output=True, env=env_with_temp(cfg=cfg))
    generated_paths = sorted(path for path in output_dir.glob("*.syz") if path.is_file())
    # syzabi_generate exits 2 when it cannot reach the requested unique-count budget.
    # Keep any programs it already materialized instead of discarding partial coverage.
    if result.returncode == 0 or (result.returncode == 2 and generated_paths):
        return generated_paths
    shutil.rmtree(output_dir, ignore_errors=True)
    raise GeneratorExecutionError(
        syscall_name=syscall_name,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def cleanup_generated_staging_dirs(paths: list[Path], *, cfg: dict[str, Any]) -> None:
    if not paths:
        return
    temp_root = temp_dir(cfg).resolve()
    parents = {path.parent.resolve() for path in paths}
    for parent in parents:
        try:
            parent.relative_to(temp_root)
        except ValueError:
            continue
        if not parent.name.startswith(GENERATED_STAGING_PREFIX):
            continue
        shutil.rmtree(parent, ignore_errors=True)


def generate_rows_for_target(
    target: dict[str, Any],
    *,
    cfg: dict[str, Any],
    settings: dict[str, Any],
    all_targets: set[str],
    existing_corpus_index: dict[str, list[dict[str, Any]]],
    template_index: dict[str, list[dict[str, Any]]],
    inspect_program_fn=inspect_program,
    generator_fn=run_syzabi_generate,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
    syscall_name = target["syscall_name"]
    rows: list[dict[str, Any]] = []
    source_modes_attempted: list[str] = []
    generator_error: GeneratorExecutionError | None = None
    inspect_error: str | None = None

    if "existing_corpus" in settings["source_modes"]:
        source_modes_attempted.append("existing_corpus")
        rows.extend(existing_corpus_index.get(syscall_name, []))

    if "seed_templates" in settings["source_modes"]:
        source_modes_attempted.append("seed_templates")
        rows.extend(template_index.get(syscall_name, []))

    if "syz_generate" in settings["source_modes"] and target["generator_class"] == "base_only":
        source_modes_attempted.append("syz_generate")
        try:
            generated_paths = generator_fn(
                syscall_name=syscall_name,
                cfg=cfg,
                budget=settings["per_target_budget"],
                preferred_length=settings["preferred_length"],
                seed=int(hashlib.sha256(f"{cfg['workflow']}:{syscall_name}".encode("utf-8")).hexdigest()[:8], 16),
            )
        except GeneratorExecutionError as exc:
            generator_error = exc
        else:
            try:
                for path in generated_paths:
                    try:
                        row, meta = persist_inspected_program(
                            path,
                            cfg=cfg,
                            source_mode="syz_generate",
                            inspect_program_fn=inspect_program_fn,
                        )
                    except (subprocess.CalledProcessError, FileNotFoundError, RuntimeError) as exc:
                        inspect_error = str(exc)
                        continue
                    covered = candidate_target_coverage(meta, all_targets)
                    if syscall_name not in covered:
                        continue
                    row["covered_target_syscalls"] = covered
                    rows.append(row)
            finally:
                cleanup_generated_staging_dirs(generated_paths, cfg=cfg)

    unique_rows = merge_candidate_rows(rows)
    coverage_row = {
        "syscall_name": syscall_name,
        "generator_class": target["generator_class"],
        "generator_gap_reason": target["generator_gap_reason"],
        "source_modes_attempted": source_modes_attempted,
        "candidate_count": len(unique_rows),
    }
    if generator_error is not None:
        coverage_row["generator_error"] = str(generator_error)
    if inspect_error is not None:
        coverage_row["inspect_error"] = inspect_error
    if unique_rows:
        return unique_rows, coverage_row, None

    if generator_error is not None:
        gap_reason = "generator_failed"
    elif inspect_error is not None:
        gap_reason = "inspect_failed"
    elif target["generator_class"] in {"helper_only", "unavailable"}:
        gap_reason = target["generator_gap_reason"]
    else:
        gap_reason = "generation_exhausted"
    gap_row = {
        "syscall_name": syscall_name,
        "category": target["category"],
        "support_tier": target["support_tier"],
        "generator_class": target["generator_class"],
        "generator_gap_reason": gap_reason,
        "source_modes_attempted": source_modes_attempted,
    }
    if generator_error is not None:
        gap_row["generator_error"] = str(generator_error)
    if inspect_error is not None:
        gap_row["inspect_error"] = inspect_error
    return [], coverage_row, gap_row


def build_generation_summary(
    *,
    cfg: dict[str, Any],
    settings: dict[str, Any],
    target_rows: list[dict[str, Any]],
    generated_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
    gap_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    target_counts = Counter(row["generator_class"] for row in target_rows)
    source_mode_counts = Counter()
    for row in generated_rows:
        for source_mode in row.get("source_modes", []):
            source_mode_counts[source_mode] += 1
    gap_reason_counts = Counter(row["generator_gap_reason"] for row in gap_rows)
    covered_targets = sum(1 for row in coverage_rows if row["candidate_count"] > 0)
    summary = {
        "workflow": cfg["workflow"],
        "profile_enabled_total": len(target_rows),
        "targets_with_candidates": covered_targets,
        "targets_without_candidates": len(target_rows) - covered_targets,
        "unique_candidate_count": len(generated_rows),
        "generator_failed_targets": gap_reason_counts.get("generator_failed", 0),
        "generator_class_counts": dict(target_counts),
        "source_mode_counts": dict(source_mode_counts),
        "gap_reason_counts": dict(gap_reason_counts),
        "generation_jobs": settings["jobs"],
        "generation_batch_size": settings["batch_size"],
        "per_target_budget": settings["per_target_budget"],
    }
    return summary


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    profile = load_json(cfg["generation_profile_path"])
    settings = generation_settings(cfg, profile, args)
    targets_file = args.targets_file or cfg["paths"]["targets_file"]
    target_rows = load_jsonl(targets_file)
    all_targets = target_syscall_set(target_rows)
    manifest = load_json(cfg["compat_manifest_path"])
    manifest_index = load_manifest_index(manifest, profile)
    ensure_dir(cfg["paths"]["generated_raw_dir"])
    ensure_dir(cfg["paths"]["generated_normalized_dir"])
    ensure_dir(cfg["paths"]["generated_meta_dir"])

    def validate_existing_meta(meta: dict[str, Any]) -> bool:
        return not derive_rejection(meta, manifest_index, profile, cfg)

    existing_corpus_index = load_existing_corpus_index(
        settings["existing_corpus_source_file"],
        workflow=cfg["workflow"],
        target_rows=target_rows,
        limit_per_target=settings["existing_corpus_limit"],
        validate_meta_fn=validate_existing_meta,
    )
    template_index = load_template_index(
        settings["template_dir"],
        settings=settings,
        cfg=cfg,
        target_rows=target_rows,
    )

    generated_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []

    def run_batch(batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        batch_rows: list[dict[str, Any]] = []
        batch_coverage: list[dict[str, Any]] = []
        batch_gaps: list[dict[str, Any]] = []
        for target in batch:
            rows, coverage, gap = generate_rows_for_target(
                target,
                cfg=cfg,
                settings=settings,
                all_targets=all_targets,
                existing_corpus_index=existing_corpus_index,
                template_index=template_index,
            )
            batch_rows.extend(rows)
            batch_coverage.append(coverage)
            if gap is not None:
                batch_gaps.append(gap)
        return batch_rows, batch_coverage, batch_gaps

    batches = target_batches(target_rows, settings["batch_size"])
    if settings["jobs"] <= 1 or len(batches) <= 1:
        for batch in batches:
            batch_rows, batch_coverage, batch_gaps = run_batch(batch)
            generated_rows.extend(batch_rows)
            coverage_rows.extend(batch_coverage)
            gap_rows.extend(batch_gaps)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=settings["jobs"]) as executor:
            futures = [executor.submit(run_batch, batch) for batch in batches]
            for future in concurrent.futures.as_completed(futures):
                batch_rows, batch_coverage, batch_gaps = future.result()
                generated_rows.extend(batch_rows)
                coverage_rows.extend(batch_coverage)
                gap_rows.extend(batch_gaps)

    generated_rows = merge_candidate_rows(generated_rows)
    coverage_rows.sort(key=lambda row: row["syscall_name"])
    gap_rows.sort(key=lambda row: row["syscall_name"])
    dump_jsonl(cfg["paths"]["generated_file"], generated_rows)
    dump_jsonl(report_path("generation-gaps.jsonl", cfg=cfg), gap_rows)
    dump_json(
        report_path("coverage-summary.json", cfg=cfg),
        build_generation_summary(
            cfg=cfg,
            settings=settings,
            target_rows=target_rows,
            generated_rows=generated_rows,
            coverage_rows=coverage_rows,
            gap_rows=gap_rows,
        ),
    )
    dump_json(
        report_path("generation-summary.json", cfg=cfg),
        {
            **build_generation_summary(
                cfg=cfg,
                settings=settings,
                target_rows=target_rows,
                generated_rows=generated_rows,
                coverage_rows=coverage_rows,
                gap_rows=gap_rows,
            ),
            "target_coverage": coverage_rows,
        },
    )
    generator_failures = [row for row in gap_rows if row["generator_gap_reason"] == "generator_failed"]
    if generator_failures:
        sys.stderr.write(f"warning: syzabi_generate failed for {len(generator_failures)} target(s)\n")


if __name__ == "__main__":
    main()
