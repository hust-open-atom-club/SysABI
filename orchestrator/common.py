from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from core.concurrency import ConcurrencyLimiter
from core.environment import env_with_go as _env_with_go, env_with_temp as _env_with_temp, temp_dir as _temp_dir
from core.filesystem import clean_dir as _clean_dir, ensure_dir as _ensure_dir
from core.paths import PathResolver, repo_root as core_repo_root, resolve_repo_path as core_resolve_repo_path
from core.persistence import dump_json as _dump_json, dump_jsonl as _dump_jsonl, load_json as _load_json, load_jsonl as _load_jsonl, read_text as _read_text, write_text as _write_text
from core.workflow_contract import WorkflowContractError, validate_repo_workflow_payload, validate_target_config_payload
from orchestrator.legacy_compat import default_presentation, emit_deprecation_warning_once, infer_legacy_target
from runners.factory import available_runner_kinds
from targets.base import (
    LEGACY_SHARED_GUEST_SHELL_EXECUTION_MODE,
    PACKAGED_PER_CASE_EXECUTION_MODE,
    SHARED_RUNTIME_BATCH_EXECUTION_MODE,
    canonical_execution_mode,
)

_vm_concurrency_limiter: ConcurrencyLimiter | None = None


def set_vm_concurrency_limit(limit: int) -> None:
    global _vm_concurrency_limiter
    _vm_concurrency_limiter = ConcurrencyLimiter(limit)


def vm_concurrency_limiter() -> ConcurrencyLimiter | None:
    return _vm_concurrency_limiter


def vm_concurrency_semaphore() -> Any | None:
    """Deprecated: use vm_concurrency_limiter() instead."""
    limiter = _vm_concurrency_limiter
    return limiter._semaphore if limiter is not None else None


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = "baseline"
WORKFLOW_ENV = "SYZABI_WORKFLOW"
CONFIG_PATH_ENV = "SYZABI_CONFIG_PATH"


def repo_root() -> Path:
    return core_repo_root()


def resolve_repo_path(value: str | Path) -> Path:
    return core_resolve_repo_path(value)


def configure_runtime(*, workflow: str | None = None, config_path: str | Path | None = None) -> None:
    if workflow is not None:
        os.environ[WORKFLOW_ENV] = workflow
    if config_path is not None:
        os.environ[CONFIG_PATH_ENV] = str(config_path)


def runtime_workflow() -> str:
    return os.environ.get(WORKFLOW_ENV, DEFAULT_WORKFLOW)


def resolved_config_path(*, workflow: str | None = None, config_path: str | Path | None = None) -> Path:
    selected_path = config_path or os.environ.get(CONFIG_PATH_ENV)
    if selected_path:
        resolved = resolve_repo_path(selected_path)
        if resolved.name.endswith("_rules.json"):
            try:
                display_path = resolved.relative_to(repo_root())
            except ValueError:
                display_path = resolved
            emit_deprecation_warning_once(
                f"legacy-config:{resolved}",
                f"{display_path} -> use configs/workflows/{workflow or runtime_workflow()}.json instead",
            )
        return resolved
    selected_workflow = workflow or runtime_workflow()
    canonical = resolve_repo_path(f"configs/workflows/{selected_workflow}.json")
    if canonical.exists():
        return canonical
    candidate = resolve_repo_path(f"configs/{selected_workflow}_rules.json")
    if candidate.exists():
        emit_deprecation_warning_once(
            f"legacy-workflow-config:{selected_workflow}",
            f"{candidate.relative_to(repo_root())} -> add/use configs/workflows/{selected_workflow}.json",
        )
        return candidate
    if selected_workflow != DEFAULT_WORKFLOW:
        raise FileNotFoundError(f"missing config for workflow {selected_workflow}: {candidate}")
    return resolve_repo_path("configs/baseline_rules.json")


# Re-export persistence helpers for backward compatibility
load_json = _load_json
dump_json = _dump_json
dump_jsonl = _dump_jsonl
load_jsonl = _load_jsonl
read_text = _read_text
write_text = _write_text

# Re-export filesystem helpers for backward compatibility
ensure_dir = _ensure_dir
clean_dir = _clean_dir


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_runner_profiles_payload(
    payload: dict[str, Any],
    *,
    resolved_path: Path,
) -> None:
    try:
        resolved_path.relative_to(resolve_repo_path("configs"))
    except ValueError:
        return

    for role in ("reference", "candidate"):
        profile = payload.get(role)
        if not isinstance(profile, dict):
            raise WorkflowContractError(f"runner profiles missing required section: {role}")
        kind = profile.get("kind", "local")
        if not isinstance(kind, str) or not kind:
            raise WorkflowContractError(f"runner profile {role} is missing a non-empty kind")
        if kind not in available_runner_kinds():
            raise WorkflowContractError(
                f"runner profile {role} references unsupported kind {kind!r}; "
                f"supported={available_runner_kinds()!r}"
            )
    candidate = payload.get("candidate", {})
    if isinstance(candidate, dict):
        batching_mode = candidate.get("command_batching_mode")
        if batching_mode not in {
            None,
            PACKAGED_PER_CASE_EXECUTION_MODE,
            SHARED_RUNTIME_BATCH_EXECUTION_MODE,
            LEGACY_SHARED_GUEST_SHELL_EXECUTION_MODE,
        }:
            raise WorkflowContractError(f"unsupported candidate command_batching_mode: {batching_mode!r}")
        if canonical_execution_mode(str(batching_mode) if batching_mode is not None else None) == SHARED_RUNTIME_BATCH_EXECUTION_MODE and not candidate.get("batch_command"):
            raise WorkflowContractError("candidate runner profile with shared runtime batching must define batch_command")


