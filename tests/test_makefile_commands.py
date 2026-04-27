#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class MakefileCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_help_target_exists(self) -> None:
        run = subprocess.run(
            ["make", "-n", "help"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("SysABI", run.stdout)

    def test_unified_run_command_routes_to_run_workflow(self) -> None:
        run = subprocess.run(
            ["make", "-n", "run", "WORKFLOW=asterinas", "CAMPAIGN=smoke", "LIMIT=50", "JOBS=4"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("run-workflow WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=4", run.stdout)

    def test_deprecated_run_smoke_emits_warning(self) -> None:
        run = subprocess.run(
            ["make", "-n", "run-smoke"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("warning: run-smoke is deprecated", run.stdout)
        self.assertIn("run WORKFLOW=baseline CAMPAIGN=smoke LIMIT=100", run.stdout)

    def test_deprecated_run_asterinas_smoke_emits_warning(self) -> None:
        run = subprocess.run(
            ["make", "-n", "run-asterinas-smoke"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("warning: run-asterinas-smoke is deprecated", run.stdout)

    def test_unified_build_command_routes_to_build_workflow(self) -> None:
        run = subprocess.run(
            ["make", "-n", "build", "WORKFLOW=baseline", "LIMIT=100"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("build-workflow WORKFLOW=baseline LIMIT=100", run.stdout)

    def test_unified_analyze_command_routes_to_analyze_workflow(self) -> None:
        run = subprocess.run(
            ["make", "-n", "analyze", "WORKFLOW=asterinas"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("analyze-workflow WORKFLOW=asterinas", run.stdout)

    def test_deprecated_build_eligible_emits_warning(self) -> None:
        run = subprocess.run(
            ["make", "-n", "build-eligible"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("warning: build-eligible is deprecated", run.stdout)


if __name__ == "__main__":
    unittest.main()
