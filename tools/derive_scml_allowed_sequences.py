#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl, report_path
from orchestrator.models import EligibleProgram


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    parser.add_argument("--source-eligible-file")
    return parser.parse_args()


def apply_generation_profile(
    manifest: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    enabled_categories = set(profile["enabled_categories"])
    deferred_categories = dict(profile.get("deferred_categories", {}))
    deferred_syscalls = dict(profile.get("deferred_syscalls", {}))
    index: dict[str, dict[str, Any]] = {}
    for category_name, category in manifest["categories"].items():
        for syscall_name, entry in category["syscalls"].items():
            effective = {
                **entry,
                "category": category_name,
            }
            generation_enabled = bool(entry.get("generation_enabled", True))
            defer_reason = entry.get("defer_reason")
            if syscall_name in deferred_syscalls:
                generation_enabled = False
                defer_reason = deferred_syscalls[syscall_name]
            elif category_name not in enabled_categories:
                generation_enabled = False
                defer_reason = deferred_categories.get(category_name, "category_not_enabled")
            effective["generation_enabled"] = generation_enabled
            effective["defer_reason"] = defer_reason
            index[syscall_name] = effective
    return index


def load_manifest_index(
    manifest: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    if profile is not None:
        return apply_generation_profile(manifest, profile)
    index: dict[str, dict[str, Any]] = {}
    for category_name, category in manifest["categories"].items():
        for syscall_name, entry in category["syscalls"].items():
            index[syscall_name] = {
                **entry,
                "category": category_name,
            }
    return index


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


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    manifest = load_json(cfg["compat_manifest_path"])
    profile = load_json(cfg["generation_profile_path"])
    manifest_index = load_manifest_index(manifest, profile)
    source_eligible_file = args.source_eligible_file or cfg["derivation"]["source_eligible_file"]
    source_rows = load_jsonl(source_eligible_file)
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
    dump_jsonl(report_path("scml-rejections.jsonl", cfg=cfg), rejected_rows)
    dump_json(
        report_path("derivation-summary.json", cfg=cfg),
        {
            "workflow": cfg["workflow"],
            "source_eligible_file": source_eligible_file,
            "static_eligible_file": static_eligible_file,
            "source_total": len(source_rows),
            "eligible": len(eligible_rows),
            "rejected": len(rejected_rows),
            "rejection_counts": dict(rejection_counts),
        },
    )


if __name__ == "__main__":
    main()
