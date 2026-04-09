from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.common import load_json
from tools.build_scml_manifest import build_manifest
from tools.derive_scml_allowed_sequences import derive_rejection, load_manifest_index


class SCMLDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = load_json("configs/asterinas_scml_rules.json")
        cls.manifest = build_manifest(
            target="asterinas",
            repo_dir=Path("third_party/asterinas"),
            source_root=Path(
                "third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage"
            ),
        )
        cls.profile = load_json(cls.cfg["generation_profile_path"])
        cls.manifest_index = load_manifest_index(cls.manifest, cls.profile)

    def test_supported_static_sequence_passes_derivation(self) -> None:
        meta = {
            "call_count": 3,
            "full_syscall_list": ["openat", "read", "close"],
        }
        self.assertEqual(
            derive_rejection(meta, self.manifest_index, self.profile, self.cfg),
            [],
        )

    def test_specialized_variant_is_rejected(self) -> None:
        meta = {
            "call_count": 1,
            "full_syscall_list": ["openat$fuse"],
        }
        self.assertEqual(
            derive_rejection(meta, self.manifest_index, self.profile, self.cfg),
            ["specialized_variant_not_allowed"],
        )

    def test_deferred_and_missing_syscalls_are_rejected(self) -> None:
        meta = {
            "call_count": 2,
            "full_syscall_list": ["mount", "bpf"],
        }
        reasons = derive_rejection(meta, self.manifest_index, self.profile, self.cfg)
        self.assertIn("deferred_category", reasons)
        self.assertIn("syscall_not_in_manifest", reasons)

    def test_sequence_length_gate_is_applied(self) -> None:
        meta = {
            "call_count": 13,
            "full_syscall_list": ["openat", "read", "close"],
        }
        reasons = derive_rejection(meta, self.manifest_index, self.profile, self.cfg)
        self.assertIn("sequence_too_long", reasons)

    def test_profile_projects_syscall_level_defer_reason(self) -> None:
        reboot = self.manifest_index["reboot"]
        self.assertFalse(reboot["generation_enabled"])
        self.assertEqual(reboot["defer_reason"], "privileged_or_environment_destructive")
        meta = {
            "call_count": 1,
            "full_syscall_list": ["reboot"],
        }
        self.assertEqual(
            derive_rejection(meta, self.manifest_index, self.profile, self.cfg),
            ["deferred_category"],
        )


if __name__ == "__main__":
    unittest.main()
