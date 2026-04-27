#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.generate_report import generate_report


class ReportConfirmedBugTests(unittest.TestCase):
    def test_generate_report_classifies_confirmed_and_unconfirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            reports_dir.mkdir()
            (reports_dir / "summary.json").write_text(json.dumps({"total": 2}))
            (reports_dir / "campaign-results.jsonl").write_text("")

            # Create fake minimized reports
            min_dir = reports_dir / "some" / "sub"
            min_dir.mkdir(parents=True)
            (min_dir / "minimized-report.json").write_text(json.dumps({
                "program_id": "confirmed-bug-1",
                "confirmed": True,
                "first_divergence_event_index": 3,
                "original_length": 100,
                "minimized_length": 50,
            }))
            (reports_dir / "minimized-report.json").write_text(json.dumps({
                "program_id": "unconfirmed-1",
                "confirmed": False,
                "first_divergence_event_index": 5,
                "original_length": 200,
                "minimized_length": 80,
            }))

            cfg = {"workflow": "baseline", "paths": {"reports_dir": str(reports_dir)}}
            with patch("tools.generate_report.config", return_value=cfg), patch(
                "tools.generate_report.report_path", side_effect=lambda name, cfg=None: reports_dir / name
            ):
                result = generate_report(cfg)

            self.assertEqual(result["confirmed_bugs_count"], 1)
            self.assertEqual(result["unconfirmed_divergences_count"], 1)
            self.assertEqual(result["minimized_reports_count"], 2)

            confirmed = json.loads((reports_dir / "confirmed-bugs.json").read_text())
            self.assertEqual(confirmed["count"], 1)
            self.assertEqual(confirmed["reports"][0]["program_id"], "confirmed-bug-1")

            unconfirmed = json.loads((reports_dir / "unconfirmed-divergences.json").read_text())
            self.assertEqual(unconfirmed["count"], 1)
            self.assertEqual(unconfirmed["reports"][0]["program_id"], "unconfirmed-1")

            report_md = (reports_dir / "report.md").read_text()
            self.assertIn("Confirmed Bugs (1)", report_md)
            self.assertIn("Unconfirmed Divergences (1)", report_md)


if __name__ == "__main__":
    unittest.main()
