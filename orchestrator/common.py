from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOW = "baseline"
WORKFLOW_ENV = "SYZABI_WORKFLOW"
CONFIG_PATH_ENV = "SYZABI_CONFIG_PATH"
TEMP_DIR_ENV = "SYZABI_TMPDIR"


def repo_root() -> Path:
    return ROOT


def resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


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
    return load_json(resolved_config_path(workflow=workflow, config_path=config_path))


def current_workflow(cfg: dict[str, Any] | None = None) -> str:
    payload = cfg or config()
    return str(payload.get("workflow", runtime_workflow()))


def reports_dir(cfg: dict[str, Any] | None = None) -> Path:
    payload = cfg or config()
    return resolve_repo_path(payload["paths"]["reports_dir"])


def report_path(*parts: str, cfg: dict[str, Any] | None = None) -> Path:
    return reports_dir(cfg).joinpath(*parts)


def runner_profiles(*, workflow: str | None = None, config_path: str | Path | None = None) -> dict[str, Any]:
    cfg = config(workflow=workflow, config_path=config_path)
    return load_json(cfg.get("runner_profiles_path", "configs/runner_profiles.json"))


def temp_dir(cfg: dict[str, Any] | None = None) -> Path:
    override = os.environ.get(TEMP_DIR_ENV)
    if override:
        return ensure_dir(override)
    payload = cfg or {}
    paths = payload.get("paths", {})
    selected = paths.get("temp_dir") if isinstance(paths, dict) else None
    if not selected:
        selected = "artifacts/tmp"
    return ensure_dir(selected)


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
