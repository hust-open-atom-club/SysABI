from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.common import configure_runtime, resolved_config_path, runner_profiles
from orchestrator.scheduler import candidate_batching_enabled
from orchestrator.vm_runner import execute_side


class ContractSurfaceTests(unittest.TestCase):
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

    def test_resolved_config_path_supports_builtin_workflows(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        self.assertEqual(resolved_config_path(workflow="baseline"), repo_root / "configs" / "workflows" / "baseline.json")
        self.assertEqual(resolved_config_path(workflow="asterinas"), repo_root / "configs" / "workflows" / "asterinas.json")
        self.assertEqual(
            resolved_config_path(workflow="asterinas_scml"),
            repo_root / "configs" / "workflows" / "asterinas_scml.json",
        )

    def test_runner_profiles_load_for_builtin_workflows(self) -> None:
        baseline = runner_profiles(workflow="baseline")
        self.assertEqual(baseline["reference"]["kind"], "local")
        self.assertEqual(baseline["candidate"]["kind"], "local")

        asterinas = runner_profiles(workflow="asterinas")
        self.assertEqual(asterinas["candidate"]["kind"], "command")
        self.assertEqual(asterinas["candidate"]["binary_name"], "testcase.candidate.bin")

        asterinas_scml = runner_profiles(workflow="asterinas_scml")
        self.assertEqual(asterinas_scml["candidate"]["kind"], "command")
        self.assertEqual(asterinas_scml["reference"]["work_root"], "artifacts/sandboxes/asterinas_scml/reference")

    def test_execute_side_command_runner_materializes_protocol_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_dir = root / "build"
            artifacts_dir = root / "artifacts"
            sandboxes_dir = root / "sandboxes"
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            runner_script = root / "fake_command_runner.py"
            profiles_path = root / "runner_profiles.json"
            config_path = root / "baseline_rules.json"

            program_id = "case-protocol"
            build_root = build_dir / program_id
            build_root.mkdir(parents=True, exist_ok=True)
            (build_root / "testcase.bin").write_text("binary", encoding="utf-8")
            (build_root / "testcase.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            (build_root / "testcase.instrumented.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            (build_root / "build-result.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            eligible_file.write_text("", encoding="utf-8")

            runner_script.write_text(
                textwrap.dedent(
                    """
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    observed_path = Path(sys.argv[1])
                    observed_env = {k: v for k, v in os.environ.items() if k.startswith("SYZABI_") or k == "TMPDIR"}
                    observed_path.write_text(json.dumps(observed_env, ensure_ascii=False, indent=2, sort_keys=True) + "\\n", encoding="utf-8")

                    events_path = Path(os.environ["SYZABI_TRACE_EVENTS_PATH"])
                    events_path.write_text(
                        json.dumps(
                            {
                                "event_index": 0,
                                "side": os.environ["SYZABI_SIDE"],
                                "syscall_name": "close",
                                "syscall_number": 3,
                                "args": [3, 0, 0, 0, 0, 0],
                                "return_value": 0,
                                "errno": 0,
                                "start_ns": 1,
                                "end_ns": 2,
                                "outputs": [],
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\\n",
                        encoding="utf-8",
                    )

                    Path(os.environ["SYZABI_EXTERNAL_STATE_PATH"]).write_text(
                        json.dumps({"files": [{"path": "out.txt", "size": 2, "sha256": "abc"}]}, ensure_ascii=False, sort_keys=True) + "\\n",
                        encoding="utf-8",
                    )
                    Path(os.environ["SYZABI_RUNNER_RESULT_PATH"]).write_text(
                        json.dumps({"status": "ok", "exit_code": 0, "kernel_build": "fake-kernel"}, ensure_ascii=False, sort_keys=True) + "\\n",
                        encoding="utf-8",
                    )
                    print("runner stdout")
                    print("runner stderr", file=sys.stderr)
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            profiles_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "reference": {
                            "kind": "command",
                            "role": "reference",
                            "snapshot_id": "fake-reference",
                            "work_root": str(sandboxes_dir / "reference"),
                            "kernel_build_command": "printf reference-kernel",
                            "command": ["python3", str(runner_script), "{artifact_root}/observed-env.json"],
                        },
                        "candidate": {
                            "kind": "command",
                            "role": "candidate",
                            "snapshot_id": "fake-candidate",
                            "work_root": str(sandboxes_dir / "candidate"),
                            "kernel_build_command": "printf candidate-kernel",
                            "command": ["python3", str(runner_script), "{artifact_root}/observed-env.json"],
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "baseline",
                        "target": "linux",
                        "schema_version": 1,
                        "runner_profiles_path": str(profiles_path),
                        "paths": {
                            "build_dir": str(build_dir),
                            "artifacts_dir": str(artifacts_dir),
                            "reports_dir": str(reports_dir),
                            "eligible_file": str(eligible_file),
                            "temp_dir": str(root / "tmp"),
                        },
                        "normalization": {"preview_bytes": 32},
                        "classification": {
                            "no_diff": "NO_DIFF",
                            "baseline_invalid": "BASELINE_INVALID",
                            "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                            "unsupported_feature": "UNSUPPORTED_FEATURE",
                            "bug_likely": "BUG_LIKELY",
                        },
                        "thresholds": {"smoke": {}, "signoff": {}},
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            configure_runtime(workflow="baseline", config_path=config_path)
            result = execute_side(
                program_id=program_id,
                side="reference",
                timeout_sec=5,
                run_id="proto-ref0",
            )

            artifact_root = artifacts_dir / program_id / "proto-ref0" / "reference"
            observed_env = json.loads((artifact_root / "observed-env.json").read_text(encoding="utf-8"))
            raw_trace = json.loads((artifact_root / "raw-trace.json").read_text(encoding="utf-8"))
            external_state = json.loads((artifact_root / "external-state.json").read_text(encoding="utf-8"))
            run_result = json.loads((artifact_root / "run-result.json").read_text(encoding="utf-8"))

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.kernel_build, "fake-kernel")
            self.assertEqual(result.runner_kind, "command")
            self.assertEqual(observed_env["SYZABI_PROGRAM_ID"], program_id)
            self.assertEqual(observed_env["SYZABI_SIDE"], "reference")
            self.assertEqual(observed_env["SYZABI_RUN_ID"], "proto-ref0")
            self.assertTrue(observed_env["SYZABI_RUNNER_RESULT_PATH"].endswith("runner-result.json"))
            self.assertTrue(observed_env["SYZABI_TRACE_EVENTS_PATH"].endswith("raw-trace.events.jsonl"))
            self.assertTrue((artifact_root / "stdout.txt").read_text(encoding="utf-8").startswith("runner stdout"))
            self.assertTrue((artifact_root / "stderr.txt").read_text(encoding="utf-8").startswith("runner stderr"))
            self.assertEqual(raw_trace["program_id"], program_id)
            self.assertEqual(raw_trace["side"], "reference")
            self.assertEqual(raw_trace["process_exit"]["status"], "ok")
            self.assertEqual(len(raw_trace["events"]), 1)
            self.assertEqual(external_state["files"][0]["path"], "out.txt")
            self.assertEqual(run_result["program_id"], program_id)
            self.assertEqual(run_result["kernel_build"], "fake-kernel")

    def test_candidate_batching_is_capability_driven(self) -> None:
        args = SimpleNamespace(candidate_batch_size=2)
        with patch("orchestrator.scheduler.runner_profiles", return_value={"candidate": {"batch_command": ["echo", "batch"]}}):
            self.assertTrue(
                candidate_batching_enabled(
                    args,
                    {
                        "workflow": "custom_workflow",
                        "capabilities": {"supports_batch_execution": True},
                    },
                )
            )
            self.assertFalse(
                candidate_batching_enabled(
                    args,
                    {
                        "workflow": "asterinas",
                        "capabilities": {"supports_batch_execution": False},
                    },
                )
            )


if __name__ == "__main__":
    unittest.main()
