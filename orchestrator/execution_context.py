from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Canonical execution context for a single testcase run.

    Replaces the scattered 13-parameter ``execution_context`` dict to
    improve readability and reduce call-site errors.
    """

    program_id: str
    side: str
    run_id: str
    timeout_sec: int
    sandbox_root: Path
    artifact_root: Path
    binary_path: Path
    stdout_path: Path
    stderr_path: Path
    console_path: Path
    events_path: Path
    raw_trace_path: Path
    external_state_path: Path
    runner_result_path: Path
    batch_manifest_path: Path | None = None

    def to_env_dict(self) -> dict[str, str]:
        """Export the context as runner environment variables."""
        result: dict[str, str] = {
            "SYZABI_PROGRAM_ID": self.program_id,
            "SYZABI_RUN_ID": self.run_id,
            "SYZABI_TIMEOUT_SEC": str(self.timeout_sec),
            "SYZABI_WORK_DIR": str(self.sandbox_root),
            "SYZABI_ARTIFACT_ROOT": str(self.artifact_root),
            "SYZABI_BINARY_PATH": str(self.binary_path),
            "SYZABI_STDOUT_PATH": str(self.stdout_path),
            "SYZABI_STDERR_PATH": str(self.stderr_path),
            "SYZABI_CONSOLE_LOG_PATH": str(self.console_path),
            "SYZABI_EVENTS_PATH": str(self.events_path),
            "SYZABI_RAW_TRACE_PATH": str(self.raw_trace_path),
            "SYZABI_EXTERNAL_STATE_PATH": str(self.external_state_path),
            "SYZABI_RUNNER_RESULT_PATH": str(self.runner_result_path),
        }
        if self.batch_manifest_path is not None:
            result["SYZABI_BATCH_MANIFEST_PATH"] = str(self.batch_manifest_path)
        return result

    def to_command_context(self) -> dict[str, str]:
        """Export the context as command-template substitution variables."""
        from core.paths import repo_root as _repo_root

        return {
            "program_id": self.program_id,
            "side": self.side,
            "run_id": self.run_id,
            "repo_root": str(_repo_root()),
            "timeout_sec": str(self.timeout_sec),
            "sandbox_root": str(self.sandbox_root),
            "artifact_root": str(self.artifact_root),
            "binary_path": str(self.binary_path),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "console_path": str(self.console_path),
            "events_path": str(self.events_path),
            "raw_trace_path": str(self.raw_trace_path),
            "external_state_path": str(self.external_state_path),
            "runner_result_path": str(self.runner_result_path),
            "batch_manifest_path": str(self.batch_manifest_path) if self.batch_manifest_path is not None else "",
        }
