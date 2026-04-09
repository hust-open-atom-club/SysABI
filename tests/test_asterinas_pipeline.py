from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from analyzer.classify import classify_result
from orchestrator.common import config, configure_runtime, runner_profiles
from orchestrator import scheduler
from orchestrator.vm_runner import finalize_process_result
from tools.derive_asterinas_corpus import derive_rejection
from tools.run_asterinas import candidate_status_from_events, compose_autorun, compose_init, host_osdk_env, qemu_log_paths


class AsterinasPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_workflow = os.environ.get("SYZABI_WORKFLOW")
        self.previous_config = os.environ.get("SYZABI_CONFIG_PATH")
        configure_runtime(workflow="asterinas", config_path=None)
        os.environ.pop("SYZABI_CONFIG_PATH", None)

    def tearDown(self) -> None:
        if self.previous_workflow is None:
            os.environ.pop("SYZABI_WORKFLOW", None)
        else:
            os.environ["SYZABI_WORKFLOW"] = self.previous_workflow
        if self.previous_config is None:
            os.environ.pop("SYZABI_CONFIG_PATH", None)
        else:
            os.environ["SYZABI_CONFIG_PATH"] = self.previous_config

    def test_asterinas_config_uses_command_candidate_profile(self) -> None:
        cfg = config()
        self.assertEqual(cfg["workflow"], "asterinas")
        self.assertEqual(cfg["paths"]["eligible_file"], "eligible_programs/asterinas.jsonl")
        self.assertEqual(cfg["parallel"]["jobs"], 4)
        self.assertEqual(runner_profiles()["candidate"]["kind"], "command")
        self.assertEqual(runner_profiles()["candidate"]["binary_name"], "testcase.candidate.bin")
        self.assertEqual(runner_profiles()["candidate"]["controlled_divergence"]["match_syscall"], "openat")

    def test_asterinas_derivation_keeps_exact_full_name_subset(self) -> None:
        cfg = config()
        allowed = {
            "full_syscall_list": ["openat", "read", "close"],
        }
        rejected_variant = {
            "full_syscall_list": ["openat$fuse"],
        }
        self.assertEqual(derive_rejection(allowed, cfg), [])
        self.assertEqual(derive_rejection(rejected_variant, cfg), ["unsupported_variant"])

    def test_asterinas_derivation_uses_stable_rejection_taxonomy(self) -> None:
        cfg = config()
        meta = {
            "full_syscall_list": ["openat", "mmap", "wait4", "socketpair$inet"],
        }
        reasons = derive_rejection(meta, cfg)
        self.assertIn("unsupported_memory_management", reasons)
        self.assertIn("unsupported_process_control", reasons)
        self.assertIn("unsupported_variant", reasons)

    def test_command_runner_result_can_report_unsupported_status(self) -> None:
        status, exit_code, detail, kernel_build = finalize_process_result(
            profile_kind="command",
            completed_returncode=1,
            runner_result={
                "status": "unsupported",
                "exit_code": None,
                "status_detail": "ENOSYS",
                "kernel_build": "asterinas-1234",
            },
            fallback_kernel_build="fallback",
        )
        self.assertEqual(status, "unsupported")
        self.assertIsNone(exit_code)
        self.assertEqual(detail, "ENOSYS")
        self.assertEqual(kernel_build, "asterinas-1234")

    def test_classifier_accepts_explicit_unsupported_status(self) -> None:
        classes = config()["classification"]
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="unsupported",
                comparison=None,
            ),
            classes["unsupported_feature"],
        )
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="unsupported",
                comparison={"equivalent": False, "noise_only": False},
            ),
            classes["unsupported_feature"],
        )

    def test_enosys_events_map_to_unsupported_candidate_status(self) -> None:
        status = candidate_status_from_events(
            [
                {
                    "return_value": -1,
                    "errno": 38,
                }
            ],
            {"status": "ok", "exit_code": 0, "timed_out": False},
        )
        self.assertEqual(status, "unsupported")

    def test_compose_init_uses_explicit_autorun_entrypoint(self) -> None:
        script = compose_init()
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("exec /syzkabi/autorun.sh", script)

    def test_compose_autorun_propagates_injected_trace_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYZABI_INJECT_TRACE_ENABLED": "1",
                "SYZABI_INJECT_TRACE_SYSCALL": "openat",
                "SYZABI_INJECT_TRACE_FIELD": "return",
                "SYZABI_INJECT_TRACE_VALUE": "-5",
            },
            clear=False,
        ):
            script = compose_autorun(32)
        self.assertIn("export SYZABI_INJECT_TRACE_ENABLED=1", script)
        self.assertIn("export SYZABI_INJECT_TRACE_SYSCALL=openat", script)
        self.assertIn("export SYZABI_INJECT_TRACE_FIELD=return", script)
        self.assertIn("export SYZABI_INJECT_TRACE_VALUE=-5", script)

    def test_qemu_logs_are_scoped_to_work_dir(self) -> None:
        work_dir = Path("/tmp/asterinas-run")
        qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
        self.assertEqual(qemu_log_path, work_dir / "qemu.log")
        self.assertEqual(qemu_serial_log_path, work_dir / "qemu-serial.log")

    def test_host_osdk_env_sets_per_run_qemu_log_paths(self) -> None:
        work_dir = Path("/tmp/asterinas-run")
        with patch("tools.run_asterinas.ensure_vdso_dir", return_value=Path("/vdso")), patch(
            "tools.run_asterinas.ensure_local_mtools", return_value=None
        ):
            env = host_osdk_env(work_dir)
        self.assertEqual(env["BOOT_METHOD"], "qemu-direct")
        self.assertEqual(env["NETDEV"], "none")
        self.assertEqual(env["QEMU_LOG_FILE"], str(work_dir / "qemu.log"))
        self.assertEqual(env["QEMU_SERIAL_LOG_FILE"], str(work_dir / "qemu-serial.log"))
        self.assertEqual(env["QEMU_DISPLAY"], "none")
        self.assertEqual(env["RUSTUP_TOOLCHAIN"], "nightly-2025-12-06")

    def test_parallel_scheduler_preserves_input_order(self) -> None:
        entries = [{"program_id": "alpha"}, {"program_id": "beta"}, {"program_id": "gamma"}]

        def fake_schedule_one(entry, args):
            delays = {"alpha": 0.05, "beta": 0.02, "gamma": 0.0}
            time.sleep(delays[entry["program_id"]])
            return {"program_id": entry["program_id"]}

        with patch("orchestrator.scheduler.schedule_one", side_effect=fake_schedule_one):
            results = scheduler.schedule_entries(entries, object(), jobs=3)
        self.assertEqual([result["program_id"] for result in results], ["alpha", "beta", "gamma"])


if __name__ == "__main__":
    unittest.main()
