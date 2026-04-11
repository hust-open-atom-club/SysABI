#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.common import config, configure_runtime
from targets.registry import get_target_adapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--batch-manifest")
    parser.add_argument("--work-dir")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode")
    parser.add_argument("--workflow", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.workflow:
        configure_runtime(workflow=args.workflow)
    cfg = config()
    adapter = get_target_adapter(cfg)
    if args.healthcheck:
        adapter.healthcheck(args)
        return
    if args.batch_manifest:
        adapter.run_batch(args)
        return
    adapter.run_case(args)


if __name__ == "__main__":
    main()