def config(*, workflow: str | None = None, config_path: str | Path | None = None) -> dict[str, Any]:
    resolved_path = resolved_config_path(workflow=workflow, config_path=config_path)
    payload = load_json(resolved_path)
    validate_repo_workflow_payload(payload, resolved_path=resolved_path, repo_root=repo_root())
    target_name = payload.get("target")
    if not isinstance(target_name, str) or not target_name:
        if resolved_path.name.endswith("_rules.json"):
            target_name = infer_legacy_target(payload, workflow=workflow or runtime_workflow())
        else:
            target_name = "linux"
        payload["target"] = target_name
    target_config_path = payload.get("target_config_path")
    if target_config_path:
        target_config = load_json(target_config_path)
        validate_target_config_payload(target_config, workflow_payload=payload)
        payload["target_config"] = target_config
        if target_name not in payload:
            payload[target_name] = target_config
    presentation = payload.get("presentation")
    if not isinstance(presentation, dict):
        workflow_name = str(payload.get("workflow", workflow or runtime_workflow()))
        presentation = default_presentation(target=target_name, workflow=workflow_name)
        payload["presentation"] = presentation
    if resolved_path.parent.name == "workflows":
        paths = dict(payload.get("paths", {}))
        payload["paths"] = paths
        resolver = PathResolver(payload)
        paths["build_dir"] = resolver.canonical_build_dir().relative_to(repo_root()).as_posix()
        paths["artifacts_dir"] = resolver.canonical_artifacts_dir().relative_to(repo_root()).as_posix()
        paths["reports_dir"] = resolver.canonical_reports_dir().relative_to(repo_root()).as_posix()
        paths["eligible_file"] = resolver.canonical_eligible_file().relative_to(repo_root()).as_posix()
        if payload.get("capabilities", {}).get("supports_batch_execution"):
            paths["candidate_initramfs_packages_dir"] = (
                resolver.candidate_initramfs_packages_dir().relative_to(repo_root()).as_posix()
            )
        if payload.get("capabilities", {}).get("supports_preflight"):
            paths["targets_file"] = resolver.canonical_targets_file().relative_to(repo_root()).as_posix()
            paths["generated_file"] = resolver.canonical_generated_file().relative_to(repo_root()).as_posix()
            paths["static_eligible_file"] = resolver.canonical_static_eligible_file().relative_to(repo_root()).as_posix()
            paths["generated_raw_dir"] = resolver.canonical_generated_raw_dir().relative_to(repo_root()).as_posix()
            paths["generated_normalized_dir"] = resolver.canonical_generated_normalized_dir().relative_to(repo_root()).as_posix()
            paths["generated_meta_dir"] = resolver.canonical_generated_meta_dir().relative_to(repo_root()).as_posix()
            preflight = dict(payload.get("preflight", {}))
            preflight["artifact_dir"] = resolver.canonical_preflight_artifact_dir().relative_to(repo_root()).as_posix()
            preflight["source_eligible_file"] = paths["static_eligible_file"]
            payload["preflight"] = preflight
            derivation = dict(payload.get("derivation", {}))
            derivation["generated_source_eligible_file"] = paths["generated_file"]
            payload["derivation"] = derivation
        target_config = dict(payload.get("target_config", {}))
        if target_config:
            target_config["build_info_path"] = resolver.canonical_build_info_path().relative_to(repo_root()).as_posix()
            payload["target_config"] = target_config
            payload[target_name] = target_config
    return payload


def current_workflow(cfg: dict[str, Any] | None = None) -> str:
    payload = cfg or config()
    return str(payload.get("workflow", runtime_workflow()))


def reports_dir(cfg: dict[str, Any] | None = None) -> Path:
    payload = cfg or config()
    return PathResolver(payload).reports_dir()


def report_path(*parts: str, cfg: dict[str, Any] | None = None) -> Path:
    return reports_dir(cfg).joinpath(*parts)


def runner_profiles(*, workflow: str | None = None, config_path: str | Path | None = None) -> dict[str, Any]:
    cfg = config(workflow=workflow, config_path=config_path)
    path = PathResolver(cfg).runner_profiles_path()
    if path.parent == resolve_repo_path("configs") and path.name in {
        "runner_profiles.asterinas.json",
        "runner_profiles.asterinas_scml.json",
    }:
        emit_deprecation_warning_once(
            f"legacy-runner-profiles:{path}",
            f"{path.relative_to(repo_root())} -> use configs/targets/<target>/runner_profiles.<workflow>.json",
        )
    payload = load_json(path)
    validate_runner_profiles_payload(payload, resolved_path=path)
    return payload


def path_resolver(cfg: dict[str, Any] | None = None) -> PathResolver:
    return PathResolver(cfg or config())


def temp_dir(cfg: dict[str, Any] | None = None) -> Path:
    override = os.environ.get("SYZABI_TMPDIR")
    return _temp_dir(override=override, cfg=cfg)


def env_with_temp(base: dict[str, str] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, str]:
    return _env_with_temp(base, cfg=cfg)


def env_with_go() -> dict[str, str]:
    return _env_with_go(cfg=config())
