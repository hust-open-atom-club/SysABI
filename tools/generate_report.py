#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, dump_json, load_json, load_jsonl, report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="baseline")
    parser.add_argument("--campaign", default=None)
    return parser.parse_args()


def collect_minimized_reports(cfg: dict[str, object]) -> list[dict[str, object]]:
    """Load all minimized-report.json files found in the reports directory."""
    reports_root = report_path("", cfg=cfg).parent
    reports: list[dict[str, object]] = []
    for path in reports_root.rglob("minimized-report.json"):
        try:
            reports.append(load_json(path))
        except Exception:
            continue
    return reports


def generate_report(cfg: dict[str, object], campaign: str | None = None) -> dict[str, object]:
    summary_path = report_path("summary.json", cfg=cfg)
    summary = load_json(summary_path) if summary_path.exists() else {}

    campaign_results = load_jsonl(report_path("campaign-results.jsonl", cfg=cfg))
    minimized_reports = collect_minimized_reports(cfg)

    confirmed = [r for r in minimized_reports if r.get("confirmed", False)]
    unconfirmed = [r for r in minimized_reports if not r.get("confirmed", False)]

    # Write aggregate JSONs
    confirmed_path = report_path("confirmed-bugs.json", cfg=cfg)
    unconfirmed_path = report_path("unconfirmed-divergences.json", cfg=cfg)
    dump_json(confirmed_path, {
        "count": len(confirmed),
        "reports": confirmed,
    })
    dump_json(unconfirmed_path, {
        "count": len(unconfirmed),
        "reports": unconfirmed,
    })

    # Build unified report.md
    lines = [
        f"# Campaign Report: {cfg.get('workflow', 'unknown')}",
        "",
        f"- campaign: {campaign or summary.get('campaign', 'unknown')}",
        f"- total cases: {summary.get('total', 0)}",
        f"- dual execution completion rate: {summary.get('dual_execution_completion_rate', 0.0):.3f}",
        f"- signoff pass: {summary.get('signoff_pass', False)}",
        "",
        "## Classification Counts",
    ]
    for key, value in sorted(summary.get("classification_counts", {}).items()):
        lines.append(f"- {key}: {value}")

    cb = summary.get("concurrency_breakdown")
    if cb:
        lines.extend(["", "## Concurrency Breakdown"])
        for key, value in sorted(cb.items()):
            lines.append(f"- {key}: {value}")

    ieb = summary.get("infra_error_breakdown")
    if ieb:
        lines.extend(["", "## Infra Error Breakdown"])
        for key, value in sorted(ieb.items()):
            lines.append(f"- {key}: {value}")

    lines.extend(["", f"## Confirmed Bugs ({len(confirmed)})"])
    for r in confirmed:
        lines.append(f"- {r.get('program_id', 'unknown')}: divergence at event {r.get('first_divergence_event_index', 'n/a')}")

    lines.extend(["", f"## Unconfirmed Divergences ({len(unconfirmed)})"])
    for r in unconfirmed:
        lines.append(f"- {r.get('program_id', 'unknown')}: divergence at event {r.get('first_divergence_event_index', 'n/a')}")

    lines.extend(["", "## Minimized Reports"])
    for r in minimized_reports:
        confirmed_str = "confirmed" if r.get("confirmed") else "unconfirmed"
        lines.append(
            f"- {r.get('program_id', 'unknown')}: {confirmed_str}, "
            f"original_len={r.get('original_length', 0)}, "
            f"minimized_len={r.get('minimized_length', 0)}"
        )

    md_path = report_path("report.md", cfg=cfg)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "summary": summary,
        "confirmed_bugs_count": len(confirmed),
        "unconfirmed_divergences_count": len(unconfirmed),
        "minimized_reports_count": len(minimized_reports),
        "report_md_path": str(md_path),
    }


def main() -> None:
    args = parse_args()
    configure_runtime(workflow=args.workflow)
    cfg = config()
    result = generate_report(cfg, campaign=args.campaign)
    print(f"Report generated: {result['report_md_path']}")
    print(f"  confirmed bugs: {result['confirmed_bugs_count']}")
    print(f"  unconfirmed divergences: {result['unconfirmed_divergences_count']}")


if __name__ == "__main__":
    main()
