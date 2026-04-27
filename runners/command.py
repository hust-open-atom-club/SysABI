from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Any

from core.constants import ExecutionStatus
from runners.common import RunnerExecution


@dataclass(slots=True)
class CommandRunner:
    profile: dict[str, Any]

    def prepare(self, **kwargs: Any) -> None:
        return None

    def _tools_needed_by_profile(self) -> set[str]:
        """Infer required tools from the runner profile commands."""
        needed = {"python3"}
        profile_text = " ".join(
            str(v)
            for key in ("command", "batch_command", "kernel_build_command")
            for v in ([self.profile.get(key)] if not isinstance(self.profile.get(key), list) else self.profile.get(key))
            if v is not None
        )
        if "docker" in profile_text:
            needed.add("docker")
        if "qemu-system-x86_64" in profile_text:
            needed.add("qemu-system-x86_64")
        if "qemu-system-riscv64" in profile_text:
            needed.add("qemu-system-riscv64")
        return needed

    def healthcheck(self, **kwargs: Any) -> dict[str, Any]:
        checks = []
        for tool in sorted(self._tools_needed_by_profile()):
            path = shutil.which(tool)
            checks.append({"tool": tool, "available": path is not None, "path": path})
        all_ok = all(c["available"] for c in checks)
        return {
            "status": "ok" if all_ok else "missing_tools",
            "checks": checks,
        }

    def _cleanup_process(self, process: subprocess.Popen[Any]) -> None:
        """Kill process group and ensure cleanup of child processes."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError, OSError):
            pass
        # Cleanup any remaining children
        psutil = None
        try:
            import psutil as _psutil
            psutil = _psutil
        except ImportError:
            pass
        if psutil is None:
            return
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                    child.wait(timeout=2)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.TimeoutExpired):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def _classify_returncode(self, returncode: int | None, stdout: str, stderr: str) -> str | None:
        """Classify returncode as ok, candidate_bug, or infra_error."""
        if returncode == 0:
            return ExecutionStatus.OK
        if returncode is None:
            return ExecutionStatus.INFRA_ERROR
        # Check for kernel panic indicators in output
        combined = (stdout or "") + (stderr or "")
        panic_indicators = [
            "panicked at",
            "Printing stack trace:",
            "kernel panic",
            "Kernel panic",
            "segfault",
            "SIGSEGV",
            "SIGILL",
            "SIGABRT",
        ]
        for indicator in panic_indicators:
            if indicator in combined:
                return ExecutionStatus.CANDIDATE_BUG
        return ExecutionStatus.INFRA_ERROR

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
            status = self._classify_returncode(process.returncode, stdout, stderr)
            return RunnerExecution(
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                status=status,
            )
        except subprocess.TimeoutExpired as exc:
            self._cleanup_process(process)
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
