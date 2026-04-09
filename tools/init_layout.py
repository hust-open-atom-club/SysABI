#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, configure_runtime, ensure_dir, runner_profiles


def parse_workflow() -> str:
    if len(sys.argv) == 3 and sys.argv[1] == "--workflow":
        return sys.argv[2]
    return "baseline"


def main() -> None:
    configure_runtime(workflow=parse_workflow())
    cfg = config()
    for path in cfg["paths"].values():
        if isinstance(path, str) and not path.endswith(".jsonl"):
            ensure_dir(path)
    ensure_dir("build/bin")
    ensure_dir("corpus/input/generated")
    for profile in runner_profiles().values():
        if not isinstance(profile, dict):
            continue
        work_root = profile.get("work_root")
        if work_root:
            ensure_dir(work_root)


if __name__ == "__main__":
    main()
