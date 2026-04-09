#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl, report_path, resolve_repo_path
from orchestrator.models import EligibleProgram
from targets.asterinas.scml import load_manifest_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    parser.add_argument("--source-eligible-file")
    parser.add_argument("--generated-source-eligible-file")
    return parser.parse_args()


def derive_rejection(
    meta: dict[str, Any],
    manifest_index: dict[str, dict[str, Any]],
    profile: dict[str, Any],
    cfg: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    taxonomy = cfg["derivation"]["rejection_taxonomy"]
    sequence = profile["generation"]["sequence_length"]
    call_count = int(meta.get("call_count", 0))
    if call_count < int(sequence["min"]):
        reasons.append(taxonomy["sequence_too_short"])
    if call_count > int(sequence["max"]):
        reasons.append(taxonomy["sequence_too_long"])

    for full_name in meta["full_syscall_list"]:
        base_name = full_name.split("$", 1)[0]
        if full_name != base_name:
            reasons.append(taxonomy["specialized_variant"])
            continue
        entry = manifest_index.get(base_name)
        if entry is None:
            reasons.append(taxonomy["syscall_not_in_manifest"])
            continue
        if not entry.get("generation_enabled", True):
            if entry.get("defer_reason"):
                reasons.append(taxonomy["deferred_category"])
            else:
                reasons.append(taxonomy["manifest_disabled"])

    return list(dict.fromkeys(reasons))


def merge_source_rows(*row_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rows in row_groups:
        for row in rows:
            program_id = str(row["program_id"])
            existing = merged.get(program_id)
            if existing is None:
                merged[program_id] = dict(row)
                continue
            combined = dict(existing)
            combined.update(row)
            if existing.get("source_modes") or row.get("source_modes"):
                combined["source_modes"] = sorted(
                    set(existing.get("source_modes", [])) | set(row.get("source_modes", []))
                )
            if existing.get("covered_target_syscalls") or row.get("covered_target_syscalls"):
                combined["covered_target_syscalls"] = sorted(
                    set(existing.get("covered_target_syscalls", []))
                    | set(row.get("covered_target_syscalls", []))
                )
            merged[program_id] = combined
    return sorted(merged.values(), key=lambda row: str(row["program_id"]))


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    manifest = load_json(cfg["compat_manifest_path"])
    profile = load_json(cfg["generation_profile_path"])
    manifest_index = load_manifest_index(manifest, profile)
    source_eligible_file = args.source_eligible_file or cfg["derivation"]["source_eligible_file"]
    generated_source_eligible_file = (
        args.generated_source_eligible_file
        or cfg["derivation"].get("generated_source_eligible_file")
    )
    base_rows = load_jsonl(source_eligible_file)
    generated_rows = []
    if generated_source_eligible_file:
        generated_path = resolve_repo_path(generated_source_eligible_file)
        if generated_path.exists():
            generated_rows = load_jsonl(generated_path)
    source_rows = merge_source_rows(base_rows, generated_rows)
    static_eligible_file = cfg["paths"].get("static_eligible_file", cfg["paths"]["eligible_file"])

    eligible_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    rejection_counts = Counter()

    for source_row in source_rows:
        meta = load_json(source_row["meta_path"])
        reasons = derive_rejection(meta, manifest_index, profile, cfg)
        if reasons:
            for reason in reasons:
                rejection_counts[reason] += 1
            rejected_rows.append(
                {
                    "program_id": source_row["program_id"],
                    "source_workflow": source_row.get("workflow", "baseline"),
                    "meta_path": source_row["meta_path"],
                    "normalized_path": source_row["normalized_path"],
                    "reasons": reasons,
                    "full_syscall_list": meta["full_syscall_list"],
                }
            )
            continue

        row = EligibleProgram(
            program_id=source_row["program_id"],
            workflow=cfg["workflow"],
            reason=list(cfg["derivation"]["accept_reasons"]),
            normalized_path=source_row["normalized_path"],
            meta_path=source_row["meta_path"],
            source_workflow=source_row.get("workflow", "baseline"),
            source_program_id=source_row["program_id"],
        )
        eligible_rows.append(row.to_dict())

    eligible_rows.sort(key=lambda row: row["program_id"])
    rejected_rows.sort(key=lambda row: row["program_id"])

    dump_jsonl(static_eligible_file, eligible_rows)
    derivation_rejections_path = report_path("derivation-rejections.jsonl", cfg=cfg)
    dump_jsonl(derivation_rejections_path, rejected_rows)
    dump_json(
        report_path("derivation-summary.json", cfg=cfg),
        {
            "workflow": cfg["workflow"],
            "source_eligible_file": source_eligible_file,
            "generated_source_eligible_file": generated_source_eligible_file,
            "base_source_total": len(base_rows),
            "generated_source_total": len(generated_rows),
            "static_eligible_file": static_eligible_file,
            "rejections_file": str(derivation_rejections_path),
            "source_total": len(source_rows),
            "eligible": len(eligible_rows),
            "rejected": len(rejected_rows),
            "rejection_counts": dict(rejection_counts),
        },
    )


if __name__ == "__main__":
    main()
