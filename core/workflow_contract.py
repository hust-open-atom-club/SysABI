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

REQUIRED_TOP_LEVEL_MAPPING_KEYS = (
    "capabilities",
    "parallel",
    "presentation",
    "stability",
    "thresholds",
)

ALLOWED_TRACE_EVENT_TRANSPORTS = {"file", "stdout"}

TARGET_REQUIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "tgoskits_starryos": (
        "default_mode",
        "revision",
        "repo_dir_env",
        "supported_arches",
        "toolchain_probes",
        "workspace_subdir",
        "disk_image_path",
        "guest_binary_path",
        "shell_prompt",
        "shell_launch_command",
        "healthcheck_shell_command",
        "prepare_commands",
        "trace_marker_prefix",
        "feature_flag_env",
    ),
    "tgoskits_arceos": (
        "default_mode",
        "revision",
        "repo_dir_env",
        "supported_targets",
        "toolchain_probes",
        "prepare_commands",
        "healthcheck_command",
        "feature_flag_env",
    ),
}


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


def _require_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise WorkflowContractError(f"workflow field {key} must be a mapping")
    return value


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
        for key in REQUIRED_TOP_LEVEL_MAPPING_KEYS:
            _require_mapping(payload, key)
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


def validate_target_config_payload(
    target_config: dict[str, Any],
    *,
    workflow_payload: dict[str, Any],
) -> None:
    target = str(workflow_payload.get("target", ""))
    required = TARGET_REQUIRED_CONFIG_KEYS.get(target, ())
    for key in required:
        if key not in target_config:
            raise WorkflowContractError(f"target_config for {target} is missing required key: {key}")
        value = target_config.get(key)
        if value in (None, ""):
            raise WorkflowContractError(f"target_config for {target} is missing required key: {key}")

    if target == "tgoskits_starryos":
        supported_arches = target_config.get("supported_arches", [])
        if str(workflow_payload.get("arch", "")) not in {str(item) for item in supported_arches}:
            raise WorkflowContractError(
                f"workflow arch={workflow_payload.get('arch')!r} is not listed in target_config.supported_arches"
            )
