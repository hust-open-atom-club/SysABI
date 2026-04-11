from __future__ import annotations

import unittest
from argparse import Namespace
from pathlib import Path
import tempfile
from unittest.mock import patch

from orchestrator.common import load_json
from targets.asterinas.scml import AsterinasSCMLGate, AsterinasSCMLSource, sctrace_command
from tools.build_scml_manifest import build_manifest
from tools.derive_scml_allowed_sequences import load_manifest_index
from tools.preflight_scml_gate import (
    classify_sctrace_line,
    evidence_root,
    output_targets,
    parse_sctrace_lines,
    restore_artifact_root_permissions,
    run_preflight,
    run_command,
)


class SCMLPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source_root = Path("third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage")
        if not source_root.exists():
            raise unittest.SkipTest("requires third_party/asterinas SCML coverage checkout")
        cls.cfg = load_json("configs/asterinas_scml_rules.json")
        manifest = build_manifest(
            target="asterinas",
            repo_dir=Path("third_party/asterinas"),
            source_root=source_root,
        )
        profile = load_json(cls.cfg["generation_profile_path"])
        cls.manifest_index = load_manifest_index(manifest, profile)
        cls.source = AsterinasSCMLSource(cls.cfg)
        cls.gate = AsterinasSCMLGate(cfg=cls.cfg, manifest_index=cls.manifest_index)

    def test_parse_sctrace_lines_keeps_only_relevant_diagnostics(self) -> None:
        lines = parse_sctrace_lines(
            "supported line\n",
            "Unsupported syscall: lseek(3, 0, SEEK_HOLE) = 0\nStrace Parse Error: ???\nnoise\n",
        )
        self.assertEqual(
            lines,
            [
                "Unsupported syscall: lseek(3, 0, SEEK_HOLE) = 0",
                "Strace Parse Error: ???",
            ],
        )

    def test_lseek_seek_hole_is_flag_pattern(self) -> None:
        reasons = classify_sctrace_line(
            "Unsupported syscall: lseek(3</tmp/x>, 0, SEEK_HOLE) = 0",
            self.manifest_index,
            self.cfg,
        )
        self.assertEqual(reasons, ["unsupported_flag_pattern"])

    def test_renameat2_exchange_is_flag_pattern(self) -> None:
        reasons = classify_sctrace_line(
            'Unsupported syscall: renameat2(AT_FDCWD, "a", AT_FDCWD, "b", RENAME_EXCHANGE) = 0',
            self.manifest_index,
            self.cfg,
        )
        self.assertEqual(reasons, ["unsupported_flag_pattern"])

    def test_clone3_is_struct_pattern(self) -> None:
        reasons = classify_sctrace_line(
            "Unsupported syscall: clone3({flags=0xdeadbeef, exit_signal=0}, 88) = -1 EINVAL (Invalid argument)",
            self.manifest_index,
            self.cfg,
        )
        self.assertEqual(reasons, ["unsupported_struct_pattern"])

    def test_mount_falls_back_to_path_pattern(self) -> None:
        manifest_index = dict(self.manifest_index)
        manifest_index["mount"] = {
            "name": "mount",
            "generation_enabled": True,
        }
        reasons = classify_sctrace_line(
            'Unsupported syscall: mount("src", "/mnt", "ext4", MS_REMOUNT, NULL) = -1 EPERM (Operation not permitted)',
            manifest_index,
            self.cfg,
        )
        self.assertEqual(reasons, ["unsupported_path_pattern"])

    def test_target_syscall_filter_ignores_startup_noise(self) -> None:
        reasons = classify_sctrace_line(
            'Unsupported syscall: execve("/tmp/testcase.bin", ["testcase.bin"], 0x0) = 0',
            self.manifest_index,
            self.cfg,
            target_syscalls={"lseek"},
        )
        self.assertEqual(reasons, [])

    def test_filtered_runs_write_to_debug_outputs(self) -> None:
        args = Namespace(program_id="abc123", limit=None)
        targets = output_targets(args, self.cfg)
        self.assertIn("debug-preflight", str(targets["eligible_file"]))
        self.assertTrue(str(targets["eligible_file"]).endswith("eligible.jsonl"))
        self.assertTrue(str(targets["rejections_file"]).endswith("scml-rejections.jsonl"))
        self.assertTrue(str(targets["summary_file"]).endswith("preflight-summary.json"))
        self.assertIn("/debug/", str(evidence_root(args, self.cfg, "pid123")))

    def test_capability_source_and_gate_abstraction_loads_manifest_index(self) -> None:
        source_index = self.source.load_manifest_index()
        self.assertIn("openat", source_index)
        self.assertEqual(
            self.gate.classify_line(
                "Unsupported syscall: clone3({flags=0xdeadbeef, exit_signal=0}, 88) = -1 EINVAL (Invalid argument)",
                target_syscalls={"clone3"},
            ),
            ["unsupported_struct_pattern"],
        )

    def test_run_command_times_out(self) -> None:
        result = run_command(
            ["python3", "-c", "import time; time.sleep(1)"],
            cwd=Path("."),
            timeout_sec=0,
        )
        self.assertTrue(result["timed_out"])

    def test_sctrace_command_skips_non_executable_in_tree_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            release = root / "release" / "sctrace"
            debug = root / "debug" / "sctrace"
            release.parent.mkdir(parents=True, exist_ok=True)
            debug.parent.mkdir(parents=True, exist_ok=True)
            release.write_text("not executable", encoding="utf-8")
            debug.write_text("not executable", encoding="utf-8")
            with patch("targets.asterinas.scml.resolve_repo_path", side_effect=[release, debug]), patch(
                "targets.asterinas.scml.shutil.which",
                return_value="/usr/bin/sctrace",
            ), patch("targets.asterinas.scml.os.access", return_value=False):
                command = sctrace_command([Path("a.scml")], Path("input.strace"))
        self.assertEqual(command[0], "/usr/bin/sctrace")

    def test_restore_artifact_root_permissions_makes_directory_writable_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            root.chmod(0o000)
            restore_artifact_root_permissions(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)

    def test_run_preflight_rejects_runtime_failure_even_without_sctrace_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            normalized = root / "program.syz"
            meta = root / "program.json"
            normalized.write_text("openat(0x0)\n", encoding="utf-8")
            meta.write_text('{"full_syscall_list":["openat"]}', encoding="utf-8")
            entry = {
                "program_id": "prog",
                "normalized_path": str(normalized),
                "meta_path": str(meta),
            }
            args = Namespace(program_id=None, limit=None, jobs=1, source_eligible_file=None)
            cfg = {
                **self.cfg,
                "paths": {**self.cfg["paths"], "reports_dir": str(root / "reports")},
                "preflight": {
                    **self.cfg["preflight"],
                    "artifact_dir": str(root / "artifacts"),
                    "rejection_taxonomy": {
                        **self.cfg["preflight"]["rejection_taxonomy"],
                        "preflight_runtime_failure": "preflight_runtime_failure",
                    },
                },
            }

            with patch(
                "tools.preflight_scml_gate.build_one",
                return_value={"status": "ok", "testcase_bin": str(root / "testcase.bin")},
            ), patch(
                "tools.preflight_scml_gate.resolve_repo_path",
                side_effect=lambda path: Path(path),
            ), patch(
                "tools.preflight_scml_gate.run_command",
                side_effect=[
                    {"stdout": "", "stderr": "boom", "returncode": 1, "timed_out": False},
                    AssertionError("sctrace should not run after runtime failure"),
                ],
            ):
                accepted, rejected = run_preflight(
                    entry,
                    args=args,
                    cfg=cfg,
                    gate=self.gate,
                    source=self.source,
                )

        self.assertIsNone(accepted)
        self.assertEqual(rejected["scml_preflight_status"], "rejected_by_scml")
        self.assertEqual(rejected["scml_rejection_reasons"], ["preflight_runtime_failure"])


if __name__ == "__main__":
    unittest.main()
