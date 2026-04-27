from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools import tgoskits_launch as launch


def init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "codex@example.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Codex"], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("fake tgoskits\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class TGOSKitsLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_workflow = os.environ.get("SYZABI_WORKFLOW")
        self.previous_config = os.environ.get("SYZABI_CONFIG_PATH")
        self.previous_tgoskits = os.environ.get("SYZABI_TGOSKITS_DIR")
        self.previous_flag = os.environ.get("SYZABI_ENABLE_TGOSKITS")
        self.previous_path = os.environ.get("PATH")

    def tearDown(self) -> None:
        if self.previous_workflow is None:
            os.environ.pop("SYZABI_WORKFLOW", None)
        else:
            os.environ["SYZABI_WORKFLOW"] = self.previous_workflow
        if self.previous_config is None:
            os.environ.pop("SYZABI_CONFIG_PATH", None)
        else:
            os.environ["SYZABI_CONFIG_PATH"] = self.previous_config
        if self.previous_tgoskits is None:
            os.environ.pop("SYZABI_TGOSKITS_DIR", None)
        else:
            os.environ["SYZABI_TGOSKITS_DIR"] = self.previous_tgoskits
        if self.previous_flag is None:
            os.environ.pop("SYZABI_ENABLE_TGOSKITS", None)
        else:
            os.environ["SYZABI_ENABLE_TGOSKITS"] = self.previous_flag
        if self.previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.previous_path

    def make_fake_starry_config(self, root: Path, *, revision: str | None = None) -> tuple[Path, Path]:
        repo = root / "tgoskits"
        workspace = repo / "os" / "StarryOS"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "make").mkdir(parents=True, exist_ok=True)
        current_revision = init_git_repo(repo)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
        for tool in ("debugfs", "qemu-system-riscv64", "riscv64-linux-musl-gcc", "make"):
            write_executable(fake_bin / tool, "#!/bin/sh\nexit 0\n")

        config_path = root / "starry.json"
        target_config_path = root / "starry-target.json"
        config_path.write_text(
            json.dumps(
                {
                    "workflow": "fake_starry",
                    "target": "tgoskits_starryos",
                    "arch": "riscv64",
                    "runner_profiles_path": "configs/targets/tgoskits_starryos/runner_profiles.tgoskits_starryos.json",
                    "target_config_path": str(target_config_path),
                    "paths": {
                        "build_dir": str(root / "build"),
                        "artifacts_dir": str(root / "artifacts"),
                        "reports_dir": str(root / "reports"),
                        "eligible_file": str(root / "eligible.jsonl"),
                        "temp_dir": str(root / "tmp"),
                        "syzkaller_dir": str(root / "syzkaller"),
                    },
                    "normalization": {"preview_bytes": 32},
                    "classification": {"no_diff": "NO_DIFF"},
                    "thresholds": {"smoke": {}, "signoff": {}},
                    "trace": {"events_transport": "stdout"},
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        target_config_path.write_text(
            json.dumps(
                {
                    "build_info_path": str(root / "build-info.json"),
                    "default_mode": "shell-qemu",
                    "revision": revision or current_revision,
                    "repo_dir_env": "SYZABI_TGOSKITS_DIR",
                    "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                    "supported_arches": ["riscv64"],
                    "toolchain_probes": ["make", "debugfs", "qemu-system-riscv64", "riscv64-linux-musl-gcc"],
                    "workspace_subdir": "os/StarryOS",
                    "disk_image_path": "os/StarryOS/make/disk.img",
                    "guest_binary_path": "/bin/testcase.candidate.bin",
                    "shell_prompt": "starry:~#",
                    "serial_transport": "stdio",
                    "trace_marker_prefix": "__SYZABI_TRACE_EVENT__ ",
                    "prepare_commands": [],
                    "shell_launch_command": ["make", "ARCH={arch}", "justrun"],
                    "healthcheck_shell_command": "pwd && echo __SYZABI_HEALTHCHECK_OK__",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.environ["SYZABI_CONFIG_PATH"] = str(config_path)
        os.environ["SYZABI_TGOSKITS_DIR"] = str(repo)
        os.environ["SYZABI_WORKFLOW"] = "fake_starry"
        return repo, config_path

    def test_preflight_prints_payload(self) -> None:
        cfg = {
            "target": "tgoskits_starryos",
            "paths": {"eligible_file": "eligible.jsonl", "syzkaller_dir": "third_party/syzkaller"},
        }
        captured = io.StringIO()
        with patch("tools.tgoskits_launch.load_cfg", return_value=cfg), patch(
            "tools.tgoskits_launch.checked_preflight_payload",
            return_value={"target": "tgoskits_starryos", "revision": "abc"},
        ), patch("sys.argv", ["tools/tgoskits_launch.py", "--workflow", "tgoskits_starryos", "preflight"]), redirect_stdout(captured):
            launch.main()
        self.assertEqual(json.loads(captured.getvalue())["target"], "tgoskits_starryos")

    def test_healthcheck_runs_entrypoint_after_preflight(self) -> None:
        cfg = {
            "target": "tgoskits_starryos",
            "paths": {"eligible_file": "eligible.jsonl", "syzkaller_dir": "third_party/syzkaller"},
        }
        commands: list[list[str]] = []
        with patch("tools.tgoskits_launch.load_cfg", return_value=cfg), patch(
            "tools.tgoskits_launch.checked_preflight_payload",
            return_value={"target": "tgoskits_starryos"},
        ), patch("tools.tgoskits_launch.run_command", side_effect=lambda command, env: commands.append(command)), patch(
            "sys.argv", ["tools/tgoskits_launch.py", "--workflow", "tgoskits_starryos", "healthcheck"]
        ):
            launch.main()
        self.assertEqual(commands, [[sys.executable, "targets/entrypoint.py", "--workflow", "tgoskits_starryos", "--healthcheck"]])

    def test_starry_campaign_runs_healthcheck_before_build_and_scheduler(self) -> None:
        cfg = {
            "target": "tgoskits_starryos",
            "paths": {"eligible_file": "eligible.jsonl", "syzkaller_dir": "third_party/syzkaller"},
        }
        commands: list[list[str]] = []
        with patch("tools.tgoskits_launch.load_cfg", return_value=cfg), patch(
            "tools.tgoskits_launch.checked_preflight_payload",
            return_value={"target": "tgoskits_starryos"},
        ), patch("tools.tgoskits_launch.campaign_preflight_payload", return_value={"target": "tgoskits_starryos"}), patch("tools.tgoskits_launch.ensure_prog2c_exists"), patch(
            "tools.tgoskits_launch.run_command", side_effect=lambda command, env: commands.append(command)
        ), patch(
            "sys.argv",
            [
                "tools/tgoskits_launch.py",
                "--workflow",
                "tgoskits_starryos",
                "campaign",
                "--campaign",
                "smoke",
                "--eligible-file",
                "eligible.jsonl",
                "--limit",
                "1",
                "--jobs",
                "1",
            ],
        ):
            launch.main()
        self.assertEqual(
            commands,
            [
                [sys.executable, "targets/entrypoint.py", "--workflow", "tgoskits_starryos", "--healthcheck"],
                [sys.executable, "tools/prog2c_wrap.py", "--workflow", "tgoskits_starryos", "--eligible-file", "eligible.jsonl", "--jobs", "1", "--limit", "1"],
                [sys.executable, "orchestrator/scheduler.py", "--workflow", "tgoskits_starryos", "--campaign", "smoke", "--eligible-file", "eligible.jsonl", "--jobs", "1", "--limit", "1"],
            ],
        )

    def test_preflight_rejects_missing_feature_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_fake_starry_config(root)
            os.environ.pop("SYZABI_ENABLE_TGOSKITS", None)
            with patch("sys.argv", ["tools/tgoskits_launch.py", "--workflow", "fake_starry", "preflight"]):
                with self.assertRaises(SystemExit) as ctx:
                    launch.main()
            self.assertIn("SYZABI_ENABLE_TGOSKITS=1", str(ctx.exception))

    def test_preflight_rejects_missing_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_fake_starry_config(root)
            os.environ["SYZABI_ENABLE_TGOSKITS"] = "1"
            missing = root / "fake-bin" / "riscv64-linux-musl-gcc"
            missing.unlink()
            with patch("sys.argv", ["tools/tgoskits_launch.py", "--workflow", "fake_starry", "preflight"]):
                with self.assertRaises(SystemExit) as ctx:
                    launch.main()
            self.assertIn("missing required StarryOS tools", str(ctx.exception))

    def test_preflight_rejects_revision_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.make_fake_starry_config(root, revision="0000000000000000000000000000000000000000")
            os.environ["SYZABI_ENABLE_TGOSKITS"] = "1"
            with patch("sys.argv", ["tools/tgoskits_launch.py", "--workflow", "fake_starry", "preflight"]):
                with self.assertRaises(SystemExit) as ctx:
                    launch.main()
            self.assertIn("revision mismatch", str(ctx.exception))

    def test_arceos_healthcheck_does_not_require_replay_only_tools(self) -> None:
        cfg = {
            "workflow": "tgoskits_arceos_smoke",
            "target": "tgoskits_arceos",
            "paths": {"eligible_file": "eligible.jsonl", "syzkaller_dir": "third_party/syzkaller"},
        }
        commands: list[list[str]] = []
        with patch("tools.tgoskits_launch.load_cfg", return_value=cfg), patch(
            "tools.tgoskits_launch.checked_preflight_payload",
            return_value={"target": "tgoskits_arceos"},
        ), patch("tools.tgoskits_launch.run_command", side_effect=lambda command, env: commands.append(command)), patch(
            "sys.argv", ["tools/tgoskits_launch.py", "--workflow", "tgoskits_arceos_smoke", "healthcheck"]
        ):
            launch.main()
        self.assertEqual(commands, [[sys.executable, "targets/entrypoint.py", "--workflow", "tgoskits_arceos_smoke", "--healthcheck"]])

    def test_arceos_preflight_uses_replay_prerequisites(self) -> None:
        cfg = {
            "workflow": "tgoskits_arceos_smoke",
            "target": "tgoskits_arceos",
            "paths": {"eligible_file": "eligible.jsonl", "syzkaller_dir": "third_party/syzkaller"},
        }
        captured = io.StringIO()
        with patch("tools.tgoskits_launch.load_cfg", return_value=cfg), patch(
            "tools.tgoskits_launch.campaign_preflight_payload",
            return_value={"target": "tgoskits_arceos", "mode": "smoke-qemu"},
        ), patch("sys.argv", ["tools/tgoskits_launch.py", "--workflow", "tgoskits_arceos_smoke", "preflight"]), redirect_stdout(captured):
            launch.main()
        self.assertEqual(json.loads(captured.getvalue())["target"], "tgoskits_arceos")

    def test_campaign_preflight_routes_through_adapter_runner_errors(self) -> None:
        class FakeAdapter:
            name = "fake"

            def prepare_campaign_assets(self, cfg, args=None):
                raise RuntimeError("fake campaign failure")

            def runner_errors(self):
                return (RuntimeError,)

        cfg = {
            "target": "tgoskits_starryos",
            "paths": {"eligible_file": "eligible.jsonl", "syzkaller_dir": "third_party/syzkaller"},
        }
        with patch("tools.tgoskits_launch.load_cfg", return_value=cfg), patch(
            "tools.tgoskits_launch.resolve_adapter", return_value=FakeAdapter()
        ), patch("sys.argv", ["tools/tgoskits_launch.py", "--workflow", "tgoskits_starryos", "campaign", "--campaign", "smoke"]):
            with self.assertRaises(SystemExit) as ctx:
                launch.main()
        self.assertIn("fake campaign failure", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
