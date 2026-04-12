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
from targets.tgoskits_arceos import api as arceos_api
from targets.tgoskits_starryos import api as starry_api


RUNNER_ERRORS = (starry_api.RunnerError, arceos_api.RunnerError)


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


def preflight_payload(cfg: dict[str, object]) -> dict[str, object]:
    target = str(cfg.get("target", ""))
    if target == "tgoskits_starryos":
        return starry_api.preflight_payload(cfg)
    if target == "tgoskits_arceos":
        return arceos_api.preflight_payload(cfg)
    raise SystemExit(f"unsupported TGOSKits target for launch tool: {target}")


def checked_preflight_payload(cfg: dict[str, object]) -> dict[str, object]:
    try:
        return preflight_payload(cfg)
    except RUNNER_ERRORS as exc:
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


def enforce_campaign_scope(cfg: dict[str, object], args: argparse.Namespace) -> None:
    target = str(cfg.get("target", ""))
    if target != "tgoskits_arceos":
        return
    if args.limit != 1 or args.jobs != 1:
        raise SystemExit(
            "ArceOS experimental campaign is single-case only; use `--limit 1 --jobs 1`."
        )


def healthcheck_command(workflow: str) -> list[str]:
    return ["python3", "targets/entrypoint.py", "--workflow", workflow, "--healthcheck"]


def main() -> None:
    args = parse_args()
    cfg = load_cfg(args.workflow)

    if args.command == "preflight":
        print(json.dumps(checked_preflight_payload(cfg), ensure_ascii=False, indent=2, sort_keys=True))
        return

    env = os.environ.copy()
    env["SYZABI_WORKFLOW"] = args.workflow

    if args.command == "healthcheck":
        checked_preflight_payload(cfg)
        run_command(healthcheck_command(args.workflow), env=env)
        return

    if args.command == "campaign":
        checked_preflight_payload(cfg)
        enforce_campaign_scope(cfg, args)
        eligible_file = args.eligible_file or str(cfg["paths"]["eligible_file"])
        if str(cfg.get("target", "")) == "tgoskits_starryos":
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
