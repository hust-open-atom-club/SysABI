from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import ExitStack, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.capabilities import capabilities_from_config
from orchestrator.common import config, configure_runtime, resolved_config_path, runner_profiles
from orchestrator.legacy_compat import _WARNED_DEPRECATIONS
from orchestrator.scheduler import candidate_batching_enabled
from orchestrator.vm_runner import TRACE_EVENT_STDOUT_PREFIX, execute_side
from runners.factory import build_runner
from targets.base import (
    PACKAGED_PER_CASE_EXECUTION_MODE,
    SHARED_RUNTIME_BATCH_EXECUTION_MODE,
    SINGLE_COMMAND_EXECUTION_MODE,
    TargetAdapter,
)
from core.workflow_contract import WorkflowContractError, validate_repo_workflow_payload
from targets.registry import TargetLookupError, get_target_adapter
from tools.render_summary import workflow_side_labels


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
        self.assertEqual(
            resolved_config_path(workflow="tgoskits_starryos"),
            repo_root / "configs" / "workflows" / "tgoskits_starryos.json",
        )
        self.assertEqual(
            resolved_config_path(workflow="tgoskits_starryos_scale"),
            repo_root / "configs" / "workflows" / "tgoskits_starryos_scale.json",
        )

    def test_runner_profiles_load_for_builtin_workflows(self) -> None:
        baseline = runner_profiles(workflow="baseline")
        self.assertEqual(baseline["reference"]["kind"], "local")
        self.assertEqual(baseline["candidate"]["kind"], "local")

        asterinas = runner_profiles(workflow="asterinas")
        self.assertEqual(asterinas["candidate"]["kind"], "command")
        self.assertEqual(asterinas["candidate"]["binary_name"], "testcase.candidate.bin")
        self.assertIn("/targets/entrypoint.py", " ".join(asterinas["candidate"]["command"]))
        self.assertNotIn("/tools/run_asterinas.py", " ".join(asterinas["candidate"]["command"]))

        asterinas_scml = runner_profiles(workflow="asterinas_scml")
        self.assertEqual(asterinas_scml["candidate"]["kind"], "command")
        self.assertEqual(asterinas_scml["reference"]["work_root"], "artifacts/sandboxes/asterinas_scml/reference")
        self.assertIn("/targets/entrypoint.py", " ".join(asterinas_scml["candidate"]["command"]))
        self.assertNotIn("/tools/run_asterinas.py", " ".join(asterinas_scml["candidate"]["command"]))

        starry = runner_profiles(workflow="tgoskits_starryos")
        self.assertEqual(starry["candidate"]["kind"], "command")
        self.assertIn("/targets/entrypoint.py", " ".join(starry["candidate"]["command"]))

        starry_scale = runner_profiles(workflow="tgoskits_starryos_scale")
        self.assertEqual(starry_scale["candidate"]["kind"], "command")
        self.assertEqual(starry_scale["candidate"]["command_batching_mode"], "shared_runtime_batch")

    def test_canonical_and_legacy_config_paths_resolve_to_target_metadata(self) -> None:
        baseline = config(workflow="baseline")
        self.assertEqual(baseline["target"], "linux")
        self.assertEqual(baseline["target_config"]["build_info_path"], "artifacts/targets/linux/build-info.json")

        canonical = config(workflow="asterinas")
        self.assertEqual(canonical["target"], "asterinas")
        self.assertEqual(canonical["paths"]["eligible_file"], "eligible_programs/targets/asterinas/asterinas/default.jsonl")
        self.assertEqual(canonical["target_config"]["build_info_path"], "artifacts/targets/asterinas/build-info.json")

        starry = config(workflow="tgoskits_starryos")
        self.assertEqual(starry["target"], "tgoskits_starryos")
        self.assertEqual(starry["trace"]["events_transport"], "stdout")
        self.assertEqual(starry["target_config"]["build_info_path"], "artifacts/targets/tgoskits_starryos/build-info.json")

        _WARNED_DEPRECATIONS.clear()
        stderr = StringIO()
        with redirect_stderr(stderr):
            legacy = config(config_path="configs/asterinas_rules.json")
        self.assertEqual(legacy["target"], "asterinas")
        self.assertIn("asterinas", legacy)
        self.assertEqual(legacy["asterinas"]["build_info_path"], "artifacts/asterinas/build-info.json")
        self.assertIn("deprecated compatibility path", stderr.getvalue())

    def test_builtin_target_adapters_expose_expanded_lifecycle_contract(self) -> None:
        workflow_modes = {
            "baseline": SINGLE_COMMAND_EXECUTION_MODE,
            "asterinas": PACKAGED_PER_CASE_EXECUTION_MODE,
            "tgoskits_starryos": SHARED_RUNTIME_BATCH_EXECUTION_MODE,
            "tgoskits_starryos_scale": SHARED_RUNTIME_BATCH_EXECUTION_MODE,
            "tgoskits_arceos_smoke": SINGLE_COMMAND_EXECUTION_MODE,
        }

        for workflow, expected_mode in workflow_modes.items():
            with self.subTest(workflow=workflow):
                cfg = config(workflow=workflow)
                adapter = get_target_adapter(cfg)
                with ExitStack() as stack:
                    if cfg["target"] == "tgoskits_starryos":
                        stack.enter_context(
                            patch(
                                "targets.tgoskits_starryos.adapter.api.preflight_payload",
                                return_value={"target": cfg["target"], "workflow": cfg["workflow"]},
                            )
                        )
                    elif cfg["target"] == "tgoskits_arceos":
                        stack.enter_context(
                            patch(
                                "targets.tgoskits_arceos.adapter.api.preflight_payload",
                                return_value={"target": cfg["target"], "workflow": cfg["workflow"]},
                            )
                        )
                        stack.enter_context(
                            patch(
                                "targets.tgoskits_arceos.adapter.api.replay_preflight_payload",
                                return_value={"target": cfg["target"], "workflow": cfg["workflow"], "mode": "experimental-c-app"},
                            )
                        )

                    self.assertIsInstance(adapter, TargetAdapter)
                    self.assertEqual(adapter.capabilities(cfg), capabilities_from_config(cfg))
                    self.assertEqual(adapter.execution_modes(cfg), (expected_mode,))

                    preflight = adapter.preflight_payload(cfg)
                    self.assertEqual(preflight["target"], cfg["target"])
                    self.assertEqual(preflight["workflow"], cfg["workflow"])

                    campaign_assets = adapter.prepare_campaign_assets(cfg)
                    self.assertEqual(campaign_assets["target"], cfg["target"])

                    prepared_case = adapter.prepare_case({"program_id": "case-one", "binary_path": "/tmp/case-one.bin"}, cfg)
                    self.assertEqual(prepared_case["target"], cfg["target"])
                    self.assertEqual(prepared_case["program_id"], "case-one")

                    prepared_batch = adapter.prepare_batch([prepared_case], cfg)
                    if capabilities_from_config(cfg).supports_batch_execution:
                        self.assertIsNotNone(prepared_batch)
                        self.assertEqual(prepared_batch["case_count"], 1)
                        self.assertEqual(prepared_batch["execution_mode"], expected_mode)
                    else:
                        self.assertIsNone(prepared_batch)

                    collected = adapter.collect_result({"status": "ok"}, cfg)
                    self.assertEqual(collected["target"], cfg["target"])
                    finalized = adapter.finalize_result(collected, cfg)
                    self.assertTrue(finalized["finalized"])

    def test_target_adapter_protocol_rejects_missing_lifecycle_methods(self) -> None:
        class IncompleteAdapter:
            name = "incomplete"

            def compose_template_inputs(self, cfg):
                return {}

            def packaged_candidate_env(self, package_dir, slot):
                return {}

            def prewarm_candidate_batch(self, *, prepared_cases, package_dir, cfg):
                return None

            def prepare_target(self, **kwargs):
                return None

            def healthcheck(self, *args, **kwargs):
                return None

            def run_case(self, *args, **kwargs):
                return None

            def run_batch(self, *args, **kwargs):
                return None

        self.assertFalse(isinstance(IncompleteAdapter(), TargetAdapter))

    def test_repo_workflow_contract_requires_normalized_top_level_mappings(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = {
            "workflow": "invalid",
            "target": "linux",
            "arch": "amd64",
            "runner_profiles_path": "configs/targets/linux/runner_profiles.baseline.json",
            "target_config_path": "configs/targets/linux/target.json",
            "paths": {
                "build_dir": "build/targets/linux/invalid/testcases",
                "artifacts_dir": "artifacts/runs/targets/linux/invalid",
                "reports_dir": "reports/targets/linux/invalid",
                "eligible_file": "eligible_programs/targets/linux/invalid/default.jsonl",
                "temp_dir": "artifacts/tmp",
            },
            "parallel": {},
            "presentation": {},
            "stability": {},
            "thresholds": {},
        }
        with self.assertRaises(WorkflowContractError):
            validate_repo_workflow_payload(
                payload,
                resolved_path=repo_root / "configs" / "workflows" / "invalid.json",
                repo_root=repo_root,
            )

    def test_repo_workflow_contract_rejects_unknown_top_level_keys(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        payload = {
            "workflow": "invalid",
            "target": "linux",
            "arch": "amd64",
            "runner_profiles_path": "configs/targets/linux/runner_profiles.baseline.json",
            "target_config_path": "configs/targets/linux/target.json",
            "paths": {
                "build_dir": "build/targets/linux/invalid/testcases",
                "artifacts_dir": "artifacts/runs/targets/linux/invalid",
                "reports_dir": "reports/targets/linux/invalid",
                "eligible_file": "eligible_programs/targets/linux/invalid/default.jsonl",
                "temp_dir": "artifacts/tmp",
            },
            "parallel": {},
            "presentation": {},
            "stability": {},
            "thresholds": {},
            "unexpected_knob": True,
        }
        with self.assertRaises(WorkflowContractError) as cm:
            validate_repo_workflow_payload(
                payload,
                resolved_path=repo_root / "configs" / "workflows" / "invalid.json",
                repo_root=repo_root,
            )
        self.assertIn("unknown top-level keys", str(cm.exception))

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

    def test_execute_side_can_extract_stdout_framed_trace_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_dir = root / "build"
            artifacts_dir = root / "artifacts"
            sandboxes_dir = root / "sandboxes"
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            runner_script = root / "fake_stdout_trace_runner.py"
            profiles_path = root / "runner_profiles.json"
            config_path = root / "stdout_trace.json"

            program_id = "case-stdout-trace"
            build_root = build_dir / program_id
            build_root.mkdir(parents=True, exist_ok=True)
            (build_root / "testcase.bin").write_text("binary", encoding="utf-8")
            (build_root / "build-result.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            eligible_file.write_text("", encoding="utf-8")

            runner_script.write_text(
                textwrap.dedent(
                    f"""
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    Path(os.environ["SYZABI_RUNNER_RESULT_PATH"]).write_text(
                        json.dumps({{"status": "ok", "exit_code": 0, "kernel_build": "stdout-trace-kernel"}}, sort_keys=True),
                        encoding="utf-8",
                    )
                    print("boot noise before trace")
                    print("{TRACE_EVENT_STDOUT_PREFIX}" + json.dumps(
                        {{
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
                        }},
                        sort_keys=True,
                    ))
                    print("{TRACE_EVENT_STDOUT_PREFIX}" + json.dumps(
                        {{
                            "event_index": 1,
                            "side": os.environ["SYZABI_SIDE"],
                            "syscall_name": "getpid",
                            "syscall_number": 39,
                            "args": [0, 0, 0, 0, 0, 0],
                            "return_value": 123,
                            "errno": 0,
                            "start_ns": 3,
                            "end_ns": 4,
                            "outputs": [],
                        }},
                        sort_keys=True,
                    ), file=sys.stderr)
                    print("boot noise after trace", file=sys.stderr)
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
                            "snapshot_id": "stdout-reference",
                            "work_root": str(sandboxes_dir / "reference"),
                            "kernel_build_command": "printf stdout-reference",
                            "command": ["python3", str(runner_script)],
                        },
                        "candidate": {
                            "kind": "command",
                            "role": "candidate",
                            "snapshot_id": "stdout-candidate",
                            "work_root": str(sandboxes_dir / "candidate"),
                            "kernel_build_command": "printf stdout-candidate",
                            "command": ["python3", str(runner_script)],
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
                        "workflow": "stdout_trace",
                        "target": "linux",
                        "schema_version": 1,
                        "runner_profiles_path": str(profiles_path),
                        "target_config_path": "configs/targets/linux/target.json",
                        "trace": {"events_transport": "stdout"},
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

            configure_runtime(workflow="stdout_trace", config_path=config_path)
            result = execute_side(program_id=program_id, side="reference", timeout_sec=5, run_id="stdout-ref0")

            artifact_root = artifacts_dir / program_id / "stdout-ref0" / "reference"
            raw_trace = json.loads((artifact_root / "raw-trace.json").read_text(encoding="utf-8"))
            extracted_events = (artifact_root / "raw-trace.events.jsonl").read_text(encoding="utf-8")

            self.assertEqual(result.status, "ok")
            self.assertEqual(raw_trace["events"][0]["syscall_name"], "close")
            self.assertEqual(raw_trace["events"][1]["syscall_name"], "getpid")
            self.assertEqual(raw_trace["events"][0]["side"], "reference")
            self.assertIn('"syscall_name": "close"', extracted_events)
            self.assertIn('"syscall_name": "getpid"', extracted_events)

    def test_registry_rejects_unknown_targets_and_runner_kinds(self) -> None:
        adapter = get_target_adapter({"target": "tgoskits_starryos"})
        self.assertEqual(adapter.name, "tgoskits_starryos")

        with self.assertRaises(TargetLookupError):
            get_target_adapter({"target": "unknown_target"})

        with self.assertRaises(ValueError):
            build_runner({"kind": "unknown_kind"})

    def test_canonical_config_loading_does_not_inject_non_linux_derivation_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workflow_dir = root / "workflows"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            config_path = workflow_dir / "custom.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "custom",
                        "target": "dragonos",
                        "schema_version": 1,
                        "runner_profiles_path": "configs/runner_profiles.json",
                        "paths": {
                            "build_dir": "build/targets/dragonos/custom/testcases",
                            "artifacts_dir": "artifacts/runs/targets/dragonos/custom",
                            "reports_dir": "reports/targets/dragonos/custom",
                            "eligible_file": "eligible_programs/targets/dragonos/custom/default.jsonl",
                            "temp_dir": "artifacts/tmp",
                        },
                        "derivation": {},
                        "normalization": {"preview_bytes": 32},
                        "classification": {"no_diff": "NO_DIFF"},
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            loaded = config(config_path=config_path)
        self.assertEqual(loaded["target"], "dragonos")
        self.assertNotIn("source_eligible_file", loaded.get("derivation", {}))

    def test_candidate_batching_is_capability_driven(self) -> None:
        args = SimpleNamespace(candidate_batch_size=2)
        with patch(
            "orchestrator.scheduler.runner_profiles",
            return_value={"candidate": {"kind": "command", "command_batching_mode": "packaged_per_case"}},
        ):
            self.assertTrue(
                candidate_batching_enabled(
                    args,
                    {
                        "workflow": "custom_workflow",
                        "target": "asterinas",
                        "capabilities": {"supports_batch_execution": True},
                    },
                )
            )
        with patch(
            "orchestrator.scheduler.runner_profiles",
            return_value={"candidate": {"kind": "command", "command_batching_mode": "unknown_mode"}},
        ):
            with self.assertRaises(WorkflowContractError):
                candidate_batching_enabled(
                    args,
                    {
                        "workflow": "custom_workflow",
                        "target": "asterinas",
                        "capabilities": {"supports_batch_execution": True},
                    },
                )
        with patch(
            "orchestrator.scheduler.runner_profiles",
            return_value={"candidate": {"kind": "command", "command_batching_mode": "packaged_per_case"}},
        ):
            self.assertFalse(
                candidate_batching_enabled(
                    args,
                    {
                        "workflow": "asterinas",
                        "capabilities": {"supports_batch_execution": False},
                    },
                )
            )

    def test_candidate_batching_rejects_non_command_runner(self) -> None:
        args = SimpleNamespace(candidate_batch_size=2)
        with patch(
            "orchestrator.scheduler.runner_profiles",
            return_value={"candidate": {"kind": "local", "command_batching_mode": "shared_runtime_batch"}},
        ):
            with self.assertRaises(WorkflowContractError) as cm:
                candidate_batching_enabled(
                    args,
                    {
                        "workflow": "custom_workflow",
                        "target": "tgoskits_starryos",
                        "capabilities": {"supports_batch_execution": True},
                    },
                )
            self.assertIn("command runner profile", str(cm.exception))

    def test_legacy_make_targets_route_through_generic_workflow_entrypoints(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        run = subprocess.run(
            ["make", "-n", "run"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("tools/init_layout.py --workflow baseline", run.stdout)
        self.assertIn("tools/init_layout.py --workflow asterinas", run.stdout)
        self.assertIn("make filter-corpus", run.stdout)
        self.assertIn("make derive-workflow WORKFLOW=asterinas", run.stdout)
        self.assertIn("make prepare-target WORKFLOW=asterinas", run.stdout)
        self.assertIn("make build-workflow WORKFLOW=asterinas", run.stdout)
        self.assertIn("make run-workflow WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=100 JOBS=4", run.stdout)

        build = subprocess.run(
            ["make", "-n", "build-eligible"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(build.returncode, 0)
        self.assertIn("build-eligible is deprecated", build.stdout)
        self.assertIn("make build-workflow WORKFLOW=baseline", build.stdout)

        smoke = subprocess.run(
            ["make", "-n", "run-smoke"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(smoke.returncode, 0)
        self.assertIn("make run-workflow WORKFLOW=baseline CAMPAIGN=smoke LIMIT=100", smoke.stdout)

        asterinas = subprocess.run(
            ["make", "-n", "run-asterinas-smoke"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(asterinas.returncode, 0)
        self.assertIn("make run-workflow WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=4", asterinas.stdout)

        analyze = subprocess.run(
            ["make", "-n", "analyze-asterinas"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(analyze.returncode, 0)
        self.assertIn("make analyze-workflow WORKFLOW=asterinas", analyze.stdout)

        derive_scml = subprocess.run(
            ["make", "-n", "derive-asterinas-scml"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(derive_scml.returncode, 0)
        self.assertIn("derive-asterinas-scml is deprecated", derive_scml.stdout)
        self.assertIn("make derive-workflow WORKFLOW=asterinas_scml", derive_scml.stdout)
        self.assertIn("make preflight-workflow WORKFLOW=asterinas_scml", derive_scml.stdout)

        preflight_scml = subprocess.run(
            ["make", "-n", "preflight-asterinas-scml"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(preflight_scml.returncode, 0)
        self.assertIn("preflight-asterinas-scml is deprecated", preflight_scml.stdout)
        self.assertIn("tools/workflow_path.py --workflow asterinas_scml --key preflight.source_eligible_file", preflight_scml.stdout)

        prepare = subprocess.run(
            ["make", "-n", "prepare-asterinas-candidate"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepare.returncode, 0)
        self.assertIn("make prepare-target WORKFLOW=asterinas", prepare.stdout)

        prepare_scml = subprocess.run(
            ["make", "-n", "prepare-target", "WORKFLOW=asterinas_scml"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prepare_scml.returncode, 0)
        self.assertIn("tools/workflow_path.py --workflow asterinas_scml --key target", prepare_scml.stdout)
        self.assertIn("targets/entrypoint.py --mode \"$TARGET_MODE\" --healthcheck", prepare_scml.stdout)
        self.assertNotIn("tools/run_asterinas.py", prepare_scml.stdout)

        starry_scale = subprocess.run(
            ["make", "-n", "run-tgoskits-starryos-scale"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(starry_scale.returncode, 0)
        self.assertIn("tools/tgoskits_launch.py --workflow tgoskits_starryos_scale campaign --campaign full", starry_scale.stdout)
        self.assertIn("--limit 200", starry_scale.stdout)
        self.assertIn("--jobs 8", starry_scale.stdout)

        clean = subprocess.run(
            ["make", "-n", "clean"],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(clean.returncode, 0)
        self.assertIn('tools/cleanup_repo_processes.py --repo-root "', clean.stdout)
        self.assertNotIn("--remove", clean.stdout)

    def test_render_summary_falls_back_to_generic_labels_without_presentation(self) -> None:
        self.assertEqual(workflow_side_labels({"target": "asterinas"}), ("Reference", "Candidate"))

    def test_current_contracts_labels_round0_coupling_as_historical(self) -> None:
        content = (Path(__file__).resolve().parents[1] / "docs" / "architecture" / "current-contracts.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Round 0 冻结的耦合点", content)
        self.assertIn("Phase 0 冻结的已知耦合", content)
        self.assertNotIn("`candidate_batching_enabled()` 仍以 `workflow.startswith(\"asterinas\")` 判断是否允许 batching；", content)

    def test_target_config_validation_rejects_missing_asterinas_keys(self) -> None:
        from core.workflow_contract import validate_target_config_payload

        workflow_payload = {"target": "asterinas"}
        malformed = {
            "base_initramfs_path": "third_party/asterinas/test/initramfs/build/initramfs.cpio.gz",
            "build_info_path": "artifacts/targets/asterinas/build-info.json",
            "build_timeout_sec": 3600,
            "default_mode": "docker-qemu",
            "docker_image": "asterinas/asterinas:0.17.1-20260317",
            "repo_dir": "third_party/asterinas",
            "revision": "main",
            # missing run_timeout_sec
        }
        with self.assertRaises(WorkflowContractError) as cm:
            validate_target_config_payload(malformed, workflow_payload=workflow_payload)
        self.assertIn("run_timeout_sec", str(cm.exception))

    def test_registry_rejects_incomplete_adapter_at_runtime(self) -> None:
        from targets.registry import TargetLookupError, _validate_target_adapter
        from targets.base import TargetAdapter

        class FakeAdapter:
            name = "fake"

            def capabilities(self, cfg):
                from core.capabilities import CapabilitySet
                return CapabilitySet(supports_batch_execution=True, supports_preflight=False, supports_snapshot_reuse=False)

            def execution_modes(self, cfg):
                return ("packaged_per_case",)

            def requires_campaign_healthcheck(self, cfg):
                return False

            def preflight_payload(self, cfg):
                return {}

            def prepare_campaign_assets(self, cfg, args=None):
                return {}

            def prepare_case(self, entry, cfg):
                return {}

            def prepare_batch(self, cases, cfg):
                return {}

            def collect_result(self, result, cfg):
                return {}

            def finalize_result(self, result, cfg):
                return {}

            def prepare_case_package_payload(self, cases, cfg, batch_metadata):
                return None  # structurally present but functionally incomplete

            def prepare_batch_manifest_payload(self, cases, cfg, batch_metadata):
                return None

            def case_package_id(self, payload):
                from targets.base import case_package_id as _case_package_id
                return _case_package_id(payload)

            def batch_manifest_id(self, payload):
                from targets.base import batch_manifest_id as _batch_manifest_id
                return _batch_manifest_id(payload)

            def runner_errors(self):
                return ()

            def compose_template_inputs(self, cfg):
                return {}

            def packaged_candidate_env(self, package_dir, slot):
                return {}

            def prewarm_candidate_batch(self, *, prepared_cases, package_dir, cfg):
                return None

            def prepare_target(self, **kwargs):
                return None

            def healthcheck(self, *args, **kwargs):
                return None

            def run_case(self, *args, **kwargs):
                return None

            def run_batch(self, *args, **kwargs):
                return None

        adapter = FakeAdapter()
        self.assertIsInstance(adapter, TargetAdapter)
        with self.assertRaises(TargetLookupError) as cm:
            _validate_target_adapter(adapter, {"target": "fake", "capabilities": {"supports_batch_execution": True}})
        self.assertIn("packaged_per_case", str(cm.exception))

    def test_vm_runner_delegates_package_identity_to_adapter(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from orchestrator.vm_runner import prepare_candidate_initramfs_package, prepare_shared_batch_manifest

        calls = []

        class IdentityAdapter:
            name = "identity"

            def prepare_case_package_payload(self, cases, cfg, batch_metadata):
                return {"cases": []}

            def prepare_batch_manifest_payload(self, cases, cfg, batch_metadata):
                return {"cases": []}

            def case_package_id(self, payload):
                calls.append("case_package_id")
                return "custom-package-id"

            def batch_manifest_id(self, payload):
                calls.append("batch_manifest_id")
                return "custom-batch-id"

        cfg = {
            "workflow": "identity",
            "target": "identity",
            "normalization": {"preview_bytes": 32},
            "paths": {"candidate_initramfs_packages_dir": "artifacts/packages"},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pkg_root = root / "artifacts" / "packages"
            pkg_root.mkdir(parents=True, exist_ok=True)
            cfg["paths"] = {
                "candidate_initramfs_packages_dir": str(pkg_root),
                "temp_dir": str(root / "tmp"),
            }
            with patch("orchestrator.vm_runner.config", return_value=cfg), patch(
                "orchestrator.vm_runner.get_target_adapter", return_value=IdentityAdapter()
            ), patch("orchestrator.vm_runner.candidate_initramfs_package_root", return_value=pkg_root):
                package_dir, slot_map = prepare_candidate_initramfs_package([], cfg)
                self.assertEqual(package_dir.name, "custom-package-id")
                self.assertIn("case_package_id", calls)

            with patch("orchestrator.vm_runner.config", return_value=cfg), patch(
                "orchestrator.vm_runner.get_target_adapter", return_value=IdentityAdapter()
            ):
                manifest_path = prepare_shared_batch_manifest([], cfg)
                self.assertEqual(manifest_path.stem, "custom-batch-id")
                self.assertIn("batch_manifest_id", calls)


if __name__ == "__main__":
    unittest.main()
