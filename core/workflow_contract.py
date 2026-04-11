from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkflowContractError(ValueError):
    """Raised when a repo-owned workflow config violates the canonical contract."""


REQUIRED_PATH_KEYS = (
    "artifacts_dir",
    "build_dir",
    "eligible_file",
    "reports_dir",
    "temp_dir",
)

ALLOWED_TRACE_EVENT_TRANSPORTS = {"file", "stdout"}


def is_repo_owned_canonical_workflow(*, resolved_path: Path, repo_root: Path) -> bool:
    canonical_root = repo_root / "configs" / "workflows"
    try:
        resolved_path.relative_to(canonical_root)
    except ValueError:
        return False
    return True


def trace_events_transport(cfg: dict[str, Any]) -> str:
    trace_cfg = cfg.get("trace", {})
    if not isinstance(trace_cfg, dict):
        return "file"
    transport = str(trace_cfg.get("events_transport", "file") or "file")
    if transport not in ALLOWED_TRACE_EVENT_TRANSPORTS:
        raise WorkflowContractError(
            f"unsupported trace.events_transport={transport!r}; expected one of "
            f"{sorted(ALLOWED_TRACE_EVENT_TRANSPORTS)!r}"
        )
    return transport


def _require_non_empty_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise WorkflowContractError(f"missing required workflow field: {key}")
    return value


def _require_paths(payload: dict[str, Any]) -> dict[str, Any]:
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        raise WorkflowContractError("workflow paths must be a mapping")
    for key in REQUIRED_PATH_KEYS:
        value = paths.get(key)
        if not isinstance(value, str) or not value:
            raise WorkflowContractError(f"workflow paths missing required key: {key}")
    return paths


@dataclass(frozen=True, slots=True)
class WorkflowPathsContract:
    artifacts_dir: str
    build_dir: str
    eligible_file: str
    reports_dir: str
    temp_dir: str


@dataclass(frozen=True, slots=True)
class WorkflowContract:
    workflow: str
    target: str
    arch: str
    runner_profiles_path: str
    target_config_path: str
    trace_events_transport: str
    paths: WorkflowPathsContract

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> WorkflowContract:
        workflow = _require_non_empty_str(payload, "workflow")
        target = _require_non_empty_str(payload, "target")
        arch = _require_non_empty_str(payload, "arch")
        runner_profiles_path = _require_non_empty_str(payload, "runner_profiles_path")
        target_config_path = _require_non_empty_str(payload, "target_config_path")
        paths = _require_paths(payload)
        return cls(
            workflow=workflow,
            target=target,
            arch=arch,
            runner_profiles_path=runner_profiles_path,
            target_config_path=target_config_path,
            trace_events_transport=trace_events_transport(payload),
            paths=WorkflowPathsContract(
                artifacts_dir=str(paths["artifacts_dir"]),
                build_dir=str(paths["build_dir"]),
                eligible_file=str(paths["eligible_file"]),
                reports_dir=str(paths["reports_dir"]),
                temp_dir=str(paths["temp_dir"]),
            ),
        )


def validate_repo_workflow_payload(
    payload: dict[str, Any],
    *,
    resolved_path: Path,
    repo_root: Path,
) -> WorkflowContract | None:
    if not is_repo_owned_canonical_workflow(resolved_path=resolved_path, repo_root=repo_root):
        return None
    return WorkflowContract.from_payload(payload)
