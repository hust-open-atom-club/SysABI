from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class ReleaseGateTests(unittest.TestCase):
    def test_check_workflow_thresholds_accepts_passing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workflow.json"
            summary_path = root / "summary.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "thresholds",
                        "target": "linux",
                        "arch": "amd64",
                        "runner_profiles_path": "configs/targets/linux/runner_profiles.baseline.json",
                        "target_config_path": "configs/targets/linux/target.json",
                        "paths": {
                            "build_dir": str(root / "build"),
                            "artifacts_dir": str(root / "artifacts"),
                            "reports_dir": str(root / "reports"),
                            "eligible_file": str(root / "eligible.jsonl"),
                            "temp_dir": str(root / "tmp"),
                        },
                        "normalization": {"preview_bytes": 32},
                        "classification": {"no_diff": "NO_DIFF"},
                        "thresholds": {
                            "smoke": {
                                "build_success_rate": 0.5,
                                "dual_execution_completion_rate": 0.5,
                                "trace_success_rate": 0.5,
                                "canonical_success_rate": 0.5,
                                "baseline_invalid_rate": 0.5,
                                "total_min": 1
                            }
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            summary_path.write_text(
                json.dumps(
                    {
                        "build_success_rate": 1.0,
                        "dual_execution_completion_rate": 1.0,
                        "trace_generation_success_rate": 1.0,
                        "canonicalization_success_rate": 1.0,
                        "baseline_invalid_rate": 0.0,
                        "total": 10,
                    }
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["SYZABI_CONFIG_PATH"] = str(config_path)
            completed = subprocess.run(
                [
                    "python3",
                    "tools/check_workflow_thresholds.py",
                    "--workflow",
                    "thresholds",
                    "--campaign",
                    "smoke",
                    "--summary",
                    str(summary_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_check_workflow_thresholds_rejects_failing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workflow.json"
            summary_path = root / "summary.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "thresholds",
                        "target": "linux",
                        "arch": "amd64",
                        "runner_profiles_path": "configs/targets/linux/runner_profiles.baseline.json",
                        "target_config_path": "configs/targets/linux/target.json",
                        "paths": {
                            "build_dir": str(root / "build"),
                            "artifacts_dir": str(root / "artifacts"),
                            "reports_dir": str(root / "reports"),
                            "eligible_file": str(root / "eligible.jsonl"),
                            "temp_dir": str(root / "tmp"),
                        },
                        "normalization": {"preview_bytes": 32},
                        "classification": {"no_diff": "NO_DIFF"},
                        "thresholds": {
                            "smoke": {
                                "build_success_rate": 0.9
                            }
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            summary_path.write_text(json.dumps({"build_success_rate": 0.5}), encoding="utf-8")
            env = os.environ.copy()
            env["SYZABI_CONFIG_PATH"] = str(config_path)
            completed = subprocess.run(
                [
                    "python3",
                    "tools/check_workflow_thresholds.py",
                    "--workflow",
                    "thresholds",
                    "--campaign",
                    "smoke",
                    "--summary",
                    str(summary_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("build_success_rate", completed.stderr + completed.stdout)

    def test_check_workflow_thresholds_enforces_minimized_report_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "workflow.json"
            summary_path = root / "summary.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "thresholds",
                        "target": "linux",
                        "arch": "amd64",
                        "runner_profiles_path": "configs/targets/linux/runner_profiles.baseline.json",
                        "target_config_path": "configs/targets/linux/target.json",
                        "paths": {
                            "build_dir": str(root / "build"),
                            "artifacts_dir": str(root / "artifacts"),
                            "reports_dir": str(root / "reports"),
                            "eligible_file": str(root / "eligible.jsonl"),
                            "temp_dir": str(root / "tmp"),
                        },
                        "normalization": {"preview_bytes": 32},
                        "classification": {"no_diff": "NO_DIFF"},
                        "thresholds": {
                            "smoke": {
                                "require_minimized_report": True
                            }
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            summary_path.write_text(json.dumps({}), encoding="utf-8")
            env = os.environ.copy()
            env["SYZABI_CONFIG_PATH"] = str(config_path)
            completed = subprocess.run(
                [
                    "python3",
                    "tools/check_workflow_thresholds.py",
                    "--workflow",
                    "thresholds",
                    "--campaign",
                    "smoke",
                    "--summary",
                    str(summary_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("minimized-report.json", completed.stderr + completed.stdout)

            (summary_path.parent / "minimized-report.json").write_text("{}", encoding="utf-8")
            completed = subprocess.run(
                [
                    "python3",
                    "tools/check_workflow_thresholds.py",
                    "--workflow",
                    "thresholds",
                    "--campaign",
                    "smoke",
                    "--summary",
                    str(summary_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
