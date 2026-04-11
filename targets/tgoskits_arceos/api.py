#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.common import config, configure_runtime, dump_json, resolve_repo_path


class RunnerError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode", default=os.environ.get("SYZABI_TGOSKITS_ARCEOS_MODE", "smoke-qemu"))
    return parser.parse_args()


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def runner_result_path() -> Path | None:
    return env_path("SYZABI_RUNNER_RESULT_PATH")


def write_runner_result(payload: dict[str, object]) -> None:
    path = runner_result_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)


def read_workflow_config() -> dict[str, Any]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "tgoskits_arceos_smoke")
    configure_runtime(workflow=workflow)
    cfg = config()
    if cfg.get("target") != "tgoskits_arceos":
        raise RunnerError(f"tgoskits_arceos entrypoint requires a tgoskits_arceos workflow, got {cfg.get('workflow')}")
    return cfg


def target_config(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = cfg.get("target_config")
    if not isinstance(payload, dict):
        raise RunnerError("tgoskits_arceos workflow is missing target_config")
    return payload


def repo_dir(cfg: dict[str, Any]) -> Path:
    env_name = str(target_config(cfg).get("repo_dir_env", "SYZABI_TGOSKITS_DIR"))
    selected = os.environ.get(env_name, str(target_config(cfg).get("repo_dir", "")))
    if not selected:
        raise RunnerError(f"missing TGOSKits workspace; set {env_name} or configure target_config.repo_dir")
    path = Path(selected).expanduser()
    if not path.exists():
        raise RunnerError(f"configured TGOSKits workspace does not exist: {path}")
    return path


def require_feature_flag(cfg: dict[str, Any]) -> None:
    feature_flag_env = str(target_config(cfg).get("feature_flag_env", ""))
    if feature_flag_env and os.environ.get(feature_flag_env) != "1":
        raise RunnerError(f"set {feature_flag_env}=1 to enable TGOSKits external target workflows")


def prepare_target(cfg: dict[str, Any]) -> str:
    require_feature_flag(cfg)
    revision = str(target_config(cfg).get("revision", ""))
    if not revision:
        raise RunnerError("target_config.revision must pin the TGOSKits checkout")
    result = subprocess.run(
        ["git", "-C", str(repo_dir(cfg)), "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or result.stdout.strip() != revision:
        raise RunnerError("TGOSKits revision mismatch for ArceOS smoke target")
    for command in target_config(cfg).get("prepare_commands", []):
        completed = subprocess.run(command, cwd=str(repo_dir(cfg)), check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or "ArceOS prepare command failed")
    dump_json(
        resolve_repo_path(target_config(cfg)["build_info_path"]),
        {"target": "tgoskits_arceos", "revision": revision, "mode": "smoke-only"},
    )
    return f"tgoskits-arceos@{revision[:12]}"


def healthcheck(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    command = list(target_config(cfg).get("healthcheck_command", []))
    completed = subprocess.run(command, cwd=str(repo_dir(cfg)), check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or "ArceOS smoke healthcheck failed")
    console_path = env_path("SYZABI_CONSOLE_LOG_PATH")
    if console_path is not None:
        console_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": label})


def run_case(args: argparse.Namespace) -> None:
    raise RunnerError(
        "ArceOS differential replay is intentionally gated: use the smoke healthcheck path and see docs/architecture/tgoskits-arceos-decision.md"
    )


def run_batch(args: argparse.Namespace) -> None:
    raise RunnerError("ArceOS differential replay does not support batch execution")


def main() -> None:
    args = parse_args()
    try:
        if args.healthcheck:
            healthcheck(args)
            return
        run_case(args)
    except RunnerError as exc:
        write_runner_result({"status": "infra_error", "exit_code": None, "detail": str(exc), "kernel_build": "unknown"})
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
