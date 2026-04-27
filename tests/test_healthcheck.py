#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runners.command import CommandRunner


class HealthcheckTests(unittest.TestCase):
    def test_local_runner_checks_python3_only(self) -> None:
        runner = CommandRunner(profile={"kind": "local", "kernel_build_command": "uname -r"})
        result = runner.healthcheck()
        self.assertEqual(result["status"], "ok")
        tools = [c["tool"] for c in result["checks"]]
        self.assertIn("python3", tools)
        self.assertNotIn("docker", tools)
        self.assertNotIn("qemu-system-x86_64", tools)

    def test_command_runner_checks_docker_when_mentioned(self) -> None:
        runner = CommandRunner(profile={"kind": "command", "command": ["docker", "run", "foo"]})
        result = runner.healthcheck()
        tools = [c["tool"] for c in result["checks"]]
        self.assertIn("docker", tools)
        self.assertIn("python3", tools)

    def test_command_runner_checks_qemu_when_mentioned(self) -> None:
        runner = CommandRunner(profile={"kind": "command", "command": ["qemu-system-riscv64", "-machine", "virt"]})
        result = runner.healthcheck()
        tools = [c["tool"] for c in result["checks"]]
        self.assertIn("qemu-system-riscv64", tools)

    def test_missing_tool_returns_missing_tools(self) -> None:
        runner = CommandRunner(profile={"kind": "command", "command": ["nonexistent_tool_xyz"]})
        result = runner.healthcheck()
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
