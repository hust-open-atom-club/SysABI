from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.capability import load_manifest_index
from orchestrator.common import configure_runtime, dump_json, dump_jsonl, load_json, load_jsonl
from tools import export_scml_targets, generate_scml_candidates
from tools.build_scml_manifest import build_manifest


class SCMLGenerationManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = build_manifest(
            target="asterinas",
            repo_dir=Path("third_party/asterinas"),
            source_root=Path(
                "third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage"
            ),
        )
        cls.profile = load_json("compat_specs/asterinas/generation-profile.json")
        cls.manifest_index = load_manifest_index(cls.manifest, cls.profile)

    def test_manifest_includes_generator_metadata(self) -> None:
        openat = self.manifest_index["openat"]
        clone = self.manifest_index["clone"]
        getppid = self.manifest_index["getppid"]
        fork = self.manifest_index["fork"]

        self.assertTrue(openat["syzkaller_base_available"])
        self.assertEqual(openat["generator_class"], "base_only")
        self.assertEqual(openat["generator_gap_reason"], "none")

        self.assertEqual(clone["generator_class"], "variant_only")
        self.assertEqual(clone["generator_gap_reason"], "missing_base_definition")

        self.assertEqual(getppid["generator_class"], "unavailable")
        self.assertEqual(getppid["generator_gap_reason"], "disabled_in_syzkaller")

        self.assertEqual(fork["generator_class"], "unavailable")
        self.assertEqual(fork["generator_gap_reason"], "missing_description")

    def test_target_export_only_keeps_profile_enabled_syscalls(self) -> None:
        rows = export_scml_targets.build_generation_targets(self.manifest_index)
        syscall_names = {row["syscall_name"] for row in rows}
        expected = {
            name
            for name, entry in self.manifest_index.items()
            if entry.get("generation_enabled", True)
        }

        self.assertEqual(syscall_names, expected)
        self.assertNotIn("mount", syscall_names)
        self.assertIn("openat", syscall_names)


