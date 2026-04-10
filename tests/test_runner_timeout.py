from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from runners.command import CommandRunner


class RunnerTimeoutTests(unittest.TestCase):
    def test_command_runner_timeout_kills_spawned_child_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pid_path = root / "child.pid"
            script = root / "spawn_child.py"
            script.write_text(
                "\n".join(
                    [
                        "import subprocess",
                        "import sys",
                        "import time",
                        "from pathlib import Path",
                        "child = subprocess.Popen(['python3', '-c', 'import time; time.sleep(60)'])",
                        "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(60)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            runner = CommandRunner({"kind": "command"})
            result = runner.run_case(
                command=["python3", str(script), str(pid_path)],
                cwd=str(root),
                env=os.environ.copy(),
                timeout_sec=1,
            )

            self.assertTrue(result.timed_out)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            time.sleep(1)
            self.assertFalse(Path(f"/proc/{child_pid}").exists())


if __name__ == "__main__":
    unittest.main()
