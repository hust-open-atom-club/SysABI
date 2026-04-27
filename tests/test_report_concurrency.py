#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.render_summary import build_rendered_summary


class ReportConcurrencyTests(unittest.TestCase):
    def test_concurrency_breakdown_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            reports_dir.mkdir()
            campaign_results = [
                {
                    "program_id": "p1",
                    "classification": "NO_DIFF",
                    "candidate_run": {"status": "ok", "trace_json_path": str(root / "p1-cand.json"), "runner_kind": "command"},
                    "reference_runs": [{"status": "ok", "trace_json_path": str(root / "p1-ref.json")}],
                },
                {
                    "program_id": "p2",
                    "classification": "BUG_LIKELY",
                    "candidate_run": {"status": "timeout", "trace_json_path": str(root / "p2-cand.json"), "runner_kind": "command"},
                    "reference_runs": [{"status": "ok", "trace_json_path": str(root / "p2-ref.json")}],
                },
                {
                    "program_id": "p3",
                    "classification": "BASELINE_INVALID",
                    "candidate_run": {"status": "infra_error", "trace_json_path": str(root / "p3-cand.json"), "runner_kind": "command"},
                    "reference_runs": [{"status": "ok", "trace_json_path": str(root / "p3-ref.json")}],
                },
            ]
            (root / "p1-cand.json").write_text("{}")
            (root / "p1-ref.json").write_text("{}")
            (root / "p2-cand.json").write_text("{}")
            (root / "p2-ref.json").write_text("{}")
            (root / "p3-cand.json").write_text("{}")
            (root / "p3-ref.json").write_text("{}")
            (reports_dir / "campaign-results.jsonl").write_text(
                "\n".join(json.dumps(r) for r in campaign_results) + "\n"
            )
            (reports_dir / "summary.json").write_text(json.dumps({"campaign": "smoke", "jobs": 4, "max_concurrent_vms": 4}))

            cfg = {
                "workflow": "baseline",
                "paths": {"reports_dir": str(reports_dir), "eligible_file": str(root / "eligible.jsonl")},
                "classification": {
                    "baseline_invalid": "BASELINE_INVALID",
                    "bug_likely": "BUG_LIKELY",
                    "no_diff": "NO_DIFF",
                    "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                },
                "thresholds": {"smoke": {"build_success_rate": 0.0, "dual_execution_completion_rate": 0.0, "trace_success_rate": 0.0, "canonical_success_rate": 0.0, "baseline_invalid_rate": 1.0, "total_min": 0, "eligible_program_count_min": 0}},
            }
            (root / "eligible.jsonl").write_text("\n".join(json.dumps({"program_id": r["program_id"]}) for r in campaign_results) + "\n")

            with patch("tools.render_summary.config", return_value=cfg), patch(
                "tools.render_summary.report_path", side_effect=lambda name, cfg=None: reports_dir / name
            ):
                summary = build_rendered_summary(cfg)

            cb = summary.get("concurrency_breakdown")
            self.assertIsNotNone(cb)
            self.assertEqual(cb["total_cases"], 3)
            self.assertEqual(cb["completed_cases"], 1)
            self.assertEqual(cb["timeout_cases"], 1)
            self.assertEqual(cb["infra_error_cases"], 1)
            self.assertEqual(cb["jobs"], 4)
            self.assertEqual(cb["max_concurrent_vms"], 4)

            ieb = summary.get("infra_error_breakdown")
            self.assertIsNotNone(ieb)
            self.assertEqual(ieb["timeout"], 1)
            self.assertEqual(ieb["infra_error"], 1)


if __name__ == "__main__":
    unittest.main()
