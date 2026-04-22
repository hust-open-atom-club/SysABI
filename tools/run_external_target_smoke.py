#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="tgoskits_arceos_smoke")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    artifact_root = ROOT / "artifacts" / "smoke" / args.workflow
    artifact_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SYZABI_CONSOLE_LOG_PATH"] = str(artifact_root / "console.log")
    env["SYZABI_RUNNER_RESULT_PATH"] = str(artifact_root / "runner-result.json")
    completed = subprocess.run(
        [sys.executable, "targets/entrypoint.py", "--workflow", args.workflow, "--healthcheck"],
        cwd=ROOT,
        env=env,
        check=False,
        text=True,
    )
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
