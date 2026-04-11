#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime


THRESHOLD_RULES = {
    "build_success_rate": ("gte", ["build_success_rate"]),
    "dual_execution_completion_rate": ("gte", ["dual_execution_completion_rate"]),
    "trace_success_rate": ("gte", ["trace_generation_success_rate"]),
    "canonical_success_rate": ("gte", ["canonicalization_success_rate"]),
    "baseline_invalid_rate": ("lte", ["baseline_invalid_rate"]),
    "total_min": ("gte", ["total"]),
    "eligible_program_count_min": ("gte", ["eligible_program_count"]),
    "import_success_rate": ("gte", ["import_success_rate"]),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--summary", required=True)
    return parser.parse_args()


def load_summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summary_metric(summary: dict[str, Any], path_candidates: list[str]) -> float | None:
    current: Any = summary
    for key in path_candidates:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    if isinstance(current, (int, float)):
        return float(current)
    return None


def minimized_report_exists(summary_path: Path) -> bool:
    return (summary_path.parent / "minimized-report.json").exists()


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    thresholds = cfg.get("thresholds", {}).get(args.campaign)
    if not isinstance(thresholds, dict):
        raise SystemExit(f"missing thresholds for workflow={args.workflow} campaign={args.campaign}")
    summary_path = Path(args.summary)
    summary = load_summary(summary_path)

    failures: list[str] = []
    for key, expected in thresholds.items():
        if key not in THRESHOLD_RULES:
            continue
        rule, metric_path = THRESHOLD_RULES[key]
        actual = summary_metric(summary, metric_path)
        if actual is None:
            failures.append(f"summary missing metric for threshold {key}")
            continue
        expected_value = float(expected)
        if rule == "gte" and actual < expected_value:
            failures.append(f"{key}: actual={actual} < expected={expected_value}")
        if rule == "lte" and actual > expected_value:
            failures.append(f"{key}: actual={actual} > expected={expected_value}")
    if thresholds.get("require_minimized_report") and not minimized_report_exists(summary_path):
        failures.append("require_minimized_report: minimized-report.json is missing")

    if failures:
        raise SystemExit("threshold check failed:\n- " + "\n- ".join(failures))
    print(f"threshold check passed for workflow={args.workflow} campaign={args.campaign}")


if __name__ == "__main__":
    main()
