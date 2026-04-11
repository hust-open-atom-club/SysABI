from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from targets.tgoskits_arceos import api as arceos_api
from targets.tgoskits_starryos import api as starry_api


def init_git_repo(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "codex@example.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Codex"], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("fake tgoskits\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class TGOSKitsTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_workflow = os.environ.get("SYZABI_WORKFLOW")
        self.previous_config = os.environ.get("SYZABI_CONFIG_PATH")
        self.previous_tgoskits = os.environ.get("SYZABI_TGOSKITS_DIR")
        self.previous_flag = os.environ.get("SYZABI_ENABLE_TGOSKITS")
        self.previous_path = os.environ.get("PATH")

    def tearDown(self) -> None:
        if self.previous_workflow is None:
            os.environ.pop("SYZABI_WORKFLOW", None)
        else:
            os.environ["SYZABI_WORKFLOW"] = self.previous_workflow
        if self.previous_config is None:
            os.environ.pop("SYZABI_CONFIG_PATH", None)
        else:
            os.environ["SYZABI_CONFIG_PATH"] = self.previous_config
        if self.previous_tgoskits is None:
            os.environ.pop("SYZABI_TGOSKITS_DIR", None)
        else:
            os.environ["SYZABI_TGOSKITS_DIR"] = self.previous_tgoskits
        if self.previous_flag is None:
            os.environ.pop("SYZABI_ENABLE_TGOSKITS", None)
        else:
            os.environ["SYZABI_ENABLE_TGOSKITS"] = self.previous_flag
        if self.previous_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self.previous_path

    def make_fake_starry_workspace(self, root: Path) -> tuple[Path, str]:
        repo = root / "tgoskits"
        workspace = repo / "os" / "StarryOS"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "make").mkdir(parents=True, exist_ok=True)
        revision = init_git_repo(repo)

        fake_bin = root / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"

        write_executable(
            fake_bin / "debugfs",
            "#!/bin/sh\nexit 0\n",
        )
        write_executable(
            fake_bin / "qemu-system-riscv64",
            "#!/bin/sh\nexit 0\n",
        )
        write_executable(
            fake_bin / "riscv64-linux-musl-gcc",
            "#!/bin/sh\nout=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = \"-o\" ]; then shift; out=\"$1\"; fi\n  shift\n done\n[ -n \"$out\" ] && : > \"$out\"\nexit 0\n",
        )
        write_executable(
            fake_bin / "make",
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import re
                import socket
                import sys
                from pathlib import Path

                cwd = Path.cwd()
                args = sys.argv[1:]
                disk = cwd / "make" / "disk.img"
                if "rootfs" in args or "build" in args:
                    disk.parent.mkdir(parents=True, exist_ok=True)
                    disk.write_text("disk", encoding="utf-8")
                    raise SystemExit(0)
                if "justrun" in args:
                    joined = " ".join(args)
                    tcp = re.search(r"tcp::(\\d+),server=on", joined)
                    unix = re.search(r"unix:([^,]+),server=on", joined)
                    if tcp:
                        server = socket.create_server(("127.0.0.1", int(tcp.group(1))), reuse_port=False)
                    elif unix:
                        path = unix.group(1)
                        Path(path).unlink(missing_ok=True)
                        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        server.bind(path)
                        server.listen(1)
                    else:
                        raise SystemExit("missing serial endpoint")
                    print("QEMU waiting for connection", file=sys.stderr, flush=True)
                    conn, _ = server.accept()
                    with conn:
                        conn.sendall(b"starry:~#")
                        buffer = ""
                        while True:
                            data = conn.recv(4096)
                            if not data:
                                break
                            buffer += data.decode("utf-8", errors="ignore")
                            while "\\n" in buffer or "\\r" in buffer:
                                line = buffer.replace("\\r", "\\n").split("\\n", 1)[0]
                                buffer = buffer.replace("\\r", "\\n").split("\\n", 1)[1] if "\\n" in buffer.replace("\\r", "\\n") else ""
                                if not line.strip():
                                    continue
                                begin = ""
                                exit_marker = ""
                                parts = [part.strip() for part in line.split(";") if part.strip()]
                                if parts and parts[0].startswith("echo "):
                                    begin = parts[0][5:]
                                if parts and parts[-1].startswith("echo ") and parts[-1].endswith("$?"):
                                    exit_marker = parts[-1][5:-2]
                                payload = []
                                if begin:
                                    payload.append(begin)
                                if "__SYZABI_HEALTHCHECK_OK__" in line:
                                    payload.append("/fake/workspace")
                                    payload.append("__SYZABI_HEALTHCHECK_OK__")
                                if ("testcase.candidate" in line or "case-missing" in line) and "missing-trace" not in line and "case-missing" not in line:
                                    payload.append('{starry_api.TRACE_EVENT_STDOUT_PREFIX}' + '{{"args":[3,0,0,0,0,0],"end_ns":2,"errno":0,"event_index":0,"outputs":[],"return_value":0,"side":"candidate","start_ns":1,"syscall_name":"close","syscall_number":3}}')
                                if exit_marker:
                                    payload.append(exit_marker + "0")
                                payload.append("starry:~#")
                                conn.sendall(("\\n".join(payload)).encode("utf-8"))
                    raise SystemExit(0)
                raise SystemExit(0)
                """
            ),
        )
        return repo, revision

    def test_starry_healthcheck_run_and_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo, revision = self.make_fake_starry_workspace(root)
            runner_result = root / "runner-result.json"
            console_log = root / "console.log"
            raw_trace = root / "raw-trace.json"
            external_state = root / "external-state.json"
            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")

            config_path = root / "starry.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_starry",
                        "target": "tgoskits_starryos",
                        "arch": "riscv64",
                        "runner_profiles_path": "configs/targets/tgoskits_starryos/runner_profiles.tgoskits_starryos.json",
                        "target_config_path": str(root / "starry-target.json"),
                        "paths": {
                            "build_dir": str(root / "build"),
                            "artifacts_dir": str(root / "artifacts"),
                            "reports_dir": str(root / "reports"),
                            "eligible_file": str(root / "eligible.jsonl"),
                            "temp_dir": str(root / "tmp"),
                        },
                        "normalization": {"preview_bytes": 32},
                        "classification": {"no_diff": "NO_DIFF"},
                        "thresholds": {"smoke": {}, "signoff": {}},
                        "trace": {"events_transport": "stdout"},
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            (root / "starry-target.json").write_text(
                json.dumps(
                    {
                        "build_info_path": str(root / "build-info.json"),
                        "default_mode": "shell-qemu",
                        "revision": revision,
                        "repo_dir_env": "SYZABI_TGOSKITS_DIR",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "supported_arches": ["riscv64"],
                        "toolchain_probes": ["make", "debugfs", "qemu-system-riscv64", "riscv64-linux-musl-gcc"],
                        "workspace_subdir": "os/StarryOS",
                        "disk_image_path": "os/StarryOS/make/disk.img",
                        "guest_binary_path": "/bin/testcase.candidate.bin",
                        "shell_prompt": "starry:~#",
                        "serial_transport": "unix",
                        "trace_marker_prefix": starry_api.TRACE_EVENT_STDOUT_PREFIX,
                        "prepare_timeout_sec": 60,
                        "boot_timeout_sec": 10,
                        "command_timeout_sec": 10,
                        "prepare_commands": [["make", "ARCH={arch}", "rootfs"], ["make", "ARCH={arch}", "build"]],
                        "shell_launch_command": ["make", "ARCH={arch}", "justrun", "QEMU_ARGS=-monitor none -serial unix:{serial_socket_path},server=on"],
                        "healthcheck_shell_command": "pwd && echo __SYZABI_HEALTHCHECK_OK__",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            os.environ["SYZABI_TGOSKITS_DIR"] = str(repo)
            os.environ["SYZABI_ENABLE_TGOSKITS"] = "1"
            os.environ["SYZABI_CONFIG_PATH"] = str(config_path)
            os.environ["SYZABI_WORKFLOW"] = "fake_starry"

            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(console_log)
            starry_api.healthcheck(SimpleNamespace(healthcheck=True, binary=None, batch_manifest=None, work_dir=str(root), mode="shell-qemu"))
            self.assertEqual(json.loads(runner_result.read_text(encoding="utf-8"))["status"], "ok")
            self.assertIn("starry:~#", console_log.read_text(encoding="utf-8"))

            os.environ["SYZABI_PROGRAM_ID"] = "case-one"
            os.environ["SYZABI_RUN_ID"] = "run-one"
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(raw_trace)
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(external_state)
            starry_api.run_case(SimpleNamespace(healthcheck=False, binary=str(binary), batch_manifest=None, work_dir=str(root), mode="shell-qemu"))
            raw_payload = json.loads(raw_trace.read_text(encoding="utf-8"))
            self.assertEqual(raw_payload["events"][0]["syscall_name"], "close")
            self.assertEqual(raw_payload["process_exit"]["exit_code"], 0)

            missing_trace_binary = root / "missing-trace.candidate.bin"
            missing_trace_binary.write_text("bin", encoding="utf-8")
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(root / "missing-trace.result.json")
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(root / "missing-trace.raw.json")
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(root / "missing-trace.state.json")
            starry_api.run_case(
                SimpleNamespace(healthcheck=False, binary=str(missing_trace_binary), batch_manifest=None, work_dir=str(root), mode="shell-qemu")
            )
            missing_result = json.loads((root / "missing-trace.result.json").read_text(encoding="utf-8"))
            self.assertEqual(missing_result["status"], "infra_error")
            self.assertFalse((root / "missing-trace.raw.json").exists())

            batch_manifest = root / "batch-manifest.json"
            case_a_raw = root / "case-a.raw.json"
            case_b_raw = root / "case-b.raw.json"
            batch_manifest.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "program_id": "case-a",
                                "run_id": "case-a-run",
                                "binary_path": str(binary),
                                "console_path": str(root / "case-a.console.log"),
                                "raw_trace_path": str(case_a_raw),
                                "external_state_path": str(root / "case-a.state.json"),
                                "runner_result_path": str(root / "case-a.result.json"),
                            },
                            {
                                "program_id": "case-b",
                                "run_id": "case-b-run",
                                "binary_path": str(binary),
                                "console_path": str(root / "case-b.console.log"),
                                "raw_trace_path": str(case_b_raw),
                                "external_state_path": str(root / "case-b.state.json"),
                                "runner_result_path": str(root / "case-b.result.json"),
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            starry_api.run_batch(SimpleNamespace(batch_manifest=str(batch_manifest), healthcheck=False, binary=None, work_dir=str(root), mode="shell-qemu"))
            self.assertEqual(json.loads(case_a_raw.read_text(encoding="utf-8"))["events"][0]["syscall_name"], "close")
            self.assertEqual(json.loads(case_b_raw.read_text(encoding="utf-8"))["events"][0]["syscall_name"], "close")

            missing_batch_manifest = root / "missing-batch-manifest.json"
            missing_batch_manifest.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "program_id": "case-missing",
                                "run_id": "case-missing-run",
                                "binary_path": str(missing_trace_binary),
                                "console_path": str(root / "case-missing.console.log"),
                                "raw_trace_path": str(root / "case-missing.raw.json"),
                                "external_state_path": str(root / "case-missing.state.json"),
                                "runner_result_path": str(root / "case-missing.result.json"),
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            starry_api.run_batch(SimpleNamespace(batch_manifest=str(missing_batch_manifest), healthcheck=False, binary=None, work_dir=str(root), mode="shell-qemu"))
            missing_batch_result = json.loads((root / "case-missing.result.json").read_text(encoding="utf-8"))
            self.assertEqual(missing_batch_result["status"], "infra_error")
            self.assertFalse((root / "case-missing.raw.json").exists())

    def test_arceos_smoke_healthcheck_and_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            repo.mkdir(parents=True, exist_ok=True)
            revision = init_git_repo(repo)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
            write_executable(
                fake_bin / "cargo",
                "#!/bin/sh\necho arceos smoke ok\nexit 0\n",
            )
            write_executable(
                fake_bin / "qemu-system-riscv64",
                "#!/bin/sh\nexit 0\n",
            )

            config_path = root / "arceos.json"
            target_config = root / "arceos-target.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_arceos",
                        "target": "tgoskits_arceos",
                        "arch": "riscv64",
                        "runner_profiles_path": "configs/targets/tgoskits_arceos/runner_profiles.tgoskits_arceos_smoke.json",
                        "target_config_path": str(target_config),
                        "paths": {
                            "build_dir": str(root / "build"),
                            "artifacts_dir": str(root / "artifacts"),
                            "reports_dir": str(root / "reports"),
                            "eligible_file": str(root / "eligible.jsonl"),
                            "temp_dir": str(root / "tmp"),
                        },
                        "normalization": {"preview_bytes": 32},
                        "classification": {"no_diff": "NO_DIFF"},
                        "thresholds": {"smoke": {}, "signoff": {}},
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            target_config.write_text(
                json.dumps(
                    {
                        "build_info_path": str(root / "arceos-build-info.json"),
                        "default_mode": "smoke-qemu",
                        "revision": revision,
                        "repo_dir_env": "SYZABI_TGOSKITS_DIR",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "qemu-system-riscv64"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            os.environ["SYZABI_TGOSKITS_DIR"] = str(repo)
            os.environ["SYZABI_ENABLE_TGOSKITS"] = "1"
            os.environ["SYZABI_CONFIG_PATH"] = str(config_path)
            os.environ["SYZABI_WORKFLOW"] = "fake_arceos"
            runner_result = root / "arceos-runner-result.json"
            console_log = root / "arceos-console.log"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(console_log)

            arceos_api.healthcheck(SimpleNamespace(healthcheck=True, binary=None, mode="smoke-qemu"))
            self.assertEqual(json.loads(runner_result.read_text(encoding="utf-8"))["status"], "ok")
            self.assertIn("arceos smoke ok", console_log.read_text(encoding="utf-8"))
            with self.assertRaises(arceos_api.RunnerError):
                arceos_api.run_case(SimpleNamespace(healthcheck=False, binary="ignored", mode="smoke-qemu"))


if __name__ == "__main__":
    unittest.main()
