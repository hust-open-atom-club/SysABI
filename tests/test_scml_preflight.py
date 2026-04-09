from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.common import load_json
from tools.build_scml_manifest import build_manifest
from tools.derive_scml_allowed_sequences import load_manifest_index
from tools.preflight_scml_gate import classify_sctrace_line, parse_sctrace_lines


class SCMLPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_json("configs/asterinas_scml_rules.json")
        manifest = build_manifest(
            target="asterinas",
            repo_dir=Path("third_party/asterinas"),
            source_root=Path(
                "third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage"
            ),
        )
        profile = load_json(cls.cfg["generation_profile_path"])
        cls.manifest_index = load_manifest_index(manifest, profile)

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


if __name__ == "__main__":
    unittest.main()
