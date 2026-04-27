#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.vm_runner import sample_external_state


class ExternalStateLimitsTests(unittest.TestCase):
    def test_sample_limits_max_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for i in range(150):
                (root / f"file{i}.txt").write_text("x", encoding="utf-8")
            result = sample_external_state(root, limits={"max_files": 100})
            self.assertLessEqual(len(result["files"]), 100)
            self.assertTrue(result["truncated"])

    def test_sample_limits_max_total_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for i in range(10):
                (root / f"file{i}.txt").write_text("x" * 1024 * 1024, encoding="utf-8")
            result = sample_external_state(root, limits={"max_total_size_bytes": 2 * 1024 * 1024})
            self.assertTrue(result["truncated"])

    def test_sample_limits_max_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "big.txt").write_text("x" * 10000, encoding="utf-8")
            result = sample_external_state(root, limits={"max_file_size": 100})
            self.assertEqual(len(result["files"]), 1)
            # SHA256 should be computed from truncated content
            self.assertIn("sha256", result["files"][0])

    def test_sample_no_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "a.txt").write_text("hello", encoding="utf-8")
            result = sample_external_state(root)
            self.assertEqual(len(result["files"]), 1)
            self.assertNotIn("truncated", result)  # truncated only present when True


if __name__ == "__main__":
    unittest.main()
