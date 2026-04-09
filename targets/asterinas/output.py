from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from analyzer.schemas import validate_raw_trace


def guest_crash_detail(console_text: str) -> str | None:
    if "Printing stack trace:" in console_text:
        return "guest crashed before emitting autorun markers (kernel stack trace observed)"
    lowered = console_text.lower()
    if "panicked at" in lowered or "kernel panic" in lowered:
        return "guest crashed before emitting autorun markers (kernel panic observed)"
    return None


def write_missing_marker_crash_result(
    *,
    console_text: str,
    raw_trace_path: Path,
    external_state_path: Path,
    kernel_build: str,
    hooks: Any,
) -> bool:
    detail = guest_crash_detail(console_text)
    if detail is None:
        return False
    if not external_state_path.exists():
        hooks.dump_json(external_state_path, {"files": []})
    raw_trace = {
        "program_id": os.environ.get("SYZABI_PROGRAM_ID", "unknown"),
        "side": os.environ.get("SYZABI_SIDE", "candidate"),
        "run_id": os.environ.get("SYZABI_RUN_ID", "unknown"),
        "status": "crash",
        "events": [],
        "process_exit": {"status": "crash", "exit_code": None, "timed_out": False},
    }
    validate_raw_trace(raw_trace)
    hooks.dump_json(raw_trace_path, raw_trace)
    hooks.write_runner_result(
        {
            "status": "crash",
            "exit_code": None,
            "status_detail": detail,
            "kernel_build": kernel_build,
        }
    )
    return True
