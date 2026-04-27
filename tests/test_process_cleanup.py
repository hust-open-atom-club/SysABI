#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runners.command import CommandRunner


class ProcessCleanupTests(unittest.TestCase):
    def test_cleanup_process_kills_process_group(self) -> None:
        runner = CommandRunner(profile={})
        # Start a child process that sleeps
        process = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.assertIsNotNone(process.pid)
        # Cleanup should succeed
        runner._cleanup_process(process)
        # After cleanup, process should be terminated
        time.sleep(0.2)
        self.assertIsNotNone(process.poll())

    def test_run_case_returns_timed_out_on_timeout(self) -> None:
        runner = CommandRunner(profile={})
        execution = runner.run_case(
            command=["python3", "-c", "import time; time.sleep(10)"],
            cwd=str(Path.cwd()),
            env={},
            timeout_sec=0.1,
        )
        self.assertTrue(execution.timed_out)
        self.assertIsNone(execution.returncode)


class ReturnCodeClassificationTests(unittest.TestCase):
    def test_classify_zero_returncode_as_ok(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(runner._classify_returncode(0, "", ""), "ok")

    def test_classify_none_returncode_as_infra_error(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(runner._classify_returncode(None, "", ""), "infra_error")

    def test_classify_panic_as_candidate_bug(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(
            runner._classify_returncode(1, "", "panicked at src/main.rs"),
            "candidate_bug",
        )

    def test_classify_stack_trace_as_candidate_bug(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(
            runner._classify_returncode(1, "Printing stack trace:", ""),
            "candidate_bug",
        )

    def test_classify_kernel_panic_as_candidate_bug(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(
            runner._classify_returncode(1, "", "Kernel panic - not syncing"),
            "candidate_bug",
        )

    def test_classify_segfault_as_candidate_bug(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(
            runner._classify_returncode(139, "", "segfault at 0x0"),
            "candidate_bug",
        )

    def test_classify_generic_error_as_infra_error(self) -> None:
        runner = CommandRunner(profile={})
        self.assertEqual(
            runner._classify_returncode(1, "some error", "no panic here"),
            "infra_error",
        )

    def test_run_case_sets_status_on_panic(self) -> None:
        runner = CommandRunner(profile={})
        execution = runner.run_case(
            command=["python3", "-c", 'import sys; print("panicked at main.rs"); sys.exit(1)'],
            cwd=str(Path.cwd()),
            env={},
            timeout_sec=5,
        )
        self.assertEqual(execution.returncode, 1)
        self.assertEqual(execution.status, "candidate_bug")


if __name__ == "__main__":
    unittest.main()
