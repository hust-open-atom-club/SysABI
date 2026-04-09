#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.capability import load_manifest_index
from orchestrator.common import config, configure_runtime, dump_json, dump_jsonl, load_json, report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="asterinas_scml")
    parser.add_argument("--output")
    return parser.parse_args()


def target_row_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "syscall_name": entry["name"],
        "category": entry["category"],
        "support_tier": entry["support_tier"],
        "generation_enabled": entry["generation_enabled"],
        "defer_reason": entry.get("defer_reason"),
        "generator_class": entry.get("generator_class", "unavailable"),
        "generator_gap_reason": entry.get("generator_gap_reason", "missing_description"),
        "syzkaller_base_available": bool(entry.get("syzkaller_base_available")),
        "syzkaller_variant_available": bool(entry.get("syzkaller_variant_available")),
        "readme_path": entry.get("readme_path"),
        "source_scml_files": list(entry.get("source_scml_files", [])),
    }


def build_generation_targets(manifest_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        target_row_from_entry(entry)
        for _, entry in sorted(manifest_index.items())
        if entry.get("generation_enabled", True)
    ]
    rows.sort(key=lambda row: row["syscall_name"])
    return rows


def build_target_summary(rows: list[dict[str, Any]], *, workflow: str) -> dict[str, Any]:
    class_counts = Counter(row["generator_class"] for row in rows)
    support_tier_counts = Counter(row["support_tier"] for row in rows)
    reachable_total = sum(1 for row in rows if row["generator_class"] in {"base_only", "variant_only"})
    return {
        "workflow": workflow,
        "target_total": len(rows),
        "generator_class_counts": dict(class_counts),
        "support_tier_counts": dict(support_tier_counts),
        "reachable_total": reachable_total,
        "unreachable_total": len(rows) - reachable_total,
    }


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    manifest = load_json(cfg["compat_manifest_path"])
    profile = load_json(cfg["generation_profile_path"])
    manifest_index = load_manifest_index(manifest, profile)
    rows = build_generation_targets(manifest_index)
    output = args.output or cfg["paths"]["targets_file"]
    dump_jsonl(output, rows)
    dump_json(
        report_path("generation-targets-summary.json", cfg=cfg),
        build_target_summary(rows, workflow=cfg["workflow"]),
    )


if __name__ == "__main__":
    main()
