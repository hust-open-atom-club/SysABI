from __future__ import annotations

import os
import signal
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
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout_sec)
            return RunnerExecution(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                process.kill()
            stdout, stderr = process.communicate()
            return RunnerExecution(
                returncode=None,
                stdout=stdout or exc.stdout or "",
                stderr=stderr or exc.stderr or "",
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
