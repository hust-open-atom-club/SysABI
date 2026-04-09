from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from orchestrator.common import configure_runtime
from tools.render_partial_summary import reconstruct_completed_results
from tools.render_summary import merge_scml_result_counts, render_summary_reports


class SCMLReportingTests(unittest.TestCase):
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

    def test_merge_scml_result_counts_includes_rejected_bucket(self) -> None:
        campaign_results = [
            {"scml_result_bucket": "passed_scml_and_no_diff"},
            {"scml_result_bucket": "passed_scml_but_candidate_failed"},
        ]
        scml_rejections = [
            {"program_id": "a"},
            {"program_id": "b"},
        ]
        self.assertEqual(
            merge_scml_result_counts(campaign_results, scml_rejections),
            {
                "passed_scml_and_no_diff": 1,
                "passed_scml_but_candidate_failed": 1,
                "rejected_by_scml": 2,
            },
        )

    def test_render_summary_reports_writes_summary_and_signoff_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n{"program_id":"case-2"}\n', encoding="utf-8")
            (reports_dir / "campaign-results.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "program_id": "case-1",
                                "classification": "NO_DIFF",
                                "reference_runs": [{"trace_json_path": str(root / "ref1" / "raw-trace.json")}],
                                "candidate_run": {
                                    "status": "ok",
                                    "runner_kind": "qemu",
                                    "kernel_build": "kernel-a",
                                    "trace_json_path": str(root / "cand1" / "raw-trace.json"),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "program_id": "case-2",
                                "classification": "BUG_LIKELY",
                                "reference_runs": [{"trace_json_path": str(root / "ref2" / "raw-trace.json")}],
                                "candidate_run": {
                                    "status": "timeout",
                                    "runner_kind": "qemu",
                                    "kernel_build": "kernel-a",
                                    "trace_json_path": str(root / "cand2" / "raw-trace.json"),
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 2, "total": 2}), encoding="utf-8")
            (reports_dir / "summary.json").write_text(json.dumps({"campaign": "smoke"}), encoding="utf-8")
            for path in [
                root / "ref1" / "canonical-trace.json",
                root / "cand1" / "canonical-trace.json",
            ]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"events": []}), encoding="utf-8")

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "smoke": {
                                "build_success_rate": 0.5,
                                "dual_execution_completion_rate": 0.5,
                                "trace_success_rate": 0.5,
                                "canonical_success_rate": 0.5,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 1,
                                "eligible_program_count_min": 1,
                            },
                            "signoff": {
                                "build_success_rate": 0.5,
                                "dual_execution_completion_rate": 0.5,
                                "trace_success_rate": 0.5,
                                "canonical_success_rate": 0.5,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 1,
                                "eligible_program_count_min": 1,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="smoke")

            self.assertEqual(summary["campaign"], "smoke")
            self.assertEqual(summary["classification_counts"]["BUG_LIKELY"], 1)
            self.assertTrue((reports_dir / "summary.md").exists())
            self.assertTrue((reports_dir / "signoff.md").exists())
            self.assertTrue((reports_dir / "syscall-summary.json").exists())
            self.assertTrue((reports_dir / "syscall-summary.md").exists())
            summary_json = json.loads((reports_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary_json["candidate_runner_kinds"], ["qemu"])
            self.assertEqual(summary_json["candidate_kernel_builds"], ["kernel-a"])

    def test_reconstruct_completed_results_defaults_missing_scml_status_to_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            eligible_file = root / "eligible.jsonl"
            artifacts_dir = root / "artifacts"
            program_dir = artifacts_dir / "case-1"
            ref_dir = program_dir / "123-ref0" / "reference"
            cand_dir = program_dir / "123-candidate0" / "candidate"
            ref_dir.mkdir(parents=True, exist_ok=True)
            cand_dir.mkdir(parents=True, exist_ok=True)

            eligible_file.write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "normalized_path": str(root / "case-1.syz"),
                        "meta_path": str(root / "case-1.json"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "case-1.syz").write_text("openat(0x0)\n", encoding="utf-8")
            (root / "case-1.json").write_text("{}", encoding="utf-8")

            ref_trace = ref_dir / "raw-trace.json"
            cand_trace = cand_dir / "raw-trace.json"
            ref_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            cand_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            canonical_payload = {
                "program_id": "case-1",
                "side": "reference",
                "event_count": 0,
                "events": [],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            }
            ref_trace.with_name("canonical-trace.json").write_text(json.dumps(canonical_payload), encoding="utf-8")
            cand_trace.with_name("canonical-trace.json").write_text(
                json.dumps({**canonical_payload, "side": "candidate"}),
                encoding="utf-8",
            )
            (ref_dir / "run-result.json").write_text(
                json.dumps({"status": "ok", "trace_json_path": str(ref_trace)}),
                encoding="utf-8",
            )
            (cand_dir / "run-result.json").write_text(
                json.dumps({"status": "ok", "trace_json_path": str(cand_trace)}),
                encoding="utf-8",
            )

            cfg = {
                "workflow": "asterinas",
                "paths": {
                    "eligible_file": str(eligible_file),
                    "artifacts_dir": str(artifacts_dir),
                },
                "classification": {
                    "no_diff": "NO_DIFF",
                    "baseline_invalid": "BASELINE_INVALID",
                    "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                    "unsupported_feature": "UNSUPPORTED_FEATURE",
                    "bug_likely": "BUG_LIKELY",
                },
            }

            results = reconstruct_completed_results(cfg)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["scml_preflight_status"], "not_run")
        self.assertEqual(results[0]["scml_result_bucket"], "")

    def test_render_summary_reports_derives_build_success_when_build_summary_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n{"program_id":"case-2"}\n', encoding="utf-8")
            (reports_dir / "campaign-results.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "program_id": "case-1",
                                "classification": "NO_DIFF",
                                "reference_runs": [{"trace_json_path": str(root / "ref1" / "raw-trace.json")}],
                                "candidate_run": {
                                    "status": "ok",
                                    "runner_kind": "qemu",
                                    "kernel_build": "kernel-a",
                                    "trace_json_path": str(root / "cand1" / "raw-trace.json"),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "program_id": "case-2",
                                "classification": "build_failure",
                                "build_result_path": str(root / "build" / "case-2" / "build-result.json"),
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            for path in [
                root / "ref1" / "canonical-trace.json",
                root / "cand1" / "canonical-trace.json",
            ]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"events": []}), encoding="utf-8")

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "smoke": {
                                "build_success_rate": 0.5,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.5,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="smoke")

            self.assertEqual(summary["build_success_rate"], 0.5)
            self.assertTrue(summary["signoff_pass"])

    def test_render_summary_reports_groups_problem_cases_by_syscall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text(
                "\n".join(
                    [
                        '{"program_id":"case-ok"}',
                        '{"program_id":"case-open-1"}',
                        '{"program_id":"case-open-2"}',
                        '{"program_id":"case-unknown"}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def write_trace_pair(name: str, return_value: int) -> tuple[Path, Path]:
                ref_trace = root / f"{name}-ref" / "raw-trace.json"
                cand_trace = root / f"{name}-cand" / "raw-trace.json"
                ref_trace.parent.mkdir(parents=True, exist_ok=True)
                cand_trace.parent.mkdir(parents=True, exist_ok=True)
                ref_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
                cand_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
                ref_trace.with_name("canonical-trace.json").write_text(
                    json.dumps(
                        {
                            "events": [
                                {
                                    "index": 0,
                                    "syscall_name": "openat",
                                    "return_value": 0,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                cand_trace.with_name("canonical-trace.json").write_text(
                    json.dumps(
                        {
                            "events": [
                                {
                                    "index": 0,
                                    "syscall_name": "openat",
                                    "return_value": return_value,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return ref_trace, cand_trace

            ok_ref, ok_cand = write_trace_pair("ok", 0)
            open_ref_1, open_cand_1 = write_trace_pair("open-1", -1)
            open_ref_2, open_cand_2 = write_trace_pair("open-2", -2)

            (reports_dir / "campaign-results.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "program_id": "case-ok",
                                "classification": "NO_DIFF",
                                "normalized_path": str(root / "case-ok.syz"),
                                "comparison": {
                                    "first_divergence_index": None,
                                    "final_state_equal": True,
                                    "process_exit_equal": True,
                                },
                                "reference_runs": [{"status": "ok", "trace_json_path": str(ok_ref)}],
                                "candidate_run": {"status": "ok", "trace_json_path": str(ok_cand)},
                            }
                        ),
                        json.dumps(
                            {
                                "program_id": "case-open-1",
                                "classification": "BUG_LIKELY",
                                "normalized_path": str(root / "case-open-1.syz"),
                                "comparison": {
                                    "first_divergence_index": 0,
                                    "final_state_equal": True,
                                    "process_exit_equal": True,
                                },
                                "reference_runs": [{"status": "ok", "trace_json_path": str(open_ref_1)}],
                                "candidate_run": {
                                    "status": "timeout",
                                    "trace_json_path": str(open_cand_1),
                                },
                                "scml_result_bucket": "passed_scml_and_diverged",
                            }
                        ),
                        json.dumps(
                            {
                                "program_id": "case-open-2",
                                "classification": "WEAK_SPEC_OR_ENV_NOISE",
                                "normalized_path": str(root / "case-open-2.syz"),
                                "comparison": {
                                    "first_divergence_index": 0,
                                    "final_state_equal": True,
                                    "process_exit_equal": True,
                                },
                                "reference_runs": [{"status": "ok", "trace_json_path": str(open_ref_2)}],
                                "candidate_run": {
                                    "status": "crash",
                                    "trace_json_path": str(open_cand_2),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "program_id": "case-unknown",
                                "classification": "UNSUPPORTED_FEATURE",
                                "normalized_path": str(root / "case-unknown.syz"),
                                "reference_runs": [{"status": "ok"}],
                                "candidate_run": {"status": "infra_error"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 3, "total": 4}), encoding="utf-8")

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "asterinas_scml",
                        "target": "asterinas",
                        "capabilities": {"supports_preflight": True},
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "full": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "smoke": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="asterinas_scml", config_path=str(config_path))
            render_summary_reports(workflow="asterinas_scml", config_path=str(config_path), campaign="full")

            syscall_summary = json.loads((reports_dir / "syscall-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(syscall_summary["campaign"], "full")
            self.assertEqual(syscall_summary["total_problem_cases"], 3)
            self.assertEqual(syscall_summary["syscall_bucket_count"], 2)
            self.assertEqual(syscall_summary["reference_label"], "Linux")
            self.assertEqual(syscall_summary["candidate_label"], "Asterinas")
            self.assertEqual(
                [row["syscall_name"] for row in syscall_summary["syscalls"]],
                ["openat", "unknown"],
            )
            openat_cases = syscall_summary["syscalls"][0]["cases"]
            self.assertEqual([case["program_id"] for case in openat_cases], ["case-open-1", "case-open-2"])
            self.assertEqual(openat_cases[0]["comparison_reason"], "syscall_result_mismatch(return_value)")
            self.assertIn("openat(args=", openat_cases[0]["reference_result"])
            self.assertIn("ret=0", openat_cases[0]["reference_result"])
            self.assertIn("ret=-1", openat_cases[0]["candidate_result"])
            self.assertEqual(syscall_summary["syscalls"][1]["cases"][0]["program_id"], "case-unknown")
            self.assertEqual(syscall_summary["syscalls"][1]["cases"][0]["candidate_status"], "infra_error")
            self.assertEqual(syscall_summary["syscalls"][1]["cases"][0]["comparison_reason"], "unknown")

            syscall_md = (reports_dir / "syscall-summary.md").read_text(encoding="utf-8")
            self.assertIn("## openat", syscall_md)
            self.assertIn("case-open-1", syscall_md)
            self.assertIn("Linux: openat", syscall_md)
            self.assertIn("Asterinas: openat", syscall_md)
            self.assertIn("## unknown", syscall_md)

    def test_render_summary_uses_latest_reference_rerun_for_rate_calculations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n', encoding="utf-8")

            ref0_trace = root / "ref0" / "raw-trace.json"
            ref1_trace = root / "ref1" / "raw-trace.json"
            cand_trace = root / "cand" / "raw-trace.json"
            for path in (ref0_trace, ref1_trace, cand_trace):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"events": []}), encoding="utf-8")
            ref1_trace.with_name("canonical-trace.json").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "side": "reference",
                        "event_count": 0,
                        "events": [],
                        "final_state": {"files": []},
                        "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
                    }
                ),
                encoding="utf-8",
            )
            cand_trace.with_name("canonical-trace.json").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "side": "candidate",
                        "event_count": 0,
                        "events": [],
                        "final_state": {"files": []},
                        "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
                    }
                ),
                encoding="utf-8",
            )

            (reports_dir / "campaign-results.jsonl").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "classification": "NO_DIFF",
                        "reference_runs": [
                            {"status": "crash", "trace_json_path": str(ref0_trace)},
                            {"status": "ok", "trace_json_path": str(ref1_trace)},
                        ],
                        "candidate_run": {
                            "status": "ok",
                            "runner_kind": "qemu",
                            "kernel_build": "kernel-a",
                            "trace_json_path": str(cand_trace),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 1, "total": 1}), encoding="utf-8")

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "full": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "smoke": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="full")

        self.assertEqual(summary["dual_execution_completion_rate"], 1.0)
        self.assertEqual(summary["trace_generation_success_rate"], 1.0)
        self.assertEqual(summary["canonicalization_success_rate"], 1.0)

    def test_render_summary_counts_crash_rows_with_traces_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n', encoding="utf-8")
            ref_trace = root / "ref" / "raw-trace.json"
            cand_trace = root / "cand" / "raw-trace.json"
            ref_trace.parent.mkdir(parents=True, exist_ok=True)
            cand_trace.parent.mkdir(parents=True, exist_ok=True)
            ref_trace.write_text(json.dumps({"events": [], "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False}}), encoding="utf-8")
            cand_trace.write_text(json.dumps({"events": [], "process_exit": {"status": "crash", "exit_code": 132, "timed_out": False}}), encoding="utf-8")
            ref_trace.with_name("canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            cand_trace.with_name("canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")

            (reports_dir / "campaign-results.jsonl").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "classification": "BUG_LIKELY",
                        "scml_result_bucket": "passed_scml_and_diverged",
                        "reference_runs": [{"trace_json_path": str(ref_trace)}],
                        "candidate_run": {
                            "status": "crash",
                            "runner_kind": "qemu",
                            "kernel_build": "kernel-a",
                            "trace_json_path": str(cand_trace),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 1, "total": 1}), encoding="utf-8")
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "full": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "smoke": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="full")

            self.assertEqual(summary["dual_execution_completion_rate"], 1.0)
            self.assertEqual(summary["trace_generation_success_rate"], 1.0)
            self.assertEqual(summary["canonicalization_success_rate"], 1.0)

    def test_render_summary_ignores_generation_metrics_when_derivation_is_baseline_driven(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            generated_file = root / "generated.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n', encoding="utf-8")
            generated_file.write_text('{"program_id":"generated-1"}\n', encoding="utf-8")
            (reports_dir / "campaign-results.jsonl").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "classification": "NO_DIFF",
                        "reference_runs": [{"trace_json_path": str(root / "ref" / "raw-trace.json")}],
                        "candidate_run": {
                            "status": "ok",
                            "runner_kind": "qemu",
                            "kernel_build": "kernel-a",
                            "trace_json_path": str(root / "cand" / "raw-trace.json"),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 1, "total": 1}), encoding="utf-8")
            (root / "ref").mkdir(parents=True, exist_ok=True)
            (root / "cand").mkdir(parents=True, exist_ok=True)
            (root / "ref" / "raw-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (root / "cand" / "raw-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (root / "ref" / "canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (root / "cand" / "canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (reports_dir / "generation-summary.json").write_text(
                json.dumps(
                    {
                        "profile_enabled_total": 1,
                        "targets_with_candidates": 1,
                        "targets_without_candidates": 0,
                        "unique_candidate_count": 4,
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                            "generated_file": str(generated_file),
                        },
                        "derivation": {
                            "source_eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "full": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "smoke": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="full")

            self.assertNotIn("profile_enabled_total", summary)
            self.assertNotIn("targets_with_candidates", summary)
            self.assertNotIn("generation_candidate_count", summary)

    def test_render_summary_includes_generation_metrics_when_generated_source_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            generated_file = root / "generated.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n', encoding="utf-8")
            generated_file.write_text('{"program_id":"generated-1"}\n', encoding="utf-8")
            (reports_dir / "campaign-results.jsonl").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "classification": "NO_DIFF",
                        "reference_runs": [{"trace_json_path": str(root / "ref" / "raw-trace.json")}],
                        "candidate_run": {
                            "status": "ok",
                            "runner_kind": "qemu",
                            "kernel_build": "kernel-a",
                            "trace_json_path": str(root / "cand" / "raw-trace.json"),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 1, "total": 1}), encoding="utf-8")
            (root / "ref").mkdir(parents=True, exist_ok=True)
            (root / "cand").mkdir(parents=True, exist_ok=True)
            (root / "ref" / "raw-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (root / "cand" / "raw-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (root / "ref" / "canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (root / "cand" / "canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (reports_dir / "generation-summary.json").write_text(
                json.dumps(
                    {
                        "profile_enabled_total": 3,
                        "targets_with_candidates": 2,
                        "targets_without_candidates": 1,
                        "unique_candidate_count": 7,
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                            "generated_file": str(generated_file),
                        },
                        "derivation": {
                            "source_eligible_file": str(eligible_file),
                            "generated_source_eligible_file": str(generated_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "full": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "smoke": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="full")

            self.assertEqual(summary["profile_enabled_total"], 3)
            self.assertEqual(summary["targets_with_candidates"], 2)
            self.assertEqual(summary["targets_without_candidates"], 1)
            self.assertEqual(summary["generation_candidate_count"], 7)

    def test_render_summary_uses_runtime_preflight_ledger_not_derivation_rejections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            reports_dir.mkdir(parents=True, exist_ok=True)
            eligible_file.write_text('{"program_id":"case-1"}\n', encoding="utf-8")
            ref_trace = root / "ref" / "raw-trace.json"
            cand_trace = root / "cand" / "raw-trace.json"
            ref_trace.parent.mkdir(parents=True, exist_ok=True)
            cand_trace.parent.mkdir(parents=True, exist_ok=True)
            ref_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            cand_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            ref_trace.with_name("canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            cand_trace.with_name("canonical-trace.json").write_text(json.dumps({"events": []}), encoding="utf-8")
            (reports_dir / "campaign-results.jsonl").write_text(
                json.dumps(
                    {
                        "program_id": "case-1",
                        "classification": "NO_DIFF",
                        "reference_runs": [{"trace_json_path": str(ref_trace)}],
                        "candidate_run": {
                            "status": "ok",
                            "runner_kind": "qemu",
                            "kernel_build": "kernel-a",
                            "trace_json_path": str(cand_trace),
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 1, "total": 1}), encoding="utf-8")
            (reports_dir / "scml-rejections.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"program_id": "runtime-1"}),
                        json.dumps({"program_id": "runtime-2"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "derivation-rejections.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"program_id": "derive-1"}),
                        json.dumps({"program_id": "derive-2"}),
                        json.dumps({"program_id": "derive-3"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (reports_dir / "preflight-summary.json").write_text(
                json.dumps({"source_total": 3, "eligible": 1, "rejected": 2}),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "reporting",
                        "paths": {
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                        },
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {
                            "full": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "signoff": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                            "smoke": {
                                "build_success_rate": 0.0,
                                "dual_execution_completion_rate": 0.0,
                                "trace_success_rate": 0.0,
                                "canonical_success_rate": 0.0,
                                "baseline_invalid_rate": 1.0,
                                "total_min": 0,
                                "eligible_program_count_min": 0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )

            configure_runtime(workflow="reporting", config_path=str(config_path))
            summary = render_summary_reports(workflow="reporting", config_path=str(config_path), campaign="full")

            self.assertEqual(summary["scml_rejected_count"], 2)
            self.assertEqual(summary["scml_result_counts"]["rejected_by_scml"], 2)
            self.assertEqual(summary["scml_preflight_pass_rate"], 1 / 3)
            signoff = (reports_dir / "signoff.md").read_text(encoding="utf-8")
            self.assertIn("- scml_rejected_count: 2", signoff)


if __name__ == "__main__":
    unittest.main()
