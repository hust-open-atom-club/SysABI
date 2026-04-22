#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.common import config, configure_runtime, resolve_repo_path
from targets.registry import get_target_adapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="tgoskits_starryos")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("preflight")
    subparsers.add_parser("healthcheck")

    campaign = subparsers.add_parser("campaign")
    campaign.add_argument("--campaign", default="smoke")
    campaign.add_argument("--eligible-file")
    campaign.add_argument("--limit", type=int)
    campaign.add_argument("--jobs", type=int, default=1)
    campaign.add_argument("--skip-build", action="store_true")
    return parser.parse_args()


def load_cfg(workflow: str) -> dict[str, object]:
    configure_runtime(workflow=workflow)
    return config()


def resolve_adapter(cfg: dict[str, object]):
    return get_target_adapter(cfg)


def campaign_preflight_payload(cfg: dict[str, object], args: argparse.Namespace | None = None) -> dict[str, object]:
    adapter = resolve_adapter(cfg)
    try:
        return adapter.prepare_campaign_assets(cfg, args)
    except adapter.runner_errors() as exc:
        raise SystemExit(str(exc))


def checked_preflight_payload(cfg: dict[str, object]) -> dict[str, object]:
    adapter = resolve_adapter(cfg)
    try:
        return adapter.preflight_payload(cfg)
    except adapter.runner_errors() as exc:
        raise SystemExit(str(exc))


def run_command(command: list[str], *, env: dict[str, str]) -> None:
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False, text=True)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def ensure_prog2c_exists(cfg: dict[str, object]) -> None:
    syzkaller_dir = resolve_repo_path(str(cfg["paths"]["syzkaller_dir"]))
    prog2c = syzkaller_dir / "bin" / "syz-prog2c"
    if prog2c.exists():
        return
    raise SystemExit(
        f"missing syz-prog2c at {prog2c}; run `make bootstrap` in {ROOT}"
    )


def healthcheck_command(workflow: str) -> list[str]:
    return ["python3", "targets/entrypoint.py", "--workflow", workflow, "--healthcheck"]


def main() -> None:
    args = parse_args()
    cfg = load_cfg(args.workflow)
    adapter = resolve_adapter(cfg)

    if args.command == "preflight":
        payload = checked_preflight_payload(cfg)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    env = os.environ.copy()
    env["SYZABI_WORKFLOW"] = args.workflow

    if args.command == "healthcheck":
        checked_preflight_payload(cfg)
        run_command(healthcheck_command(args.workflow), env=env)
        return

    if args.command == "campaign":
        campaign_preflight_payload(cfg, args)
        eligible_file = args.eligible_file or str(cfg["paths"]["eligible_file"])
        if adapter.requires_campaign_healthcheck(cfg):
            run_command(healthcheck_command(args.workflow), env=env)
        if not args.skip_build:
            ensure_prog2c_exists(cfg)
            build_cmd = [
                "python3",
                "tools/prog2c_wrap.py",
                "--workflow",
                args.workflow,
                "--eligible-file",
                eligible_file,
                "--jobs",
                str(args.jobs),
            ]
            if args.limit is not None:
                build_cmd.extend(["--limit", str(args.limit)])
            run_command(build_cmd, env=env)
        scheduler_cmd = [
            "python3",
            "orchestrator/scheduler.py",
            "--workflow",
            args.workflow,
            "--campaign",
            args.campaign,
            "--eligible-file",
            eligible_file,
            "--jobs",
            str(args.jobs),
        ]
        if args.limit is not None:
            scheduler_cmd.extend(["--limit", str(args.limit)])
        run_command(scheduler_cmd, env=env)
        return

    raise SystemExit(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
