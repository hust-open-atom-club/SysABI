from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RunnerExecution:
    returncode: int | None
    stdout: str | bytes
    stderr: str | bytes
    timed_out: bool = False
    os_error: str | None = None
    status: str | None = None
