from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools import reduce_case
from tools.reduce_case import find_campaign_package_context, greedy_reduce, package_binary_matches_current_build, scml_reduction_invariants_hold, select_scml_campaign_row


class SCMLReduceCaseTests(unittest.TestCase):
    class FakeRun(SimpleNamespace):
        def to_dict(self) -> dict[str, object]:
            return {
                "status": self.status,
                "trace_json_path": self.trace_json_path,
                "external_state_path": self.external_state_path,
                "console_log_path": getattr(self, "console_log_path", ""),
            }

    def test_select_scml_campaign_row_requires_diverged_bucket_for_program_id(self) -> None:
        rows = [
            {
                "program_id": "no-diff",
                "scml_preflight_status": "passed",
                "scml_result_bucket": "passed_scml_and_no_diff",
            }
        ]
        with self.assertRaises(SystemExit):
            select_scml_campaign_row(rows, program_id="no-diff")

    def test_select_scml_campaign_row_accepts_passed_diverged_case(self) -> None:
        row = {
            "program_id": "diverged",
            "scml_preflight_status": "passed",
            "scml_result_bucket": "passed_scml_and_diverged",
        }
        self.assertEqual(select_scml_campaign_row([row]), row)

    def test_scml_reduction_invariants_require_non_runtime_divergence_index(self) -> None:
        comparison = {"equivalent": False, "first_divergence_index": 1}
        reference_canonical = {
            "events": [
                {"index": 0, "syscall_name": "mmap"},
                {"index": 1, "syscall_name": "exit_group"},
            ]
        }
        self.assertTrue(scml_reduction_invariants_hold(comparison, reference_canonical, "passed"))

    def test_scml_reduction_invariants_reject_missing_program_syscall_index(self) -> None:
        comparison = {"equivalent": False, "first_divergence_index": 0}
        reference_canonical = {
            "events": [
                {"index": 0, "syscall_name": "mmap"},
                {"index": 1, "syscall_name": "exit_group"},
            ]
        }
        self.assertFalse(scml_reduction_invariants_hold(comparison, reference_canonical, "passed"))
        self.assertFalse(scml_reduction_invariants_hold(comparison, reference_canonical, "rejected_by_scml"))

    def test_run_case_uses_packaged_candidate_path_for_asterinas_scml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            program_path.write_text("openat(0, 0, 0)\n", encoding="utf-8")
            reference_trace = root / "reference-trace.json"
            reference_state = root / "reference-state.json"
            reference_triage_trace = root / "reference-triage-trace.json"
            reference_triage_state = root / "reference-triage-state.json"
            candidate_trace = root / "candidate-trace.json"
            candidate_state = root / "candidate-state.json"
            candidate_triage_trace = root / "candidate-triage-trace.json"
            candidate_triage_state = root / "candidate-triage-state.json"
            for path, payload in (
                (reference_trace, {"events": []}),
                (reference_state, {"files": []}),
                (reference_triage_trace, {"events": []}),
                (reference_triage_state, {"files": []}),
                (candidate_trace, {"events": []}),
                (candidate_state, {"files": []}),
                (candidate_triage_trace, {"events": []}),
                (candidate_triage_state, {"files": []}),
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")

            reference_run = self.FakeRun(
                status="ok",
                trace_json_path=str(reference_trace),
                external_state_path=str(reference_state),
                console_log_path=str(root / "reference-console.log"),
            )
            candidate_run = self.FakeRun(
                status="ok",
                trace_json_path=str(candidate_trace),
                external_state_path=str(candidate_state),
                console_log_path=str(root / "candidate-console.log"),
            )
            reference_triage_run = self.FakeRun(
                status="ok",
                trace_json_path=str(reference_triage_trace),
                external_state_path=str(reference_triage_state),
                console_log_path=str(root / "reference-triage-console.log"),
            )
            candidate_triage_run = self.FakeRun(
                status="ok",
                trace_json_path=str(candidate_triage_trace),
                external_state_path=str(candidate_triage_state),
                console_log_path=str(root / "candidate-triage-console.log"),
            )

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 1},
                    "normalization": {"runtime_syscalls": []},
                },
            ), patch(
                "tools.reduce_case.inspect_program",
                return_value={"program_id": "diverged", "call_count": 1},
            ), patch(
                "tools.reduce_case.build_one"
            ), patch(
                "tools.reduce_case.execute_side",
                side_effect=[reference_run, reference_triage_run],
            ) as execute_side, patch(
                "tools.reduce_case.execute_candidate_batch_with_context",
                return_value=({"diverged": candidate_run}, Path("/tmp/package"), {"diverged": 0}),
            ) as execute_candidate_batch_with_context, patch(
                "tools.reduce_case.execute_candidate_case_in_package",
                return_value=candidate_triage_run,
            ) as execute_candidate_case_in_package, patch(
                "tools.reduce_case.canonicalize",
                side_effect=lambda raw, external: {"events": raw.get("events", [])},
            ), patch(
                "tools.reduce_case.compare_canonical",
                side_effect=[
                    {"equivalent": False, "first_divergence_index": 0},
                    {"equivalent": True, "first_divergence_index": None},
                ],
            ), patch(
                "tools.reduce_case.dump_json"
            ):
                info, comparison, runs = reduce_case.run_case(program_path)

        self.assertEqual(info["program_id"], "diverged")
        self.assertTrue(comparison["equivalent"])
        self.assertEqual(runs["candidate"]["status"], "ok")
        self.assertEqual(execute_side.call_count, 2)
        self.assertEqual(execute_side.call_args_list[0].kwargs["side"], "reference")
        execute_candidate_batch_with_context.assert_called_once()
        execute_candidate_case_in_package.assert_called_once()
        batch_case = execute_candidate_batch_with_context.call_args.kwargs["batch_cases"][0]
        self.assertEqual(batch_case["program_id"], "diverged")
        self.assertTrue(batch_case["run_id"].endswith("-candidate"))

    def test_run_case_prefers_campaign_package_context_for_source_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            program_path.write_text("openat(0, 0, 0)\n", encoding="utf-8")
            campaign_package_dir = root / "campaign-package"
            campaign_package_dir.mkdir()
            candidate_binary = root / "testcase.candidate.bin"
            candidate_binary.write_bytes(b"candidate-binary")
            candidate_sha = __import__("hashlib").sha256(candidate_binary.read_bytes()).hexdigest()
            (campaign_package_dir / "package-manifest.json").write_text(
                json.dumps({"cases": [{"slot": 7, "binary_sha256": candidate_sha}]}),
                encoding="utf-8",
            )
            reference_trace = root / "reference-trace.json"
            reference_state = root / "reference-state.json"
            candidate_trace = root / "candidate-trace.json"
            candidate_state = root / "candidate-state.json"
            for path, payload in (
                (reference_trace, {"events": []}),
                (reference_state, {"files": []}),
                (candidate_trace, {"events": []}),
                (candidate_state, {"files": []}),
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")

            reference_run = self.FakeRun(
                status="ok",
                trace_json_path=str(reference_trace),
                external_state_path=str(reference_state),
                console_log_path=str(root / "reference-console.log"),
            )
            candidate_run = self.FakeRun(
                status="ok",
                trace_json_path=str(candidate_trace),
                external_state_path=str(candidate_state),
                console_log_path=str(root / "candidate-console.log"),
            )

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "normalization": {"runtime_syscalls": []},
                },
            ), patch(
                "tools.reduce_case.inspect_program",
                return_value={"program_id": "diverged", "call_count": 1},
            ), patch(
                "tools.reduce_case.build_one",
                return_value={"candidate_testcase_bin": str(candidate_binary)},
            ), patch(
                "tools.reduce_case.execute_side",
                return_value=reference_run,
            ), patch(
                "tools.reduce_case.execute_candidate_case_in_package",
                return_value=candidate_run,
            ) as execute_candidate_case_in_package, patch(
                "tools.reduce_case.execute_candidate_batch_with_context",
                side_effect=AssertionError("unexpected single-case package creation"),
            ), patch(
                "tools.reduce_case.canonicalize",
                side_effect=lambda raw, external: {"events": raw.get("events", [])},
            ), patch(
                "tools.reduce_case.compare_canonical",
                return_value={"equivalent": True, "first_divergence_index": None},
            ), patch(
                "tools.reduce_case.dump_json"
            ):
                reduce_case.run_case(
                    program_path,
                    campaign_package_dir=campaign_package_dir,
                    campaign_package_slot=7,
                )

        execute_candidate_case_in_package.assert_called_once()
        self.assertEqual(execute_candidate_case_in_package.call_args.kwargs["package_dir"], campaign_package_dir)
        self.assertEqual(execute_candidate_case_in_package.call_args.kwargs["slot"], 7)

    def test_package_binary_matches_current_build_validates_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package_dir = root / "package"
            package_dir.mkdir()
            candidate_binary = root / "testcase.candidate.bin"
            candidate_binary.write_bytes(b"current-binary")
            expected_sha = __import__("hashlib").sha256(b"current-binary").hexdigest()
            (package_dir / "package-manifest.json").write_text(
                json.dumps(
                    {
                        "cases": [
                            {"slot": 3, "binary_sha256": expected_sha},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(
                package_binary_matches_current_build(
                    package_dir,
                    3,
                    {"candidate_testcase_bin": str(candidate_binary)},
                )
            )
            self.assertFalse(
                package_binary_matches_current_build(
                    package_dir,
                    2,
                    {"candidate_testcase_bin": str(candidate_binary)},
                )
            )

    def test_find_campaign_package_context_prefers_larger_campaign_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            campaign_dir = root / "campaign"
            single_dir = root / "single"
            campaign_dir.mkdir()
            single_dir.mkdir()
            (campaign_dir / "package-manifest.json").write_text(
                json.dumps(
                    {
                        "workflow": "asterinas_scml",
                        "cases": [
                            {"program_id": "target", "slot": 5},
                            {"program_id": "other", "slot": 6},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (single_dir / "package-manifest.json").write_text(
                json.dumps({"workflow": "asterinas", "cases": [{"program_id": "target", "slot": 0}]}),
                encoding="utf-8",
            )
            with patch("tools.reduce_case.Path.exists", return_value=True), patch(
                "tools.reduce_case.Path.iterdir",
                return_value=[single_dir, campaign_dir],
            ):
                context = find_campaign_package_context("target", workflow="asterinas_scml")

        self.assertIsNotNone(context)
        self.assertEqual(context["slot"], 5)
        self.assertEqual(Path(context["package_dir"]), campaign_dir.resolve())

    def test_seed_program_prefers_campaign_row_package_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            eligible_file = root / "eligible.jsonl"
            campaign_results = root / "campaign-results.jsonl"
            normalized = root / "program.syz"
            meta = root / "program.json"
            normalized.write_text("openat(0x0)\n", encoding="utf-8")
            meta.write_text("{}", encoding="utf-8")
            eligible_file.write_text(
                json.dumps(
                    {
                        "program_id": "target",
                        "normalized_path": str(normalized),
                        "meta_path": str(meta),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            campaign_results.write_text(
                json.dumps(
                    {
                        "program_id": "target",
                        "scml_preflight_status": "passed",
                        "scml_result_bucket": "passed_scml_and_diverged",
                        "candidate_package_dir": "/tmp/package",
                        "candidate_package_slot": 9,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "paths": {"eligible_file": str(eligible_file)},
                },
            ), patch(
                "tools.reduce_case.report_path",
                return_value=campaign_results,
            ), patch(
                "tools.reduce_case.find_campaign_package_context",
                side_effect=AssertionError("unexpected fallback lookup"),
            ):
                program_path, row = reduce_case.seed_program("fixture", program_id="target")

        self.assertEqual(program_path, normalized)
        self.assertEqual(row["campaign_package_dir"], "/tmp/package")
        self.assertEqual(row["campaign_package_slot"], 9)

    def test_seed_program_uses_historical_campaign_row_when_current_eligible_corpus_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            eligible_file = root / "eligible.jsonl"
            campaign_results = root / "campaign-results.jsonl"
            historical_program = root / "historical.syz"
            historical_meta = root / "historical.json"
            historical_program.write_text("openat(0x0)\n", encoding="utf-8")
            historical_meta.write_text("{}", encoding="utf-8")
            eligible_file.write_text(
                json.dumps(
                    {
                        "program_id": "other",
                        "normalized_path": str(root / "other.syz"),
                        "meta_path": str(root / "other.json"),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            campaign_results.write_text(
                json.dumps(
                    {
                        "program_id": "target",
                        "normalized_path": str(historical_program),
                        "meta_path": str(historical_meta),
                        "scml_preflight_status": "passed",
                        "scml_result_bucket": "passed_scml_and_diverged",
                        "candidate_package_dir": "/tmp/package",
                        "candidate_package_slot": 4,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "paths": {"eligible_file": str(eligible_file)},
                },
            ), patch(
                "tools.reduce_case.report_path",
                return_value=campaign_results,
            ), patch(
                "tools.reduce_case.find_campaign_package_context",
                side_effect=AssertionError("unexpected fallback lookup"),
            ):
                program_path, row = reduce_case.seed_program("fixture", program_id="target")

        self.assertEqual(program_path, historical_program)
        self.assertEqual(row["meta_path"], str(historical_meta))
        self.assertEqual(row["campaign_package_dir"], "/tmp/package")
        self.assertEqual(row["campaign_package_slot"], 4)

    def test_seed_program_honors_program_id_before_default_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fixture_root = root / "tests" / "fixtures" / "corpus"
            fixture_root.mkdir(parents=True, exist_ok=True)
            fixture_path = fixture_root / "controlled_divergence.syz"
            fixture_path.write_text("fixture\n", encoding="utf-8")
            eligible_file = root / "eligible.jsonl"
            target_program = root / "target.syz"
            target_meta = root / "target.json"
            target_program.write_text("program-id\n", encoding="utf-8")
            target_meta.write_text("{}", encoding="utf-8")
            eligible_file.write_text(
                json.dumps(
                    {
                        "program_id": "target",
                        "normalized_path": str(target_program),
                        "meta_path": str(target_meta),
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "baseline",
                    "paths": {"eligible_file": str(eligible_file)},
                },
            ):
                previous_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    program_path, row = reduce_case.seed_program("controlled_divergence", program_id="target")
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(program_path, target_program)
        self.assertEqual(row["program_id"], "target")

    def test_run_case_rebuilds_package_when_historical_binary_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            program_path.write_text("openat(0, 0, 0)\n", encoding="utf-8")
            package_dir = root / "campaign-package"
            package_dir.mkdir()
            (package_dir / "package-manifest.json").write_text(
                json.dumps({"cases": [{"slot": 7, "binary_sha256": "stale"}]}),
                encoding="utf-8",
            )
            candidate_binary = root / "testcase.candidate.bin"
            candidate_binary.write_bytes(b"fresh-binary")

            reference_trace = root / "reference-trace.json"
            reference_state = root / "reference-state.json"
            candidate_trace = root / "candidate-trace.json"
            candidate_state = root / "candidate-state.json"
            for path, payload in (
                (reference_trace, {"events": []}),
                (reference_state, {"files": []}),
                (candidate_trace, {"events": []}),
                (candidate_state, {"files": []}),
            ):
                path.write_text(json.dumps(payload), encoding="utf-8")

            reference_run = self.FakeRun(
                status="ok",
                trace_json_path=str(reference_trace),
                external_state_path=str(reference_state),
                console_log_path=str(root / "reference-console.log"),
            )
            candidate_run = self.FakeRun(
                status="ok",
                trace_json_path=str(candidate_trace),
                external_state_path=str(candidate_state),
                console_log_path=str(root / "candidate-console.log"),
            )

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "normalization": {"runtime_syscalls": []},
                },
            ), patch(
                "tools.reduce_case.inspect_program",
                return_value={"program_id": "diverged", "call_count": 1},
            ), patch(
                "tools.reduce_case.build_one",
                return_value={"candidate_testcase_bin": str(candidate_binary)},
            ), patch(
                "tools.reduce_case.execute_side",
                return_value=reference_run,
            ), patch(
                "tools.reduce_case.execute_candidate_batch_with_context",
                return_value=({"diverged": candidate_run}, Path("/tmp/package"), {"diverged": 0}),
            ) as execute_candidate_batch_with_context, patch(
                "tools.reduce_case.execute_candidate_case_in_package",
                side_effect=AssertionError("stale packaged binary should not be reused"),
            ), patch(
                "tools.reduce_case.canonicalize",
                side_effect=lambda raw, external: {"events": raw.get("events", [])},
            ), patch(
                "tools.reduce_case.compare_canonical",
                return_value={"equivalent": True, "first_divergence_index": None},
            ), patch(
                "tools.reduce_case.dump_json"
            ):
                reduce_case.run_case(
                    program_path,
                    campaign_package_dir=package_dir,
                    campaign_package_slot=7,
                )

        execute_candidate_batch_with_context.assert_called_once()

    def test_greedy_reduce_attempts_fresh_replay_before_recovery_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            program_path.write_text("openat(0x0)\nclose(0x0)\n", encoding="utf-8")

            ref_trace = root / "reference" / "raw-trace.json"
            cand_trace = root / "candidate" / "raw-trace.json"
            ref_trace.parent.mkdir(parents=True, exist_ok=True)
            cand_trace.parent.mkdir(parents=True, exist_ok=True)
            ref_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            cand_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            ref_trace.with_name("canonical-trace.json").write_text(
                json.dumps({"events": [{"index": 0, "syscall_name": "openat"}]}),
                encoding="utf-8",
            )
            cand_trace.with_name("canonical-trace.json").write_text(
                json.dumps({"events": [{"index": 0, "syscall_name": "openat"}]}),
                encoding="utf-8",
            )

            source_entry = {
                "program_id": "target",
                "comparison": {"equivalent": False, "first_divergence_index": 0},
                "reference_runs": [
                    {
                        "trace_json_path": str(ref_trace),
                        "console_log_path": str(root / "reference-console.log"),
                    }
                ],
                "candidate_run": {
                    "trace_json_path": str(cand_trace),
                    "console_log_path": str(root / "candidate-console.log"),
                },
                "scml_preflight_status": "passed",
                "scml_rejection_reasons": [],
                "scml_trace_log_path": str(root / "preflight.strace.log"),
                "scml_sctrace_output_path": str(root / "preflight.sctrace.txt"),
                "campaign_package_dir": "/tmp/source-package",
                "campaign_package_slot": 9,
            }

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "normalization": {"runtime_syscalls": ["mmap"]},
                },
            ), patch(
                "tools.reduce_case.run_case",
                side_effect=SystemExit("docker unavailable"),
            ) as run_case, patch(
                "tools.reduce_case.inspect_program",
                return_value={"program_id": "target", "call_count": 2},
            ), patch(
                "tools.reduce_case.report_path",
                side_effect=lambda *parts, **_kwargs: root / parts[0],
            ):
                minimized_path, info, comparison, runs, minimized_preflight = greedy_reduce(
                    program_path,
                    source_entry=source_entry,
                )
                minimized_text = minimized_path.read_text(encoding="utf-8")

            run_case.assert_called_once()
            self.assertEqual(run_case.call_args.kwargs["campaign_package_dir"], Path("/tmp/source-package"))
            self.assertEqual(run_case.call_args.kwargs["campaign_package_slot"], 9)
            self.assertEqual(minimized_text, "openat(0x0)\nclose(0x0)\n")
            self.assertEqual(info["program_id"], "target")
            self.assertEqual(comparison["first_divergence_index"], 0)
            self.assertEqual(runs["candidate"]["trace_json_path"], str(cand_trace))
            self.assertEqual(minimized_preflight["status"], "passed")
            self.assertEqual(minimized_preflight["reducer_replay_mode"], "recovery_only")
            self.assertIn("fresh replay failed before reduction", minimized_preflight["reducer_replay_recovery_reason"])

    def test_greedy_reduce_keeps_fresh_replay_evidence_for_source_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            program_path.write_text("openat(0x0)\n", encoding="utf-8")
            fresh_ref_trace = root / "fresh-reference" / "raw-trace.json"
            fresh_cand_trace = root / "fresh-candidate" / "raw-trace.json"
            fresh_ref_trace.parent.mkdir(parents=True, exist_ok=True)
            fresh_cand_trace.parent.mkdir(parents=True, exist_ok=True)
            fresh_ref_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            fresh_cand_trace.write_text(json.dumps({"events": []}), encoding="utf-8")
            fresh_ref_trace.with_name("canonical-trace.json").write_text(
                json.dumps({"events": [{"index": 0, "syscall_name": "openat"}]}),
                encoding="utf-8",
            )
            fresh_cand_trace.with_name("canonical-trace.json").write_text(
                json.dumps({"events": [{"index": 0, "syscall_name": "openat"}]}),
                encoding="utf-8",
            )

            fresh_runs = {
                "reference": {
                    "trace_json_path": str(fresh_ref_trace),
                    "console_log_path": str(root / "fresh-reference-console.log"),
                },
                "candidate": {
                    "trace_json_path": str(fresh_cand_trace),
                    "console_log_path": str(root / "fresh-candidate-console.log"),
                },
                "reference_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "candidate_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "reference_canonical_path": str(fresh_ref_trace.with_name("canonical-trace.json")),
                "candidate_canonical_path": str(fresh_cand_trace.with_name("canonical-trace.json")),
            }

            source_entry = {
                "program_id": "target",
                "campaign_package_dir": "/tmp/source-package",
                "campaign_package_slot": 4,
            }

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "normalization": {"runtime_syscalls": ["mmap"]},
                },
            ), patch(
                "tools.reduce_case.run_case",
                return_value=(
                    {"program_id": "target", "call_count": 0},
                    {"equivalent": False, "first_divergence_index": 0},
                    fresh_runs,
                ),
            ) as run_case, patch(
                "tools.reduce_case.scml_preflight_for_program",
                return_value={
                    "program_id": "target",
                    "status": "passed",
                    "reasons": [],
                    "trace_log_path": str(root / "preflight.strace.log"),
                    "sctrace_output_path": str(root / "preflight.sctrace.txt"),
                    "reducer_replay_mode": "fresh_replay",
                    "reducer_replay_recovery_reason": "",
                },
            ), patch(
                "tools.reduce_case.recorded_source_evidence",
                side_effect=AssertionError("unexpected recovery fallback"),
            ), patch(
                "tools.reduce_case.report_path",
                side_effect=lambda *parts, **_kwargs: root / parts[0],
            ):
                minimized_path, info, comparison, runs, minimized_preflight = greedy_reduce(
                    program_path,
                    source_entry=source_entry,
                )
                minimized_text = minimized_path.read_text(encoding="utf-8")

            run_case.assert_called_once()
            self.assertEqual(run_case.call_args.kwargs["campaign_package_dir"], Path("/tmp/source-package"))
            self.assertEqual(run_case.call_args.kwargs["campaign_package_slot"], 4)
            self.assertEqual(info["program_id"], "target")
            self.assertEqual(comparison["first_divergence_index"], 0)
            self.assertEqual(runs["reference"]["trace_json_path"], str(fresh_ref_trace))
            self.assertEqual(runs["candidate"]["trace_json_path"], str(fresh_cand_trace))
            self.assertEqual(minimized_text, "openat(0x0)\n")
            self.assertEqual(minimized_preflight["reducer_replay_mode"], "fresh_replay")

    def test_greedy_reduce_skips_invalid_trial_programs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            program_path.write_text("openat(0x0)\nclose(0x0)\n", encoding="utf-8")

            initial_runs = {
                "reference": {"trace_json_path": str(root / "initial-ref.json"), "console_log_path": str(root / "initial-ref.log")},
                "candidate": {"trace_json_path": str(root / "initial-cand.json"), "console_log_path": str(root / "initial-cand.log")},
                "reference_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "candidate_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "reference_canonical_path": str(root / "initial-ref-canonical.json"),
                "candidate_canonical_path": str(root / "initial-cand-canonical.json"),
            }
            valid_trial_runs = {
                "reference": {"trace_json_path": str(root / "valid-ref.json"), "console_log_path": str(root / "valid-ref.log")},
                "candidate": {"trace_json_path": str(root / "valid-cand.json"), "console_log_path": str(root / "valid-cand.log")},
                "reference_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "candidate_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "reference_canonical_path": str(root / "valid-ref-canonical.json"),
                "candidate_canonical_path": str(root / "valid-cand-canonical.json"),
            }
            terminal_trial_runs = {
                "reference": {"trace_json_path": str(root / "terminal-ref.json"), "console_log_path": str(root / "terminal-ref.log")},
                "candidate": {"trace_json_path": str(root / "terminal-cand.json"), "console_log_path": str(root / "terminal-cand.log")},
                "reference_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "candidate_canonical": {"events": [{"index": 0, "syscall_name": "openat"}]},
                "reference_canonical_path": str(root / "terminal-ref-canonical.json"),
                "candidate_canonical_path": str(root / "terminal-cand-canonical.json"),
            }

            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "normalization": {"runtime_syscalls": ["mmap"]},
                },
            ), patch(
                "tools.reduce_case.temp_dir",
                return_value=root,
            ), patch(
                "tools.reduce_case.run_case",
                side_effect=[
                    (
                        {"program_id": "target", "call_count": 2},
                        {"equivalent": False, "first_divergence_index": 0},
                        initial_runs,
                    ),
                    SystemExit("invalid mutant"),
                    (
                        {"program_id": "reduced", "call_count": 1},
                        {"equivalent": False, "first_divergence_index": 0},
                        valid_trial_runs,
                    ),
                    (
                        {"program_id": "reduced", "call_count": 1},
                        {"equivalent": True, "first_divergence_index": None},
                        terminal_trial_runs,
                    ),
                ],
            ) as run_case, patch(
                "tools.reduce_case.scml_preflight_for_program",
                side_effect=[
                    {
                        "program_id": "target",
                        "status": "passed",
                        "reasons": [],
                        "trace_log_path": str(root / "initial-preflight.strace.log"),
                        "sctrace_output_path": str(root / "initial-preflight.sctrace.txt"),
                        "reducer_replay_mode": "fresh_replay",
                        "reducer_replay_recovery_reason": "",
                    },
                    {
                        "program_id": "reduced",
                        "status": "passed",
                        "reasons": [],
                        "trace_log_path": str(root / "valid-preflight.strace.log"),
                        "sctrace_output_path": str(root / "valid-preflight.sctrace.txt"),
                        "reducer_replay_mode": "fresh_replay",
                        "reducer_replay_recovery_reason": "",
                    },
                ],
            ), patch(
                "tools.reduce_case.mutate_drop_call",
                side_effect=["invalid\n", "reduced\n", "terminal\n"],
            ), patch(
                "tools.reduce_case.report_path",
                side_effect=lambda *parts, **_kwargs: root / parts[0],
            ):
                minimized_path, info, comparison, runs, minimized_preflight = greedy_reduce(program_path)
                minimized_text = minimized_path.read_text(encoding="utf-8")

        self.assertEqual(run_case.call_count, 4)
        self.assertEqual(info["program_id"], "reduced")
        self.assertEqual(comparison["first_divergence_index"], 0)
        self.assertEqual(runs["candidate"]["trace_json_path"], str(root / "valid-cand.json"))
        self.assertEqual(minimized_text, "reduced\n")
        self.assertEqual(minimized_preflight["status"], "passed")

    def test_scml_preflight_for_program_uses_configured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            binary_path = root / "testcase.bin"
            binary_path.write_text("", encoding="utf-8")
            fake_source = SimpleNamespace(
                load_manifest_index=lambda: {},
                scml_files=lambda: [],
                sctrace_command=lambda *_args: ["sctrace"],
            )
            fake_gate = SimpleNamespace(
                parse_sctrace_lines=lambda *_args, **_kwargs: [],
                relevant_output_lines=lambda lines, **_kwargs: lines,
                classify_line=lambda *_args, **_kwargs: [],
            )
            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "preflight": {
                        "timeout_sec": 7,
                        "rejection_taxonomy": {
                            "preflight_runtime_timeout": "preflight_runtime_timeout",
                            "preflight_runtime_failure": "preflight_runtime_failure",
                            "preflight_matcher_timeout": "preflight_matcher_timeout",
                            "scml_parser_gap": "scml_parser_gap",
                        },
                    },
                    "normalization": {"runtime_syscalls": ["mmap"]},
                },
            ), patch(
                "tools.reduce_case.inspect_program",
                return_value={"program_id": "target", "full_syscall_list": ["openat"]},
            ), patch(
                "tools.reduce_case.build_one",
                return_value={"status": "ok", "testcase_bin": str(binary_path)},
            ), patch(
                "tools.reduce_case.report_path",
                side_effect=lambda *parts, **_kwargs: root / Path(*parts),
            ), patch(
                "tools.reduce_case.AsterinasSCMLSource",
                return_value=fake_source,
            ), patch(
                "tools.reduce_case.AsterinasSCMLGate",
                return_value=fake_gate,
            ), patch(
                "tools.reduce_case.subprocess.run",
                side_effect=[
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                ],
            ) as subprocess_run:
                result = reduce_case.scml_preflight_for_program(program_path, require_zero_exit=True)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(subprocess_run.call_args_list[0].kwargs["timeout"], 7)
        self.assertEqual(subprocess_run.call_args_list[1].kwargs["timeout"], 7)

    def test_scml_preflight_for_program_rejects_nonzero_runtime_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            program_path = root / "program.syz"
            binary_path = root / "testcase.bin"
            binary_path.write_text("", encoding="utf-8")
            fake_source = SimpleNamespace(
                load_manifest_index=lambda: {},
                scml_files=lambda: [],
                sctrace_command=lambda *_args: ["sctrace"],
            )
            fake_gate = SimpleNamespace(
                parse_sctrace_lines=lambda *_args, **_kwargs: [],
                relevant_output_lines=lambda lines, **_kwargs: lines,
                classify_line=lambda *_args, **_kwargs: [],
            )
            with patch(
                "tools.reduce_case.config",
                return_value={
                    "workflow": "asterinas_scml",
                    "stability": {"timeout_sec": 120, "rerun_count": 0},
                    "preflight": {
                        "timeout_sec": 7,
                        "rejection_taxonomy": {
                            "preflight_runtime_timeout": "preflight_runtime_timeout",
                            "preflight_runtime_failure": "preflight_runtime_failure",
                            "preflight_matcher_timeout": "preflight_matcher_timeout",
                            "scml_parser_gap": "scml_parser_gap",
                        },
                    },
                    "normalization": {"runtime_syscalls": ["mmap"]},
                },
            ), patch(
                "tools.reduce_case.inspect_program",
                return_value={"program_id": "target", "full_syscall_list": ["openat"]},
            ), patch(
                "tools.reduce_case.build_one",
                return_value={"status": "ok", "testcase_bin": str(binary_path)},
            ), patch(
                "tools.reduce_case.report_path",
                side_effect=lambda *parts, **_kwargs: root / Path(*parts),
            ), patch(
                "tools.reduce_case.AsterinasSCMLSource",
                return_value=fake_source,
            ), patch(
                "tools.reduce_case.AsterinasSCMLGate",
                return_value=fake_gate,
            ), patch(
                "tools.reduce_case.subprocess.run",
                side_effect=[
                    SimpleNamespace(returncode=1, stdout="", stderr=""),
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                ],
            ):
                result = reduce_case.scml_preflight_for_program(program_path)

        self.assertEqual(result["status"], "rejected_by_scml")
        self.assertEqual(result["reasons"], ["preflight_runtime_failure"])


if __name__ == "__main__":
    unittest.main()