class SCMLCandidateGenerationTests(unittest.TestCase):
    def test_run_syzabi_generate_keeps_partial_output_on_budget_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fake_bin = root / "syzabi_generate"
            fake_bin.write_text("", encoding="utf-8")
            fake_bin.chmod(0o755)
            cfg = {
                "target_os": "linux",
                "arch": "amd64",
                "paths": {
                    "temp_dir": str(root / "tmp"),
                },
            }

            def fake_run(cmd, **_kwargs):
                output_dir = Path(cmd[cmd.index("-output-dir") + 1])
                (output_dir / "0000-test.syz").write_text("openat(0x0)\n", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 2, "", "generated 1/4 unique programs\n")

            with patch("tools.generate_scml_candidates.project_bin", return_value=fake_bin), patch(
                "tools.generate_scml_candidates.subprocess.run",
                side_effect=fake_run,
            ):
                generated = generate_scml_candidates.run_syzabi_generate(
                    syscall_name="openat",
                    cfg=cfg,
                    budget=4,
                    preferred_length=4,
                    seed=1,
                )

            self.assertEqual(len(generated), 1)
            self.assertTrue(generated[0].exists())
            self.assertIn("scml-syzgen-openat-", generated[0].parent.name)

    def test_persist_inspected_program_returns_json_meta_payload_for_new_program(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "input.syz"
            program_path.write_text("openat(0x0)\n", encoding="utf-8")
            cfg = {
                "workflow": "asterinas_scml",
                "paths": {
                    "generated_raw_dir": str(root / "raw"),
                    "generated_normalized_dir": str(root / "normalized"),
                    "generated_meta_dir": str(root / "meta"),
                },
            }

            def fake_inspect(_path):
                return {
                    "program_id": "prog-1",
                    "normalized_syz": "openat(0x0)\n",
                    "target_os": "linux",
                    "arch": "amd64",
                    "syscall_list": ["openat"],
                    "full_syscall_list": ["openat"],
                    "resource_classes": [],
                    "uses_pseudo_syscalls": False,
                    "uses_threading_sensitive_features": False,
                    "call_count": 1,
                }

            row, meta = generate_scml_candidates.persist_inspected_program(
                program_path,
                cfg=cfg,
                source_mode="syz_generate",
                inspect_program_fn=fake_inspect,
            )

        self.assertIsInstance(meta, dict)
        self.assertEqual(meta["program_id"], "prog-1")
        self.assertEqual(row["program_id"], "prog-1")

    def test_merge_candidate_rows_unions_target_coverage_and_source_modes(self) -> None:
        rows = generate_scml_candidates.merge_candidate_rows(
            [
                {
                    "program_id": "p1",
                    "workflow": "asterinas_scml",
                    "source_mode": "existing_corpus",
                    "source_modes": ["existing_corpus"],
                    "source_workflow": "baseline",
                    "source_program_id": "p1",
                    "normalized_path": "/tmp/p1.syz",
                    "meta_path": "/tmp/p1.json",
                    "covered_target_syscalls": ["openat"],
                },
                {
                    "program_id": "p1",
                    "workflow": "asterinas_scml",
                    "source_mode": "syz_generate",
                    "source_modes": ["syz_generate"],
                    "source_workflow": "asterinas_scml",
                    "source_program_id": "p1",
                    "normalized_path": "/tmp/p1.syz",
                    "meta_path": "/tmp/p1.json",
                    "covered_target_syscalls": ["close"],
                },
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["covered_target_syscalls"], ["close", "openat"])
        self.assertEqual(rows[0]["source_modes"], ["existing_corpus", "syz_generate"])

    def test_generate_rows_for_helper_only_target_reports_gap(self) -> None:
        target = {
            "syscall_name": "clone",
            "category": "process-and-thread-management",
            "support_tier": "partial",
            "generator_class": "helper_only",
            "generator_gap_reason": "disabled_in_syzkaller",
        }
        rows, coverage, gap = generate_scml_candidates.generate_rows_for_target(
            target,
            cfg={"workflow": "asterinas_scml"},
            settings={
                "source_modes": ["syz_generate"],
                "per_target_budget": 1,
                "preferred_length": 4,
            },
            all_targets={"clone"},
            existing_corpus_index={},
            template_index={},
        )
        self.assertEqual(rows, [])
        self.assertEqual(coverage["candidate_count"], 0)
        self.assertIsNotNone(gap)
        self.assertEqual(gap["generator_gap_reason"], "disabled_in_syzkaller")

    def test_load_template_index_blocks_variant_templates_without_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            template = root / "openat.syz"
            template.write_text("openat$fuse(0x0)\n", encoding="utf-8")
            cfg = {
                "workflow": "asterinas_scml",
                "paths": {
                    "generated_raw_dir": str(root / "raw"),
                    "generated_normalized_dir": str(root / "normalized"),
                    "generated_meta_dir": str(root / "meta"),
                },
            }
            target_rows = [
                {
                    "syscall_name": "openat",
                    "category": "file-and-directory-operations",
                    "support_tier": "partial",
                }
            ]

            def fake_persist(*args, **kwargs):
                return (
                    {
                        "program_id": "template-1",
                        "workflow": "asterinas_scml",
                        "source_mode": "seed_templates",
                        "source_modes": ["seed_templates"],
                        "source_workflow": "asterinas_scml",
                        "source_program_id": "template-1",
                        "normalized_path": "/tmp/template.syz",
                        "meta_path": "/tmp/template.json",
                        "covered_target_syscalls": [],
                    },
                    {
                        "syscall_list": ["openat"],
                        "full_syscall_list": ["openat$fuse"],
                    },
                )

            with patch("tools.generate_scml_candidates.persist_inspected_program", side_effect=fake_persist):
                blocked = generate_scml_candidates.load_template_index(
                    str(root),
                    settings={"allow_variant_templates": set()},
                    cfg=cfg,
                    target_rows=target_rows,
                )
                allowed = generate_scml_candidates.load_template_index(
                    str(root),
                    settings={"allow_variant_templates": {"openat"}},
                    cfg=cfg,
                    target_rows=target_rows,
                )

        self.assertEqual(blocked, {})
        self.assertEqual(len(allowed["openat"]), 1)

    def test_existing_corpus_limit_is_applied_after_meta_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_file = root / "baseline.jsonl"
            invalid_meta_path = root / "invalid.json"
            valid_meta_path = root / "valid.json"
            dump_json(
                invalid_meta_path,
                {
                    "syscall_list": ["openat"],
                    "full_syscall_list": ["openat$fuse"],
                },
            )
            dump_json(
                valid_meta_path,
                {
                    "syscall_list": ["openat"],
                    "full_syscall_list": ["openat"],
                },
            )
            dump_jsonl(
                source_file,
                [
                    {
                        "program_id": "invalid",
                        "workflow": "baseline",
                        "normalized_path": "/tmp/invalid.syz",
                        "meta_path": str(invalid_meta_path),
                    },
                    {
                        "program_id": "valid",
                        "workflow": "baseline",
                        "normalized_path": "/tmp/valid.syz",
                        "meta_path": str(valid_meta_path),
                    },
                ],
            )

            index = generate_scml_candidates.load_existing_corpus_index(
                str(source_file),
                workflow="asterinas_scml",
                target_rows=[{"syscall_name": "openat"}],
                limit_per_target=1,
                validate_meta_fn=lambda meta: "$" not in meta["full_syscall_list"][0],
            )

        self.assertEqual([row["program_id"] for row in index["openat"]], ["valid"])

    def test_generate_rows_for_target_surfaces_generator_failure(self) -> None:
        target = {
            "syscall_name": "openat",
            "category": "file-and-directory-operations",
            "support_tier": "partial",
            "generator_class": "base_only",
            "generator_gap_reason": "none",
        }

        def fake_generator(**_kwargs):
            raise generate_scml_candidates.GeneratorExecutionError(
                syscall_name="openat",
                returncode=2,
                stdout="",
                stderr="boom",
            )

        rows, coverage, gap = generate_scml_candidates.generate_rows_for_target(
            target,
            cfg={"workflow": "asterinas_scml"},
            settings={
                "source_modes": ["syz_generate"],
                "per_target_budget": 1,
                "preferred_length": 4,
            },
            all_targets={"openat"},
            existing_corpus_index={},
            template_index={},
            generator_fn=fake_generator,
        )

        self.assertEqual(rows, [])
        self.assertEqual(coverage["candidate_count"], 0)
        self.assertEqual(gap["generator_gap_reason"], "generator_failed")
        self.assertIn("returncode=2", gap["generator_error"])

    def test_generate_rows_for_target_keeps_successful_generated_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            staging = root / "tmp" / "scml-syzgen-openat-test"
            staging.mkdir(parents=True, exist_ok=True)
            generated_path = staging / "candidate.syz"
            generated_path.write_text("openat(0x0)\n", encoding="utf-8")
            target = {
                "syscall_name": "openat",
                "category": "file-and-directory-operations",
                "support_tier": "partial",
                "generator_class": "base_only",
                "generator_gap_reason": "none",
            }
            cfg = {
                "workflow": "asterinas_scml",
                "paths": {
                    "temp_dir": str(root / "tmp"),
                },
            }

            def fake_persist(*_args, **_kwargs):
                return (
                    {
                        "program_id": "generated-openat",
                        "workflow": "asterinas_scml",
                        "source_mode": "syz_generate",
                        "source_modes": ["syz_generate"],
                        "source_workflow": "asterinas_scml",
                        "source_program_id": "generated-openat",
                        "normalized_path": "/tmp/generated-openat.syz",
                        "meta_path": "/tmp/generated-openat.json",
                        "covered_target_syscalls": [],
                    },
                    {
                        "syscall_list": ["openat"],
                    },
                )

            with patch("tools.generate_scml_candidates.persist_inspected_program", side_effect=fake_persist):
                rows, coverage, gap = generate_scml_candidates.generate_rows_for_target(
                    target,
                    cfg=cfg,
                    settings={
                        "source_modes": ["syz_generate"],
                        "per_target_budget": 1,
                        "preferred_length": 4,
                    },
                    all_targets={"openat"},
                    existing_corpus_index={},
                    template_index={},
                    generator_fn=lambda **_kwargs: [generated_path],
                )

        self.assertEqual([row["program_id"] for row in rows], ["generated-openat"])
        self.assertEqual(coverage["candidate_count"], 1)
        self.assertIsNone(gap)
        self.assertFalse(generated_path.parent.exists())

    def test_generate_rows_for_target_surfaces_inspect_failure_without_aborting(self) -> None:
        target = {
            "syscall_name": "openat",
            "category": "file-and-directory-operations",
            "support_tier": "partial",
            "generator_class": "base_only",
            "generator_gap_reason": "none",
        }

        def fake_generator(**_kwargs):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "candidate.syz"
                path.write_text("openat(0x0)\n", encoding="utf-8")
                return [path]

        def fake_persist(*_args, **_kwargs):
            raise subprocess.CalledProcessError(returncode=1, cmd=["syzabi_inspect"], stderr="bad program")

        with patch("tools.generate_scml_candidates.persist_inspected_program", side_effect=fake_persist):
            rows, coverage, gap = generate_scml_candidates.generate_rows_for_target(
                target,
                cfg={"workflow": "asterinas_scml"},
                settings={
                    "source_modes": ["syz_generate"],
                    "per_target_budget": 1,
                    "preferred_length": 4,
                },
                all_targets={"openat"},
                existing_corpus_index={},
                template_index={},
                generator_fn=fake_generator,
            )

        self.assertEqual(rows, [])
        self.assertEqual(coverage["candidate_count"], 0)
        self.assertEqual(gap["generator_gap_reason"], "inspect_failed")
        self.assertIn("syzabi_inspect", gap["inspect_error"])


class SCMLGenerationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_workflow = os.environ.get("SYZABI_WORKFLOW")
        self.previous_config = os.environ.get("SYZABI_CONFIG_PATH")

    def tearDown(self) -> None:
        if self.previous_workflow is None:
            os.environ.pop("SYZABI_WORKFLOW", None)
        else:
            os.environ["SYZABI_WORKFLOW"] = self.previous_workflow
        if self.previous_config is None:
            os.environ.pop("SYZABI_CONFIG_PATH", None)
        else:
            os.environ["SYZABI_CONFIG_PATH"] = self.previous_config

    def test_export_and_generate_pipeline_can_run_from_temp_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "scml-manifest.json"
            profile_path = root / "generation-profile.json"
            config_path = root / "scml_rules.json"
            reports_dir = root / "reports"
            targets_file = root / "targets.jsonl"
            generated_file = root / "generated.jsonl"
            existing_file = root / "baseline.jsonl"
            normalized_path = root / "openat.syz"
            meta_path = root / "openat.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            normalized_path.write_text("openat(0x0)\n", encoding="utf-8")
            dump_json(
                meta_path,
                {
                    "program_id": "seed-openat",
                    "syscall_list": ["openat"],
                    "full_syscall_list": ["openat"],
                    "call_count": 1,
                },
            )
            dump_jsonl(
                existing_file,
                [
                    {
                        "program_id": "seed-openat",
                        "workflow": "baseline",
                        "normalized_path": str(normalized_path),
                        "meta_path": str(meta_path),
                    }
                ],
            )
            dump_json(
                manifest_path,
                {
                    "categories": {
                        "file-and-directory-operations": {
                            "syscalls": {
                                "openat": {
                                    "name": "openat",
                                    "category": "file-and-directory-operations",
                                    "support_tier": "partial",
                                    "generation_enabled": True,
                                    "defer_reason": None,
                                    "generator_class": "base_only",
                                    "generator_gap_reason": "none",
                                    "syzkaller_base_available": True,
                                    "syzkaller_variant_available": False,
                                    "source_scml_files": [],
                                }
                            }
                        },
                        "file-systems-and-mount-control": {
                            "syscalls": {
                                "mount": {
                                    "name": "mount",
                                    "category": "file-systems-and-mount-control",
                                    "support_tier": "partial",
                                    "generation_enabled": True,
                                    "defer_reason": None,
                                    "generator_class": "unavailable",
                                    "generator_gap_reason": "missing_description",
                                    "syzkaller_base_available": False,
                                    "syzkaller_variant_available": False,
                                    "source_scml_files": [],
                                }
                            }
                        },
                    }
                },
            )
            dump_json(
                profile_path,
                {
                    "generation": {
                        "source_modes": ["existing_corpus"],
                        "jobs": 2,
                        "batch_size": 2,
                        "per_target_budget": 1,
                        "existing_corpus_limit": 2,
                        "existing_corpus_source_file": str(existing_file),
                        "sequence_length": {"min": 1, "max": 4},
                    },
                    "enabled_categories": ["file-and-directory-operations"],
                    "deferred_categories": {
                        "file-systems-and-mount-control": "privileged_or_mount_heavy",
                    },
                    "deferred_syscalls": {},
                },
            )
            dump_json(
                config_path,
                {
                    "workflow": "scmlgen",
                    "target_os": "linux",
                    "arch": "amd64",
                    "compat_manifest_path": str(manifest_path),
                    "generation_profile_path": str(profile_path),
                    "paths": {
                        "temp_dir": str(root / "tmp"),
                        "targets_file": str(targets_file),
                        "generated_file": str(generated_file),
                        "generated_raw_dir": str(root / "generated" / "raw"),
                        "generated_normalized_dir": str(root / "generated" / "normalized"),
                        "generated_meta_dir": str(root / "generated" / "meta"),
                        "reports_dir": str(reports_dir),
                    },
                    "parallel": {
                        "jobs": 2,
                    },
                    "derivation": {
                        "legacy_source_eligible_file": str(existing_file),
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

            configure_runtime(workflow="scmlgen", config_path=str(config_path))
            with patch.object(sys, "argv", ["export_scml_targets.py", "--workflow", "scmlgen"]):
                export_scml_targets.main()
            with patch.object(sys, "argv", ["generate_scml_candidates.py", "--workflow", "scmlgen"]):
                generate_scml_candidates.main()

            target_rows = load_jsonl(targets_file)
            generated_rows = load_jsonl(generated_file)
            summary = load_json(reports_dir / "generation-summary.json")

        self.assertEqual([row["syscall_name"] for row in target_rows], ["openat"])
        self.assertEqual(len(generated_rows), 1)
        self.assertEqual(generated_rows[0]["program_id"], "seed-openat")
        self.assertEqual(summary["targets_with_candidates"], 1)
        self.assertEqual(summary["targets_without_candidates"], 0)

    def test_generate_main_writes_outputs_even_with_generator_failed_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "scml-manifest.json"
            profile_path = root / "generation-profile.json"
            config_path = root / "scml_rules.json"
            reports_dir = root / "reports"
            targets_file = root / "targets.jsonl"
            generated_file = root / "generated.jsonl"

            reports_dir.mkdir(parents=True, exist_ok=True)
            dump_json(manifest_path, {"categories": {}})
            dump_json(
                profile_path,
                {
                    "generation": {
                        "source_modes": ["syz_generate"],
                        "jobs": 1,
                        "batch_size": 1,
                        "per_target_budget": 1,
                        "sequence_length": {"min": 1, "max": 4},
                    }
                },
            )
            dump_jsonl(
                targets_file,
                [
                    {
                        "syscall_name": "openat",
                        "category": "file-and-directory-operations",
                        "support_tier": "partial",
                        "generator_class": "base_only",
                        "generator_gap_reason": "none",
                    }
                ],
            )
            dump_json(
                config_path,
                {
                    "workflow": "scmlgen",
                    "target_os": "linux",
                    "arch": "amd64",
                    "compat_manifest_path": str(manifest_path),
                    "generation_profile_path": str(profile_path),
                    "paths": {
                        "targets_file": str(targets_file),
                        "generated_file": str(generated_file),
                        "generated_raw_dir": str(root / "generated" / "raw"),
                        "generated_normalized_dir": str(root / "generated" / "normalized"),
                        "generated_meta_dir": str(root / "generated" / "meta"),
                        "reports_dir": str(reports_dir),
                    },
                },
            )

            configure_runtime(workflow="scmlgen", config_path=str(config_path))
            with patch(
                "tools.generate_scml_candidates.load_manifest_index",
                return_value={},
            ), patch(
                "tools.generate_scml_candidates.generate_rows_for_target",
                return_value=(
                    [],
                    {
                        "syscall_name": "openat",
                        "generator_class": "base_only",
                        "generator_gap_reason": "none",
                        "source_modes_attempted": ["syz_generate"],
                        "candidate_count": 0,
                        "generator_error": "boom",
                    },
                    {
                        "syscall_name": "openat",
                        "category": "file-and-directory-operations",
                        "support_tier": "partial",
                        "generator_class": "base_only",
                        "generator_gap_reason": "generator_failed",
                        "source_modes_attempted": ["syz_generate"],
                        "generator_error": "boom",
                    },
                ),
            ), patch.object(sys, "argv", ["generate_scml_candidates.py", "--workflow", "scmlgen"]):
                generate_scml_candidates.main()

            self.assertTrue(generated_file.exists())
            summary = load_json(reports_dir / "generation-summary.json")
            self.assertEqual(summary["generator_failed_targets"], 1)


if __name__ == "__main__":
    unittest.main()
