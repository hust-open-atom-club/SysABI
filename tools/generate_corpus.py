#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, dump_json, ensure_dir, env_with_temp, resolve_repo_path
from orchestrator.syzkaller import project_bin


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--output-dir", default="corpus/input/generated")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--length", type=int, default=4)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = config()
    binary = project_bin("syzabi_generate")
    if not binary.exists():
        raise SystemExit("missing build/bin/syzabi_generate, run `make bootstrap` first")

    output_dir = ensure_dir(args.output_dir)
    allow = ",".join(cfg["allowlist"]["syscalls"])
    cmd = [
        str(binary),
        "-os",
        cfg["target_os"],
        "-arch",
        cfg["arch"],
        "-output-dir",
        str(output_dir),
        "-count",
        str(args.count),
        "-seed",
        str(args.seed),
        "-length",
        str(args.length),
        "-allow",
        allow,
    ]
    subprocess.run(cmd, check=True, text=True, env=env_with_temp())
    dump_json(
        "reports/baseline/generated-corpus-summary.json",
        {
            "count": args.count,
            "output_dir": str(resolve_repo_path(args.output_dir)),
            "seed": args.seed,
            "length": args.length,
        },
    )


if __name__ == "__main__":
    main()
