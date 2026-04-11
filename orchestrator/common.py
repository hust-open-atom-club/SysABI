from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from core.paths import PathResolver, repo_root as core_repo_root, resolve_repo_path as core_resolve_repo_path
from core.workflow_contract import WorkflowContractError, validate_repo_workflow_payload, validate_target_config_payload
from orchestrator.legacy_compat import default_presentation, emit_deprecation_warning_once, infer_legacy_target
from runners.factory import available_runner_kinds


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = "baseline"
WORKFLOW_ENV = "SYZABI_WORKFLOW"
CONFIG_PATH_ENV = "SYZABI_CONFIG_PATH"
TEMP_DIR_ENV = "SYZABI_TMPDIR"


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


def load_json(path: str | Path) -> dict[str, Any]:
    with resolve_repo_path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json(path: str | Path, payload: Any) -> None:
    destination = resolve_repo_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def dump_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> None:
    destination = resolve_repo_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with resolve_repo_path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(path: str | Path) -> Path:
    resolved = resolve_repo_path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def clean_dir(path: str | Path) -> Path:
    resolved = resolve_repo_path(path)
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def read_text(path: str | Path) -> str:
    with resolve_repo_path(path).open("r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path: str | Path, content: str) -> None:
    destination = resolve_repo_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        handle.write(content)


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
        if batching_mode == "shared_guest_shell" and not candidate.get("batch_command"):
            raise WorkflowContractError("candidate runner profile with shared_guest_shell batching must define batch_command")


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
    override = os.environ.get(TEMP_DIR_ENV)
    if override:
        return ensure_dir(override)
    payload = cfg or config()
    return ensure_dir(path_resolver(payload).temp_dir())


def env_with_temp(base: dict[str, str] | None = None, cfg: dict[str, Any] | None = None) -> dict[str, str]:
    env = dict(base) if base is not None else os.environ.copy()
    env["TMPDIR"] = str(temp_dir(cfg))
    return env


def env_with_go() -> dict[str, str]:
    cfg = config()
    env = env_with_temp(cfg=cfg)
    go_root = resolve_repo_path(cfg["paths"]["go_root"])
    env["GOROOT"] = str(go_root)
    env["PATH"] = f"{go_root / 'bin'}:{env.get('PATH', '')}"
    return env
