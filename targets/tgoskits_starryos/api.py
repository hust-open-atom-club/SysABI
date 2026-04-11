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

from orchestrator.common import config, configure_runtime, dump_json


class RunnerError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--work-dir")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode", default=os.environ.get("SYZABI_TGOSKITS_STARRY_MODE", "host-direct"))
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
    workflow = os.environ.get("SYZABI_WORKFLOW", "tgoskits_starryos")
    configure_runtime(workflow=workflow)
    cfg = config()
    if cfg.get("target") != "tgoskits_starryos":
        raise RunnerError(
            f"tgoskits_starryos entrypoint requires a tgoskits_starryos workflow, got {cfg.get('workflow')}"
        )
    return cfg


def target_config(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = cfg.get("target_config")
    if not isinstance(payload, dict):
        raise RunnerError("tgoskits_starryos workflow is missing target_config")
    return payload


def repo_dir(cfg: dict[str, Any]) -> Path:
    target_cfg = target_config(cfg)
    env_name = str(target_cfg.get("repo_dir_env", "SYZABI_TGOSKITS_DIR"))
    override = os.environ.get(env_name)
    selected = override if override else str(target_cfg.get("repo_dir", ""))
    if not selected:
        raise RunnerError(
            f"missing TGOSKits workspace; set {env_name} or configure target_config.repo_dir for workflow {cfg['workflow']}"
        )
    path = Path(selected).expanduser()
    if not path.exists():
        raise RunnerError(f"configured TGOSKits workspace does not exist: {path}")
    return path


def command_values(cfg: dict[str, Any], *, binary_path: str | None = None, work_dir: str | None = None) -> dict[str, str]:
    workspace = repo_dir(cfg)
    return {
        "arch": str(cfg.get("arch", "riscv64")),
        "binary_path": binary_path or "",
        "repo_dir": str(workspace),
        "work_dir": work_dir or "",
        "workflow": str(cfg.get("workflow", "tgoskits_starryos")),
    }


def resolve_command(template: object, values: dict[str, str]) -> list[str]:
    if isinstance(template, str):
        return [part.format(**values) for part in template.split()]
    if isinstance(template, list):
        return [str(part).format(**values) for part in template]
    raise RunnerError(f"unsupported command template type: {type(template)!r}")


def required_command(cfg: dict[str, Any], key: str, *, binary_path: str | None = None, work_dir: str | None = None) -> list[str]:
    target_cfg = target_config(cfg)
    template = target_cfg.get(key)
    if not template:
        raise RunnerError(f"target config is missing {key}")
    return resolve_command(template, command_values(cfg, binary_path=binary_path, work_dir=work_dir))


def run_subprocess(command: list[str], *, cwd: Path, timeout_sec: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=False,
        text=True,
        timeout=timeout_sec,
    )


def prepare_target(cfg: dict[str, Any]) -> str:
    target_cfg = target_config(cfg)
    workspace = repo_dir(cfg)
    timeout_sec = int(target_cfg.get("prepare_timeout_sec", 1800))
    prepare_commands = target_cfg.get("prepare_commands", [])
    if not isinstance(prepare_commands, list):
        raise RunnerError("target_config.prepare_commands must be a list")
    values = command_values(cfg)
    for template in prepare_commands:
        command = resolve_command(template, values)
        completed = run_subprocess(command, cwd=workspace, timeout_sec=timeout_sec)
        if completed.returncode != 0:
            raise RunnerError(f"prepare command failed: {' '.join(command)}")
    return str(target_cfg.get("runner_label", "tgoskits-starryos"))


def healthcheck(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    command = required_command(cfg, "healthcheck_command", work_dir=args.work_dir)
    completed = run_subprocess(command, cwd=repo_dir(cfg), timeout_sec=int(target_config(cfg).get("run_timeout_sec", 300)))
    if completed.returncode != 0:
        raise RunnerError(f"healthcheck failed: {' '.join(command)}")
    write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": label})


def run_case(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    command = required_command(cfg, "run_command", binary_path=args.binary, work_dir=args.work_dir)
    completed = run_subprocess(command, cwd=repo_dir(cfg), timeout_sec=int(target_config(cfg).get("run_timeout_sec", 300)))
    write_runner_result(
        {
            "status": "ok" if completed.returncode == 0 else "infra_error",
            "exit_code": completed.returncode,
            "kernel_build": label,
        }
    )
    raise SystemExit(completed.returncode)


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
