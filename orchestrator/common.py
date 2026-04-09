from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from core.paths import PathResolver, repo_root as core_repo_root, resolve_repo_path as core_resolve_repo_path


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
        return resolve_repo_path(selected_path)
    selected_workflow = workflow or runtime_workflow()
    canonical = resolve_repo_path(f"configs/workflows/{selected_workflow}.json")
    if canonical.exists():
        return canonical
    candidate = resolve_repo_path(f"configs/{selected_workflow}_rules.json")
    if candidate.exists():
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


def config(*, workflow: str | None = None, config_path: str | Path | None = None) -> dict[str, Any]:
    resolved_path = resolved_config_path(workflow=workflow, config_path=config_path)
    payload = load_json(resolved_path)
    target_name = payload.get("target")
    if not isinstance(target_name, str) or not target_name:
        if resolved_path.name.endswith("_rules.json"):
            inferred_workflow = str(payload.get("workflow", workflow or runtime_workflow()))
            if inferred_workflow.startswith("asterinas"):
                target_name = "asterinas"
            else:
                target_name = "linux"
        else:
            target_name = "linux"
        payload["target"] = target_name
    target_config_path = payload.get("target_config_path")
    if target_config_path:
        target_config = load_json(target_config_path)
        payload["target_config"] = target_config
        if target_name not in payload:
            payload[target_name] = target_config
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
        derivation = dict(payload.get("derivation", {}))
        if target_name != "linux":
            derivation["source_eligible_file"] = "eligible_programs/targets/linux/baseline/default.jsonl"
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
    return load_json(PathResolver(cfg).runner_profiles_path())


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
