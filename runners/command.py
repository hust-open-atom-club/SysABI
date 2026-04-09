from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any

from runners.common import RunnerExecution


@dataclass(slots=True)
class CommandRunner:
    profile: dict[str, Any]

    def prepare(self, **kwargs: Any) -> None:
        return None

    def healthcheck(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "unknown"}

    def run_case(
        self,
        *,
        command: list[str],
        cwd: str,
        env: dict[str, str],
        timeout_sec: int,
    ) -> RunnerExecution:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
            return RunnerExecution(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        except subprocess.TimeoutExpired as exc:
            return RunnerExecution(
                returncode=None,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                timed_out=True,
            )
        except OSError as exc:
            return RunnerExecution(
                returncode=None,
                stdout="",
                stderr=str(exc),
                os_error=str(exc),
            )

    def run_batch(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("batch execution is not implemented on CommandRunner yet")

    def collect_outputs(self, **kwargs: Any) -> dict[str, Any]:
        return {}
