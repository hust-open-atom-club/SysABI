#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl, report_path
from orchestrator.models import EligibleProgram


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas")
    parser.add_argument("--source-eligible-file")
    return parser.parse_args()


def derive_rejection(meta: dict[str, object], cfg: dict[str, object]) -> list[str]:
    allow = set(cfg["allowlist"]["syscalls"])
    taxonomy = cfg["derivation"]["unsupported_taxonomy"]
    memory_management = set(taxonomy["memory_management"])
    process_control = set(taxonomy["process_control"])
    reasons: list[str] = []
    for full_name in meta["full_syscall_list"]:
        base_name = full_name.split("$", 1)[0]
        if full_name != base_name and full_name not in allow:
            reasons.append("unsupported_variant")
            continue
        if base_name in allow:
            continue
        if base_name in memory_management:
            reasons.append("unsupported_memory_management")
        elif base_name in process_control:
            reasons.append("unsupported_process_control")
        else:
            reasons.append("unsupported_syscall")
    return list(dict.fromkeys(reasons))


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    source_eligible_file = args.source_eligible_file or cfg["derivation"]["source_eligible_file"]
    source_rows = load_jsonl(source_eligible_file)
    eligible_rows: list[dict[str, object]] = []
    rejected_rows: list[dict[str, object]] = []
    rejection_counts = Counter()

    for source_row in source_rows:
        meta = load_json(source_row["meta_path"])
        reasons = derive_rejection(meta, cfg)
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

    dump_jsonl(cfg["paths"]["eligible_file"], eligible_rows)
    dump_jsonl(report_path("derivation-rejections.jsonl", cfg=cfg), rejected_rows)
    dump_json(
        report_path("derivation-summary.json", cfg=cfg),
        {
            "workflow": cfg["workflow"],
            "source_eligible_file": source_eligible_file,
            "source_total": len(source_rows),
            "eligible": len(eligible_rows),
            "rejected": len(rejected_rows),
            "rejection_counts": dict(rejection_counts),
        },
    )


if __name__ == "__main__":
    main()
