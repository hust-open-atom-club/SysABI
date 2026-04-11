from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.common import configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl
from tools import derive_scml_allowed_sequences
from tools.build_scml_manifest import build_manifest
from tools.derive_scml_allowed_sequences import derive_rejection, load_manifest_index, merge_source_rows


class SCMLDerivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source_root = Path("third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage")
        if not source_root.exists():
            raise unittest.SkipTest("requires third_party/asterinas SCML coverage checkout")
        cls.cfg = load_json("configs/asterinas_scml_rules.json")
        cls.manifest = build_manifest(
            target="asterinas",
            repo_dir=Path("third_party/asterinas"),
            source_root=source_root,
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

    def test_merge_source_rows_prefers_generated_metadata_and_unions_lists(self) -> None:
        rows = merge_source_rows(
            [
                {
                    "program_id": "prog",
                    "workflow": "baseline",
                    "source_modes": ["existing_corpus"],
                    "covered_target_syscalls": ["openat"],
                }
            ],
            [
                {
                    "program_id": "prog",
                    "workflow": "asterinas_scml",
                    "source_modes": ["syz_generate"],
                    "covered_target_syscalls": ["close"],
                }
            ],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workflow"], "asterinas_scml")
        self.assertEqual(rows[0]["source_modes"], ["existing_corpus", "syz_generate"])
        self.assertEqual(rows[0]["covered_target_syscalls"], ["close", "openat"])

    def test_main_merges_generated_source_rows_into_static_eligible(self) -> None:
        previous_workflow = None
        previous_config = None
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "scml-manifest.json"
            profile_path = root / "generation-profile.json"
            config_path = root / "scml_rules.json"
            baseline_file = root / "baseline.jsonl"
            generated_file = root / "generated.jsonl"
            static_eligible_file = root / "static.jsonl"
            reports_dir = root / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)

            baseline_meta = root / "baseline-meta.json"
            generated_meta = root / "generated-meta.json"
            dump_json(baseline_meta, {"call_count": 1, "full_syscall_list": ["openat"]})
            dump_json(generated_meta, {"call_count": 1, "full_syscall_list": ["close"]})
            dump_jsonl(
                baseline_file,
                [
                    {
                        "program_id": "baseline-prog",
                        "workflow": "baseline",
                        "normalized_path": "/tmp/baseline.syz",
                        "meta_path": str(baseline_meta),
                    }
                ],
            )
            dump_jsonl(
                generated_file,
                [
                    {
                        "program_id": "generated-prog",
                        "workflow": "asterinas_scml",
                        "normalized_path": "/tmp/generated.syz",
                        "meta_path": str(generated_meta),
                        "source_modes": ["syz_generate"],
                        "covered_target_syscalls": ["close"],
                    }
                ],
            )
            dump_json(
                manifest_path,
                {
                    "categories": {
                        "file-and-directory-operations": {
                            "syscalls": {
                                "openat": {"name": "openat", "generation_enabled": True, "defer_reason": None},
                                "close": {"name": "close", "generation_enabled": True, "defer_reason": None},
                            }
                        }
                    }
                },
            )
            dump_json(
                profile_path,
                {
                    "generation": {"sequence_length": {"min": 1, "max": 4}},
                    "enabled_categories": ["file-and-directory-operations"],
                    "deferred_syscalls": {},
                    "deferred_categories": {},
                },
            )
            dump_json(
                config_path,
                {
                    "workflow": "scmlderive",
                    "compat_manifest_path": str(manifest_path),
                    "generation_profile_path": str(profile_path),
                    "paths": {
                        "reports_dir": str(reports_dir),
                        "static_eligible_file": str(static_eligible_file),
                        "eligible_file": str(static_eligible_file),
                    },
                    "derivation": {
                        "source_eligible_file": str(baseline_file),
                        "generated_source_eligible_file": str(generated_file),
                        "accept_reasons": ["ok"],
                        "rejection_taxonomy": {
                            "specialized_variant": "specialized_variant_not_allowed",
                            "sequence_too_short": "sequence_too_short",
                            "sequence_too_long": "sequence_too_long",
                            "syscall_not_in_manifest": "syscall_not_in_manifest",
                            "deferred_category": "deferred_category",
                            "manifest_disabled": "manifest_disabled",
                        },
                    },
                },
            )

            previous_workflow = os.environ.get("SYZABI_WORKFLOW")
            previous_config = os.environ.get("SYZABI_CONFIG_PATH")
            configure_runtime(workflow="scmlderive", config_path=str(config_path))
            with patch("sys.argv", ["derive_scml_allowed_sequences.py", "--workflow", "scmlderive"]):
                derive_scml_allowed_sequences.main()

            eligible_rows = load_jsonl(static_eligible_file)
            summary = load_json(reports_dir / "derivation-summary.json")
            derivation_rejections = load_jsonl(reports_dir / "derivation-rejections.jsonl")

        if previous_workflow is None:
            os.environ.pop("SYZABI_WORKFLOW", None)
        else:
            os.environ["SYZABI_WORKFLOW"] = previous_workflow
        if previous_config is None:
            os.environ.pop("SYZABI_CONFIG_PATH", None)
        else:
            os.environ["SYZABI_CONFIG_PATH"] = previous_config

        self.assertEqual([row["program_id"] for row in eligible_rows], ["baseline-prog", "generated-prog"])
        self.assertEqual(summary["base_source_total"], 1)
        self.assertEqual(summary["generated_source_total"], 1)
        self.assertEqual(summary["source_total"], 2)
        self.assertEqual(summary["rejections_file"], str(reports_dir / "derivation-rejections.jsonl"))
        self.assertEqual(derivation_rejections, [])
        self.assertFalse((reports_dir / "scml-rejections.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
