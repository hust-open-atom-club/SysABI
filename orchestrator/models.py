from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ProgramMeta:
    program_id: str
    source: str
    target_os: str
    arch: str
    syscall_list: list[str]
    full_syscall_list: list[str]
    resource_classes: list[str]
    uses_pseudo_syscalls: bool
    uses_threading_sensitive_features: bool
    original_path: str
    normalized_path: str = ""
    raw_path: str = ""
    call_count: int = 0
    duplicate_inputs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EligibleProgram:
    program_id: str
    workflow: str
    reason: list[str]
    normalized_path: str
    meta_path: str
    source_workflow: str = ""
    source_program_id: str = ""
    scml_preflight_status: str = "not_run"
    scml_rejection_reasons: list[str] = field(default_factory=list)
    scml_trace_log_path: str = ""
    scml_sctrace_output_path: str = ""
    scml_preflight_run_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunResult:
    program_id: str
    side: str
    status: str
    exit_code: int | None
    stdout_path: str
    stderr_path: str
    console_log_path: str
    trace_json_path: str | None
    external_state_path: str | None
    elapsed_ms: int
    role: str
    snapshot_id: str
    kernel_build: str
    run_id: str
    status_detail: str | None = None
    runner_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
