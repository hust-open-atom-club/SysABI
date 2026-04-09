#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--key", required=True, help="Dot path within workflow config, e.g. paths.eligible_file")
    return parser.parse_args()


def nested_value(payload: dict[str, Any], key: str) -> Any:
    current: Any = payload
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"missing workflow config key: {key}")
        current = current[part]
    return current


def main() -> None:
    args = parse_args()
    value = nested_value(config(workflow=args.workflow), args.key)
    if isinstance(value, bool):
        print("true" if value else "false")
        return
    if not isinstance(value, (str, int, float)):
        raise SystemExit(f"workflow config key is not printable scalar: {args.key}")
    print(value)


if __name__ == "__main__":
    main()
