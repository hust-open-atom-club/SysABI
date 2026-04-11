from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import scheduler


FIXTURE_ROOT = Path(__file__).resolve().parent / "regression" / "golden" / "reporting_smoke"
BASELINE_FIXTURE_ROOT = Path(__file__).resolve().parent / "regression" / "golden" / "baseline_smoke"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def load_fixture_json(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def load_fixture_jsonl(name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in (FIXTURE_ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def load_baseline_fixture_json(name: str) -> dict[str, object]:
    return json.loads((BASELINE_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def load_baseline_fixture_jsonl(name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in (BASELINE_FIXTURE_ROOT / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def canonical_trace(program_id: str, side: str, syscall_name: str) -> dict[str, object]:
    return {
        "program_id": program_id,
        "side": side,
        "event_count": 1,
        "events": [
            {
                "index": 0,
                "source_event_index": 0,
                "syscall_name": syscall_name,
                "syscall_number": 0,
                "args": ["fd#0"],
                "return_value": 0,
                "errno": 0,
                "duration_ns": 1,
                "outputs": [],
            }
        ],
        "final_state": {"files": []},
        "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
    }


class ContractRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
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

    def normalize_paths(self, value: object, root: Path) -> object:
        if isinstance(value, str):
            return value.replace(str(root), "__ROOT__")
        if isinstance(value, list):
            return [self.normalize_paths(item, root) for item in value]
        if isinstance(value, dict):
            return {key: self.normalize_paths(item, root) for key, item in value.items()}
        return value

    def setup_case(self, root: Path, name: str, syscall_name: str) -> tuple[Path, Path, Path]:
        normalized = root / "programs" / f"{name}.syz"
        normalized.parent.mkdir(parents=True, exist_ok=True)
        normalized.write_text(f"{syscall_name}()\n", encoding="utf-8")

        ref_raw = root / "runs" / name / "reference" / "raw-trace.json"
        cand_raw = root / "runs" / name / "candidate" / "raw-trace.json"
        write_json(
            ref_raw,
            {
                "program_id": name,
                "side": "reference",
                "run_id": f"{name}-ref",
                "status": "ok",
                "events": [],
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            },
        )
        write_json(
            cand_raw,
            {
                "program_id": name,
                "side": "candidate",
                "run_id": f"{name}-cand",
                "status": "ok",
                "events": [],
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            },
        )
        write_json(ref_raw.with_name("canonical-trace.json"), canonical_trace(name, "reference", syscall_name))
        write_json(cand_raw.with_name("canonical-trace.json"), canonical_trace(name, "candidate", syscall_name))
        return normalized, ref_raw, cand_raw

    def test_scheduler_reporting_outputs_match_golden_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_dir = root / "build"
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"
            reports_dir.mkdir(parents=True, exist_ok=True)

            eligible_rows = [
                {"program_id": "case-no-diff"},
                {"program_id": "case-bug"},
                {"program_id": "case-noise"},
                {"program_id": "case-baseline-invalid"},
            ]
            write_jsonl(eligible_file, eligible_rows)
            for program_id in ("case-no-diff", "case-bug", "case-noise", "case-baseline-invalid"):
                write_json(build_dir / program_id / "build-result.json", {"status": "ok"})
            write_json(
                config_path,
                {
                    "workflow": "reporting",
                    "paths": {
                        "build_dir": str(build_dir),
                        "artifacts_dir": str(root / "artifacts"),
                        "reports_dir": str(reports_dir),
                        "eligible_file": str(eligible_file),
                        "temp_dir": str(root / "tmp"),
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
                    },
                },
            )

            no_diff_norm, no_diff_ref, no_diff_cand = self.setup_case(root, "case-no-diff", "close")
            bug_norm, bug_ref, bug_cand = self.setup_case(root, "case-bug", "openat")
            noise_norm, noise_ref, noise_cand = self.setup_case(root, "case-noise", "read")

            invalid_norm = root / "programs" / "case-baseline-invalid.syz"
            invalid_norm.parent.mkdir(parents=True, exist_ok=True)
            invalid_norm.write_text("write()\n", encoding="utf-8")
            invalid_ref = root / "runs" / "case-baseline-invalid" / "reference" / "raw-trace.json"
            write_json(
                invalid_ref,
                {
                    "program_id": "case-baseline-invalid",
                    "side": "reference",
                    "run_id": "case-baseline-invalid-ref",
                    "status": "timeout",
                    "events": [],
                    "process_exit": {"status": "timeout", "exit_code": None, "timed_out": True},
                },
            )

            results = [
                {
                    "program_id": "case-no-diff",
                    "classification": "NO_DIFF",
                    "normalized_path": str(no_diff_norm),
                    "reference_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(no_diff_ref),
                            "console_log_path": str(root / "runs" / "case-no-diff" / "reference" / "console.log"),
                        }
                    ],
                    "candidate_run": {
                        "status": "ok",
                        "runner_kind": "qemu",
                        "kernel_build": "kernel-a",
                        "trace_json_path": str(no_diff_cand),
                        "console_log_path": str(root / "runs" / "case-no-diff" / "candidate" / "console.log"),
                    },
                },
                {
                    "program_id": "case-bug",
                    "classification": "BUG_LIKELY",
                    "normalized_path": str(bug_norm),
                    "comparison": {
                        "first_divergence_index": 0,
                        "final_state_equal": True,
                        "process_exit_equal": True,
                    },
                    "reference_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(bug_ref),
                            "console_log_path": str(root / "runs" / "case-bug" / "reference" / "console.log"),
                        }
                    ],
                    "candidate_run": {
                        "status": "ok",
                        "runner_kind": "qemu",
                        "kernel_build": "kernel-a",
                        "trace_json_path": str(bug_cand),
                        "console_log_path": str(root / "runs" / "case-bug" / "candidate" / "console.log"),
                    },
                    "candidate_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(bug_cand),
                            "console_log_path": str(root / "runs" / "case-bug" / "candidate" / "console.log"),
                        }
                    ],
                },
                {
                    "program_id": "case-noise",
                    "classification": "WEAK_SPEC_OR_ENV_NOISE",
                    "normalized_path": str(noise_norm),
                    "comparison": {
                        "first_divergence_index": 0,
                        "final_state_equal": True,
                        "process_exit_equal": True,
                    },
                    "reference_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(noise_ref),
                            "console_log_path": str(root / "runs" / "case-noise" / "reference" / "console.log"),
                        }
                    ],
                    "candidate_run": {
                        "status": "ok",
                        "runner_kind": "qemu",
                        "kernel_build": "kernel-a",
                        "trace_json_path": str(noise_cand),
                        "console_log_path": str(root / "runs" / "case-noise" / "candidate" / "console.log"),
                    },
                    "candidate_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(noise_cand),
                            "console_log_path": str(root / "runs" / "case-noise" / "candidate" / "console.log"),
                        }
                    ],
                },
                {
                    "program_id": "case-baseline-invalid",
                    "classification": "BASELINE_INVALID",
                    "normalized_path": str(invalid_norm),
                    "reference_runs": [
                        {
                            "status": "timeout",
                            "trace_json_path": str(invalid_ref),
                            "console_log_path": str(root / "runs" / "case-baseline-invalid" / "reference" / "console.log"),
                        }
                    ],
                },
            ]

            args = SimpleNamespace(
                workflow="reporting",
                campaign="smoke",
                eligible_file=str(eligible_file),
                limit=None,
                jobs=None,
                candidate_batch_size=None,
                program_id=None,
                controlled_divergence=False,
            )

            os.environ["SYZABI_CONFIG_PATH"] = str(config_path)
            with patch("orchestrator.scheduler.parse_args", return_value=args), patch(
                "orchestrator.scheduler.selected_entries", return_value=eligible_rows
            ), patch("orchestrator.scheduler.schedule_entries", return_value=results):
                scheduler.main()

            summary = self.normalize_paths(
                json.loads((reports_dir / "summary.json").read_text(encoding="utf-8")),
                root,
            )
            failure_report = self.normalize_paths(
                json.loads((reports_dir / "failure-report.json").read_text(encoding="utf-8")),
                root,
            )
            divergence_index = self.normalize_paths(
                [
                    json.loads(line)
                    for line in (reports_dir / "divergence-index.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ],
                root,
            )

            self.assertEqual(summary, load_fixture_json("summary.json"))
            self.assertEqual(failure_report, load_fixture_json("failure-report.json"))
            self.assertEqual(divergence_index, load_fixture_jsonl("divergence-index.jsonl"))

    def test_scheduler_baseline_named_outputs_match_golden_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_dir = root / "build"
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "baseline_rules.json"
            reports_dir.mkdir(parents=True, exist_ok=True)

            eligible_rows = [
                {"program_id": "case-no-diff"},
                {"program_id": "case-bug"},
                {"program_id": "case-noise"},
                {"program_id": "case-baseline-invalid"},
            ]
            write_jsonl(eligible_file, eligible_rows)
            for program_id in ("case-no-diff", "case-bug", "case-noise", "case-baseline-invalid"):
                write_json(build_dir / program_id / "build-result.json", {"status": "ok"})

            baseline_cfg = json.loads((Path(__file__).resolve().parents[1] / "configs" / "baseline_rules.json").read_text(encoding="utf-8"))
            baseline_cfg["paths"]["build_dir"] = str(build_dir)
            baseline_cfg["paths"]["reports_dir"] = str(reports_dir)
            baseline_cfg["paths"]["eligible_file"] = str(eligible_file)
            write_json(config_path, baseline_cfg)

            no_diff_norm, no_diff_ref, no_diff_cand = self.setup_case(root, "case-no-diff", "close")
            bug_norm, bug_ref, bug_cand = self.setup_case(root, "case-bug", "openat")
            noise_norm, noise_ref, noise_cand = self.setup_case(root, "case-noise", "read")

            invalid_norm = root / "programs" / "case-baseline-invalid.syz"
            invalid_norm.parent.mkdir(parents=True, exist_ok=True)
            invalid_norm.write_text("write()\n", encoding="utf-8")
            invalid_ref = root / "runs" / "case-baseline-invalid" / "reference" / "raw-trace.json"
            write_json(
                invalid_ref,
                {
                    "program_id": "case-baseline-invalid",
                    "side": "reference",
                    "run_id": "case-baseline-invalid-ref",
                    "status": "timeout",
                    "events": [],
                    "process_exit": {"status": "timeout", "exit_code": None, "timed_out": True},
                },
            )

            results = [
                {
                    "program_id": "case-no-diff",
                    "classification": "NO_DIFF",
                    "normalized_path": str(no_diff_norm),
                    "reference_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(no_diff_ref),
                            "console_log_path": str(root / "runs" / "case-no-diff" / "reference" / "console.log"),
                        }
                    ],
                    "candidate_run": {
                        "status": "ok",
                        "runner_kind": "local",
                        "kernel_build": "linux-6.1",
                        "trace_json_path": str(no_diff_cand),
                        "console_log_path": str(root / "runs" / "case-no-diff" / "candidate" / "console.log"),
                    },
                },
                {
                    "program_id": "case-bug",
                    "classification": "BUG_LIKELY",
                    "normalized_path": str(bug_norm),
                    "comparison": {
                        "first_divergence_index": 0,
                        "final_state_equal": True,
                        "process_exit_equal": True,
                    },
                    "reference_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(bug_ref),
                            "console_log_path": str(root / "runs" / "case-bug" / "reference" / "console.log"),
                        }
                    ],
                    "candidate_run": {
                        "status": "ok",
                        "runner_kind": "local",
                        "kernel_build": "linux-6.1",
                        "trace_json_path": str(bug_cand),
                        "console_log_path": str(root / "runs" / "case-bug" / "candidate" / "console.log"),
                    },
                    "candidate_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(bug_cand),
                            "console_log_path": str(root / "runs" / "case-bug" / "candidate" / "console.log"),
                        }
                    ],
                },
                {
                    "program_id": "case-noise",
                    "classification": "WEAK_SPEC_OR_ENV_NOISE",
                    "normalized_path": str(noise_norm),
                    "comparison": {
                        "first_divergence_index": 0,
                        "final_state_equal": True,
                        "process_exit_equal": True,
                    },
                    "reference_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(noise_ref),
                            "console_log_path": str(root / "runs" / "case-noise" / "reference" / "console.log"),
                        }
                    ],
                    "candidate_run": {
                        "status": "ok",
                        "runner_kind": "local",
                        "kernel_build": "linux-6.1",
                        "trace_json_path": str(noise_cand),
                        "console_log_path": str(root / "runs" / "case-noise" / "candidate" / "console.log"),
                    },
                    "candidate_runs": [
                        {
                            "status": "ok",
                            "trace_json_path": str(noise_cand),
                            "console_log_path": str(root / "runs" / "case-noise" / "candidate" / "console.log"),
                        }
                    ],
                },
                {
                    "program_id": "case-baseline-invalid",
                    "classification": "BASELINE_INVALID",
                    "normalized_path": str(invalid_norm),
                    "reference_runs": [
                        {
                            "status": "timeout",
                            "trace_json_path": str(invalid_ref),
                            "console_log_path": str(root / "runs" / "case-baseline-invalid" / "reference" / "console.log"),
                        }
                    ],
                },
            ]

            args = SimpleNamespace(
                workflow="baseline",
                campaign="smoke",
                eligible_file=str(eligible_file),
                limit=None,
                jobs=None,
                candidate_batch_size=None,
                program_id=None,
                controlled_divergence=False,
            )

            os.environ["SYZABI_CONFIG_PATH"] = str(config_path)
            with patch("orchestrator.scheduler.parse_args", return_value=args), patch(
                "orchestrator.scheduler.selected_entries", return_value=eligible_rows
            ), patch("orchestrator.scheduler.schedule_entries", return_value=results):
                scheduler.main()

            summary = self.normalize_paths(
                json.loads((reports_dir / "summary.json").read_text(encoding="utf-8")),
                root,
            )
            failure_report = self.normalize_paths(
                json.loads((reports_dir / "failure-report.json").read_text(encoding="utf-8")),
                root,
            )
            divergence_index = self.normalize_paths(
                [
                    json.loads(line)
                    for line in (reports_dir / "divergence-index.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ],
                root,
            )

            self.assertEqual(summary, load_baseline_fixture_json("summary.json"))
            self.assertEqual(failure_report, load_baseline_fixture_json("failure-report.json"))
            self.assertEqual(divergence_index, load_baseline_fixture_jsonl("divergence-index.jsonl"))


if __name__ == "__main__":
    unittest.main()
