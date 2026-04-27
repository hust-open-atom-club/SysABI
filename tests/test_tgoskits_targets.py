from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from targets import entrypoint as target_entrypoint
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
                import sys
                from pathlib import Path

                cwd = Path.cwd()
                args = sys.argv[1:]
                disk = cwd / "make" / "disk.img"
                if "rootfs" in args or "build" in args:
                    disk.parent.mkdir(parents=True, exist_ok=True)
                    disk.write_text("disk", encoding="utf-8")
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

            class FakeSession:
                def __init__(self, cfg, *, cwd=None):
                    self.cfg = cfg
                    self._cwd = cwd
                    self._console = "starry:~#"

                def start(self) -> None:
                    return None

                def run_command(self, label: str, command: str, *, timeout_sec: int):
                    if label == "healthcheck":
                        self._console += "\n__SYZABI_HEALTHCHECK_OK__\nstarry:~#"
                        return "/fake/workspace\n__SYZABI_HEALTHCHECK_OK__\n", 0
                    if "missing-trace" in command or "missing-trace" in label or "case-missing" in label:
                        self._console += f"\n{label}: no trace\nstarry:~#"
                        return "plain output\n", 0
                    section = (
                        starry_api.TRACE_EVENT_STDOUT_PREFIX
                        + '{"args":[3,0,0,0,0,0],"end_ns":2,"errno":0,"event_index":0,"outputs":[],"return_value":0,"side":"candidate","start_ns":1,"syscall_name":"close","syscall_number":3}\n'
                    )
                    self._console += "\n" + section + "starry:~#"
                    return section, 0

                def console_text(self) -> str:
                    return self._console

                def close(self) -> None:
                    return None

            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(console_log)
            with patch("targets.tgoskits_starryos.api.ShellSession", FakeSession):
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
                with patch("sys.argv", ["targets/entrypoint.py", "--batch-manifest", str(batch_manifest)]):
                    target_entrypoint.main()
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
                with patch("sys.argv", ["targets/entrypoint.py", "--batch-manifest", str(missing_batch_manifest)]):
                    target_entrypoint.main()
                missing_batch_result = json.loads((root / "case-missing.result.json").read_text(encoding="utf-8"))
                self.assertEqual(missing_batch_result["status"], "infra_error")
                self.assertFalse((root / "case-missing.raw.json").exists())

    def test_arceos_smoke_healthcheck_and_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            workspace = repo / "os" / "arceos"
            workspace.mkdir(parents=True, exist_ok=True)
            platform_config = repo / "components" / "axplat_crates" / "platforms" / "axplat-riscv64-qemu-virt" / "axconfig.toml"
            platform_config.parent.mkdir(parents=True, exist_ok=True)
            platform_config.write_text("package = \"ax-plat-riscv64-qemu-virt\"\n", encoding="utf-8")
            template = repo / "scripts" / "arceos-c-test-cargo-config.template.toml"
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                "# Generated for tests.\n# axbuild-managed: arceos-c-test-cargo-config\n# axbuild-managed-patches: appended below\n",
                encoding="utf-8",
            )
            (repo / "components" / "axallocator").mkdir(parents=True, exist_ok=True)
            (repo / "Cargo.toml").write_text(
                "[patch.crates-io]\nax-allocator = { path = \"components/axallocator\" }\n",
                encoding="utf-8",
            )
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
            write_executable(
                fake_bin / "mkfs.fat",
                "#!/bin/sh\nexit 0\n",
            )
            for tool in ("riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"):
                write_executable(
                    fake_bin / tool,
                    "#!/bin/sh\nexit 0\n",
                )
            write_executable(
                fake_bin / "make",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    disk = None
                    for arg in args:
                        if arg.startswith("DISK_IMG="):
                            disk = Path(arg.split("=", 1)[1])
                    if "disk_img" in args:
                        if disk is None:
                            raise SystemExit(1)
                        disk.parent.mkdir(parents=True, exist_ok=True)
                        disk.write_text("disk", encoding="utf-8")
                        raise SystemExit(0)
                    if "defconfig" in args:
                        raise SystemExit(0)
                    if "run" in args:
                        sys.stdout.write("\\x1b[m__SYZABI_TRACE_EVENT__ {\\"args\\":[0,0,0,0,0,0],\\"end_ns\\":2,\\"errno\\":0,\\"event_index\\":0,\\"outputs\\":[],\\"return_value\\":0,\\"side\\":\\"candidate\\",\\"start_ns\\":1,\\"syscall_name\\":\\"close\\",\\"syscall_number\\":1028}\\n")
                        raise SystemExit(0)
                    raise SystemExit(0)
                    """
                ),
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
                        "workspace_subdir": "os/arceos",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "default_target": "riscv64gc-unknown-none-elf",
                        "platform_config_path": "components/axplat_crates/platforms/axplat-riscv64-qemu-virt/axconfig.toml",
                        "disk_image_path": "os/arceos/disk.img",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "make", "mkfs.fat", "qemu-system-riscv64", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"],
                        "app_features": ["alloc", "fd", "fs"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                        "command_timeout_sec": 10,
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
            raw_trace = root / "arceos-raw-trace.json"
            external_state = root / "arceos-external-state.json"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(console_log)

            arceos_api.healthcheck(SimpleNamespace(healthcheck=True, binary=None, mode="smoke-qemu"))
            self.assertEqual(json.loads(runner_result.read_text(encoding="utf-8"))["status"], "ok")
            self.assertIn("arceos smoke ok", console_log.read_text(encoding="utf-8"))

            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text(
                '#include <sys/syscall.h>\n#include "trace.h"\nint main(void) {\n    traced_syscall("close", __NR_close, 0, 0, 0, 0, 0, 0, 0);\n    return 0;\n}\n',
                encoding="utf-8",
            )
            os.environ["SYZABI_PROGRAM_ID"] = "case-close"
            os.environ["SYZABI_RUN_ID"] = "case-close-run"
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(raw_trace)
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(external_state)
            workdir = root / "workdir"
            arceos_api.run_case(SimpleNamespace(healthcheck=False, binary=str(binary), mode="smoke-qemu", work_dir=str(workdir)))
            self.assertEqual(json.loads(runner_result.read_text(encoding="utf-8"))["status"], "ok")
            self.assertEqual(json.loads(raw_trace.read_text(encoding="utf-8"))["events"][0]["syscall_name"], "close")
            self.assertTrue((workdir / "disk.img").exists())
            self.assertFalse((workspace / "disk.img").exists())
            self.assertIn("read_error", json.loads(external_state.read_text(encoding="utf-8")))
            self.assertFalse((workspace / ".cargo" / "config.toml").exists())
            with self.assertRaises(arceos_api.RunnerError):
                arceos_api.run_batch(SimpleNamespace(batch_manifest="ignored"))

    def test_arceos_run_case_preserves_exit_status_from_exit_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            workspace = repo / "os" / "arceos"
            workspace.mkdir(parents=True, exist_ok=True)
            platform_config = repo / "components" / "axplat_crates" / "platforms" / "axplat-riscv64-qemu-virt" / "axconfig.toml"
            platform_config.parent.mkdir(parents=True, exist_ok=True)
            platform_config.write_text("package = \"ax-plat-riscv64-qemu-virt\"\n", encoding="utf-8")
            template = repo / "scripts" / "arceos-c-test-cargo-config.template.toml"
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                "# Generated for tests.\n# axbuild-managed: arceos-c-test-cargo-config\n# axbuild-managed-patches: appended below\n",
                encoding="utf-8",
            )
            (repo / "components" / "axallocator").mkdir(parents=True, exist_ok=True)
            (repo / "Cargo.toml").write_text(
                "[patch.crates-io]\nax-allocator = { path = \"components/axallocator\" }\n",
                encoding="utf-8",
            )
            revision = init_git_repo(repo)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
            for tool in ("cargo", "qemu-system-riscv64", "mkfs.fat", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"):
                write_executable(fake_bin / tool, "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "make",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    disk = None
                    for arg in args:
                        if arg.startswith("DISK_IMG="):
                            disk = Path(arg.split("=", 1)[1])
                    if "disk_img" in args:
                        disk.parent.mkdir(parents=True, exist_ok=True)
                        disk.write_text("disk", encoding="utf-8")
                        raise SystemExit(0)
                    if "defconfig" in args:
                        raise SystemExit(0)
                    if "run" in args:
                        sys.stdout.write("__SYZABI_TRACE_EVENT__ {\\"args\\":[7,0,0,0,0,0],\\"end_ns\\":2,\\"errno\\":0,\\"event_index\\":0,\\"outputs\\":[],\\"return_value\\":0,\\"side\\":\\"candidate\\",\\"start_ns\\":1,\\"syscall_name\\":\\"exit_group\\",\\"syscall_number\\":1030}\\n")
                        raise SystemExit(0)
                    raise SystemExit(0)
                    """
                ),
            )
            config_path = root / "arceos.json"
            target_config = root / "arceos-target.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_arceos_exit",
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
                        "workspace_subdir": "os/arceos",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "default_target": "riscv64gc-unknown-none-elf",
                        "platform_config_path": "components/axplat_crates/platforms/axplat-riscv64-qemu-virt/axconfig.toml",
                        "disk_image_path": "os/arceos/disk.img",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "make", "mkfs.fat", "qemu-system-riscv64", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"],
                        "app_features": ["alloc", "fd", "fs"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                        "command_timeout_sec": 10,
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
            os.environ["SYZABI_WORKFLOW"] = "fake_arceos_exit"
            runner_result = root / "arceos-runner-result.json"
            console_log = root / "arceos-console.log"
            raw_trace = root / "arceos-raw-trace.json"
            external_state = root / "arceos-external-state.json"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(console_log)
            os.environ["SYZABI_PROGRAM_ID"] = "case-exit"
            os.environ["SYZABI_RUN_ID"] = "case-exit-run"
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(raw_trace)
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(external_state)

            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

            workdir = root / "workdir"
            arceos_api.run_case(SimpleNamespace(healthcheck=False, binary=str(binary), mode="smoke-qemu", work_dir=str(workdir)))
            self.assertEqual(json.loads(runner_result.read_text(encoding="utf-8"))["exit_code"], 7)
            self.assertEqual(json.loads(raw_trace.read_text(encoding="utf-8"))["process_exit"]["exit_code"], 7)
            self.assertTrue((workdir / "disk.img").exists())

    def test_arceos_sample_fat_external_state_accepts_non_fat32_image(self) -> None:
        if shutil.which("mkfs.fat") is None:
            self.skipTest("mkfs.fat not available")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image = root / "fat.img"
            subprocess.run(["dd", "if=/dev/zero", f"of={image}", "bs=1M", "count=4"], check=True, capture_output=True, text=True)
            subprocess.run(["mkfs.fat", str(image)], check=True, capture_output=True, text=True)
            payload = arceos_api.sample_fat_external_state(image)
            self.assertNotIn("read_error", payload)
            self.assertEqual(payload["files"], [])

    def test_arceos_trace_preserves_close_stdout_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "driver.c"
            binary = root / "driver"
            source.write_text(
                textwrap.dedent(
                    """\
                    #include <errno.h>
                    #include <string.h>
                    #include "trace.h"

                    int main(void) {
                        static const char msg[] = "A";
                        long close_ret = traced_syscall("close", 1028, 0, 1, 0, 0, 0, 0, 0);
                        long write_ret = traced_syscall("write", 1027, 1, 1, (long)msg, 1, 0, 0, 0);
                        if (close_ret != 0)
                            return 11;
                        if (write_ret != -1)
                            return 12;
                        if (errno != EBADF)
                            return 13;
                        return 0;
                    }
                    """
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-std=gnu11",
                    "-I",
                    str(Path.cwd() / "agent" / "arceos"),
                    "-I",
                    str(Path.cwd() / "agent" / "arceos" / "include"),
                    str(source),
                    str(Path.cwd() / "agent" / "arceos" / "trace.c"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run([str(binary)], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            events = [
                json.loads(line[len("__SYZABI_TRACE_EVENT__ ") :])
                for line in completed.stdout.splitlines()
                if line.startswith("__SYZABI_TRACE_EVENT__ ")
            ]
            self.assertEqual(events[0]["syscall_name"], "close")
            self.assertEqual(events[0]["return_value"], 0)
            self.assertEqual(events[1]["syscall_name"], "write")
            self.assertEqual(events[1]["return_value"], -1)
            self.assertEqual(events[1]["errno"], 9)

    def test_arceos_trace_records_read_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "driver.c"
            binary = root / "driver"
            source.write_text(
                textwrap.dedent(
                    """\
                    #include <string.h>
                    #include <unistd.h>
                    #include "trace.h"

                    int main(void) {{
                        int pipefd[2];
                        char buf[8] = {0};
                        if (pipe(pipefd) != 0)
                            return 11;
                        if (write(pipefd[1], "DATA", 4) != 4)
                            return 12;
                        if (traced_syscall("read", 1026, 0, pipefd[0], (long)buf, 4, 0, 0, 0) != 4)
                            return 13;
                        if (memcmp(buf, "DATA", 4) != 0)
                            return 14;
                        return 0;
                    }}
                    """
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-std=gnu11",
                    "-I",
                    str(Path.cwd() / "agent" / "arceos"),
                    "-I",
                    str(Path.cwd() / "agent" / "arceos" / "include"),
                    str(source),
                    str(Path.cwd() / "agent" / "arceos" / "trace.c"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run([str(binary)], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            event_line = next(line for line in completed.stdout.splitlines() if line.startswith("__SYZABI_TRACE_EVENT__ "))
            event = json.loads(event_line[len("__SYZABI_TRACE_EVENT__ ") :])
            self.assertEqual(event["syscall_name"], "read")
            self.assertEqual(event["outputs"][0]["label"], "buf")
            self.assertEqual(event["outputs"][0]["length"], 4)
            self.assertEqual(event["outputs"][0]["preview_hex"], "44415441")

    def test_arceos_trace_does_not_consume_fd3(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "driver.c"
            binary = root / "driver"
            source.write_text(
                textwrap.dedent(
                    """\
                    #include <errno.h>
                    #include <stdio.h>
                    #include "trace.h"

                    int main(void) {
                        errno = 0;
                        if (traced_syscall("close", 1028, 0, 3, 0, 0, 0, 0, 0) != -1)
                            return 11;
                        if (errno != EBADF)
                            return 12;
                        return 0;
                    }
                    """
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-std=gnu11",
                    "-I",
                    str(Path.cwd() / "agent" / "arceos"),
                    "-I",
                    str(Path.cwd() / "agent" / "arceos" / "include"),
                    str(source),
                    str(Path.cwd() / "agent" / "arceos" / "trace.c"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run([str(binary)], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_arceos_trace_starts_marker_on_new_line_after_stdout_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "driver.c"
            binary = root / "driver"
            source.write_text(
                textwrap.dedent(
                    """\
                    #include <unistd.h>
                    #include "trace.h"

                    int main(void) {
                        write(1, "DATA", 4);
                        traced_syscall("close", 1028, 0, 0, 0, 0, 0, 0, 0);
                        return 0;
                    }
                    """
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-std=gnu11",
                    "-I",
                    str(Path.cwd() / "agent" / "arceos"),
                    "-I",
                    str(Path.cwd() / "agent" / "arceos" / "include"),
                    str(source),
                    str(Path.cwd() / "agent" / "arceos" / "trace.c"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run([str(binary)], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("DATA\n__SYZABI_TRACE_EVENT__", completed.stdout)

    def test_arceos_trace_handles_open_tmpfile_mode_without_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "driver.c"
            binary = root / "driver"
            source.write_text(
                textwrap.dedent(
                    """\
                    #include <fcntl.h>
                    #include <unistd.h>
                    #include "trace.h"

#ifndef O_TMPFILE
#define O_TMPFILE 020200000
#endif

                    int main(void) {
                        long fd = traced_syscall("open", 1024, 0, (long)".", O_TMPFILE | O_RDWR, 0600, 0, 0, 0);
                        if (fd >= 0)
                            close((int)fd);
                        return 0;
                    }
                    """
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "gcc",
                    "-O2",
                    "-std=gnu11",
                    "-I",
                    str(Path.cwd() / "agent" / "arceos"),
                    "-I",
                    str(Path.cwd() / "agent" / "arceos" / "include"),
                    str(source),
                    str(Path.cwd() / "agent" / "arceos" / "trace.c"),
                    "-o",
                    str(binary),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run([str(binary)], check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("__SYZABI_TRACE_EVENT__", completed.stdout)

    def test_arceos_run_case_restores_managed_cargo_config_on_make_args_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            workspace = repo / "os" / "arceos"
            workspace.mkdir(parents=True, exist_ok=True)
            template = repo / "scripts" / "arceos-c-test-cargo-config.template.toml"
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                "# Generated for tests.\n# axbuild-managed: arceos-c-test-cargo-config\n# axbuild-managed-patches: appended below\n",
                encoding="utf-8",
            )
            (repo / "components" / "axallocator").mkdir(parents=True, exist_ok=True)
            (repo / "Cargo.toml").write_text(
                "[patch.crates-io]\nax-allocator = { path = \"components/axallocator\" }\n",
                encoding="utf-8",
            )
            revision = init_git_repo(repo)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
            for tool in ("cargo", "qemu-system-riscv64", "mkfs.fat", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"):
                write_executable(fake_bin / tool, "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "make",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    for arg in sys.argv[1:]:
                        if arg.startswith("DISK_IMG="):
                            disk = Path(arg.split("=", 1)[1])
                            disk.parent.mkdir(parents=True, exist_ok=True)
                            disk.write_text("disk", encoding="utf-8")
                    raise SystemExit(0)
                    """
                ),
            )
            config_path = root / "arceos.json"
            target_config = root / "arceos-target.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_arceos_bad_platform",
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
                        "workspace_subdir": "os/arceos",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "default_target": "riscv64gc-unknown-none-elf",
                        "platform_config_path": "components/does-not-exist/axconfig.toml",
                        "disk_image_path": "os/arceos/disk.img",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "make", "mkfs.fat", "qemu-system-riscv64", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"],
                        "app_features": ["alloc", "fd", "fs"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                        "command_timeout_sec": 10,
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
            os.environ["SYZABI_WORKFLOW"] = "fake_arceos_bad_platform"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(root / "runner.json")
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(root / "console.log")
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(root / "raw.json")
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(root / "state.json")
            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            with self.assertRaises(arceos_api.RunnerError):
                arceos_api.run_case(SimpleNamespace(healthcheck=False, binary=str(binary), mode="smoke-qemu", work_dir=str(root / "workdir")))
            self.assertFalse((workspace / ".cargo" / "config.toml").exists())

    def test_arceos_run_case_reports_missing_replay_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            workspace = repo / "os" / "arceos"
            workspace.mkdir(parents=True, exist_ok=True)
            platform_config = repo / "components" / "axplat_crates" / "platforms" / "axplat-riscv64-qemu-virt" / "axconfig.toml"
            platform_config.parent.mkdir(parents=True, exist_ok=True)
            platform_config.write_text("package = \"ax-plat-riscv64-qemu-virt\"\n", encoding="utf-8")
            template = repo / "scripts" / "arceos-c-test-cargo-config.template.toml"
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                "# Generated for tests.\n# axbuild-managed: arceos-c-test-cargo-config\n# axbuild-managed-patches: appended below\n",
                encoding="utf-8",
            )
            (repo / "components" / "axallocator").mkdir(parents=True, exist_ok=True)
            (repo / "Cargo.toml").write_text(
                "[patch.crates-io]\nax-allocator = { path = \"components/axallocator\" }\n",
                encoding="utf-8",
            )
            revision = init_git_repo(repo)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
            for tool in ("cargo", "qemu-system-riscv64"):
                write_executable(fake_bin / tool, "#!/bin/sh\nexit 0\n")
            config_path = root / "arceos.json"
            target_config = root / "arceos-target.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_arceos_missing_toolchain",
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
                        "workspace_subdir": "os/arceos",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "default_target": "riscv64gc-unknown-none-elf",
                        "platform_config_path": "components/axplat_crates/platforms/axplat-riscv64-qemu-virt/axconfig.toml",
                        "disk_image_path": "os/arceos/disk.img",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "qemu-system-riscv64"],
                        "replay_toolchain_probes": ["gcc", "make", "mkfs.fat", "riscv64-linux-musl-gcc"],
                        "app_features": ["alloc", "fd", "fs"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                        "command_timeout_sec": 10,
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
            os.environ["SYZABI_WORKFLOW"] = "fake_arceos_missing_toolchain"
            runner_result = root / "runner.json"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(root / "console.log")
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(root / "raw.json")
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(root / "state.json")
            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            with self.assertRaises(arceos_api.RunnerError):
                arceos_api.run_case(SimpleNamespace(healthcheck=False, binary=str(binary), mode="smoke-qemu", work_dir=str(root / "workdir")))
            payload = json.loads(runner_result.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "infra_error")
            self.assertIn("missing required ArceOS tools", payload["detail"])

    def test_arceos_run_case_rejects_openat_programs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text(
                '#include <sys/syscall.h>\n#include "trace.h"\nint main(void) {\n    traced_syscall("openat", __NR_openat, 0, -100, 0, 0, 0, 0, 0);\n    return 0;\n}\n',
                encoding="utf-8",
            )
            with self.assertRaises(arceos_api.RunnerError) as ctx:
                arceos_api.reject_unsupported_source(binary)
            self.assertIn("openat", str(ctx.exception))

    def test_arceos_main_preserves_existing_infra_error_runner_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runner_result = root / "runner.json"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)

            def fail_after_writing(*_args, **_kwargs) -> None:
                runner_result.write_text(
                    json.dumps(
                        {
                            "status": "infra_error",
                            "exit_code": None,
                            "detail": "ArceOS run timed out after 10s",
                            "kernel_build": "tgoskits-arceos@test",
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                raise arceos_api.RunnerError("ArceOS run timed out after 10s")

            with patch("targets.tgoskits_arceos.api.parse_args", return_value=SimpleNamespace(healthcheck=False, batch_manifest=None, binary="ignored", mode="smoke-qemu", work_dir=str(root))), patch(
                "targets.tgoskits_arceos.api.run_case", side_effect=fail_after_writing
            ):
                with self.assertRaises(SystemExit):
                    arceos_api.main()
            payload = json.loads(runner_result.read_text(encoding="utf-8"))
            self.assertEqual(payload["kernel_build"], "tgoskits-arceos@test")

    def test_arceos_main_overwrites_stale_runner_result_on_new_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runner_result = root / "runner.json"
            runner_result.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "exit_code": 0,
                        "kernel_build": "stale-build",
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            with patch("targets.tgoskits_arceos.api.parse_args", return_value=SimpleNamespace(healthcheck=False, batch_manifest=None, binary="ignored", mode="smoke-qemu", work_dir=str(root))), patch(
                "targets.tgoskits_arceos.api.run_case", side_effect=arceos_api.RunnerError("fresh failure")
            ):
                with self.assertRaises(SystemExit):
                    arceos_api.main()
            payload = json.loads(runner_result.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "infra_error")
            self.assertEqual(payload["detail"], "fresh failure")

    def test_arceos_main_writes_runner_result_when_managed_cargo_template_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            workspace = repo / "os" / "arceos"
            workspace.mkdir(parents=True, exist_ok=True)
            platform_config = repo / "components" / "axplat_crates" / "platforms" / "axplat-riscv64-qemu-virt" / "axconfig.toml"
            platform_config.parent.mkdir(parents=True, exist_ok=True)
            platform_config.write_text("package = \"ax-plat-riscv64-qemu-virt\"\n", encoding="utf-8")
            (repo / "components" / "axallocator").mkdir(parents=True, exist_ok=True)
            (repo / "Cargo.toml").write_text(
                "[patch.crates-io]\nax-allocator = { path = \"components/axallocator\" }\n",
                encoding="utf-8",
            )
            revision = init_git_repo(repo)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
            for tool in ("cargo", "qemu-system-riscv64", "mkfs.fat", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib", "gcc"):
                write_executable(fake_bin / tool, "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "make",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    for arg in sys.argv[1:]:
                        if arg.startswith("DISK_IMG="):
                            disk = Path(arg.split("=", 1)[1])
                            disk.parent.mkdir(parents=True, exist_ok=True)
                            disk.write_text("disk", encoding="utf-8")
                    raise SystemExit(0)
                    """
                ),
            )
            config_path = root / "arceos.json"
            target_config = root / "arceos-target.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_arceos_missing_template",
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
                        "workspace_subdir": "os/arceos",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "default_target": "riscv64gc-unknown-none-elf",
                        "platform_config_path": "components/axplat_crates/platforms/axplat-riscv64-qemu-virt/axconfig.toml",
                        "disk_image_path": "os/arceos/disk.img",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "qemu-system-riscv64"],
                        "replay_toolchain_probes": ["gcc", "make", "mkfs.fat", "riscv64-linux-musl-gcc"],
                        "app_features": ["alloc", "fd", "fs"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                        "command_timeout_sec": 10,
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
            os.environ["SYZABI_WORKFLOW"] = "fake_arceos_missing_template"
            runner_result = root / "runner.json"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(root / "console.log")
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(root / "raw.json")
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(root / "state.json")
            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                with patch("targets.tgoskits_arceos.api.parse_args", return_value=SimpleNamespace(healthcheck=False, batch_manifest=None, binary=str(binary), mode="smoke-qemu", work_dir=str(root / "workdir"))):
                    arceos_api.main()
            payload = json.loads(runner_result.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "infra_error")
            self.assertIn("managed ArceOS cargo-config template", payload["detail"])

    def test_arceos_run_case_writes_timeout_runner_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "tgoskits"
            workspace = repo / "os" / "arceos"
            workspace.mkdir(parents=True, exist_ok=True)
            platform_config = repo / "components" / "axplat_crates" / "platforms" / "axplat-riscv64-qemu-virt" / "axconfig.toml"
            platform_config.parent.mkdir(parents=True, exist_ok=True)
            platform_config.write_text("package = \"ax-plat-riscv64-qemu-virt\"\n", encoding="utf-8")
            template = repo / "scripts" / "arceos-c-test-cargo-config.template.toml"
            template.parent.mkdir(parents=True, exist_ok=True)
            template.write_text(
                "# Generated for tests.\n# axbuild-managed: arceos-c-test-cargo-config\n# axbuild-managed-patches: appended below\n",
                encoding="utf-8",
            )
            (repo / "components" / "axallocator").mkdir(parents=True, exist_ok=True)
            (repo / "Cargo.toml").write_text(
                "[patch.crates-io]\nax-allocator = { path = \"components/axallocator\" }\n",
                encoding="utf-8",
            )
            revision = init_git_repo(repo)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            os.environ["PATH"] = f"{fake_bin}:{os.environ.get('PATH', '')}"
            for tool in ("cargo", "qemu-system-riscv64", "mkfs.fat", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"):
                write_executable(fake_bin / tool, "#!/bin/sh\nexit 0\n")
            write_executable(
                fake_bin / "make",
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import sys
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    disk = None
                    for arg in args:
                        if arg.startswith("DISK_IMG="):
                            disk = Path(arg.split("=", 1)[1])
                    if "disk_img" in args:
                        disk.parent.mkdir(parents=True, exist_ok=True)
                        disk.write_text("disk", encoding="utf-8")
                        raise SystemExit(0)
                    if "defconfig" in args:
                        raise SystemExit(0)
                    if "run" in args:
                        time.sleep(2)
                        raise SystemExit(0)
                    raise SystemExit(0)
                    """
                ),
            )
            config_path = root / "arceos.json"
            target_config = root / "arceos-target.json"
            config_path.write_text(
                json.dumps(
                    {
                        "workflow": "fake_arceos_timeout",
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
                        "workspace_subdir": "os/arceos",
                        "feature_flag_env": "SYZABI_ENABLE_TGOSKITS",
                        "default_target": "riscv64gc-unknown-none-elf",
                        "platform_config_path": "components/axplat_crates/platforms/axplat-riscv64-qemu-virt/axconfig.toml",
                        "disk_image_path": "os/arceos/disk.img",
                        "supported_targets": ["riscv64gc-unknown-none-elf"],
                        "toolchain_probes": ["cargo", "make", "mkfs.fat", "qemu-system-riscv64", "riscv64-linux-musl-gcc", "riscv64-linux-musl-ar", "riscv64-linux-musl-ranlib"],
                        "app_features": ["alloc", "fd", "fs"],
                        "prepare_commands": [],
                        "healthcheck_command": ["cargo", "xtask", "arceos", "qemu", "--package", "ax-helloworld", "--target", "riscv64gc-unknown-none-elf"],
                        "command_timeout_sec": 1,
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
            os.environ["SYZABI_WORKFLOW"] = "fake_arceos_timeout"
            runner_result = root / "arceos-runner-result.json"
            console_log = root / "arceos-console.log"
            raw_trace = root / "arceos-raw-trace.json"
            external_state = root / "arceos-external-state.json"
            os.environ["SYZABI_RUNNER_RESULT_PATH"] = str(runner_result)
            os.environ["SYZABI_CONSOLE_LOG_PATH"] = str(console_log)
            os.environ["SYZABI_PROGRAM_ID"] = "case-timeout"
            os.environ["SYZABI_RUN_ID"] = "case-timeout-run"
            os.environ["SYZABI_RAW_TRACE_PATH"] = str(raw_trace)
            os.environ["SYZABI_EXTERNAL_STATE_PATH"] = str(external_state)

            binary = root / "testcase.candidate.bin"
            binary.write_text("bin", encoding="utf-8")
            binary.with_name("testcase.instrumented.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

            with self.assertRaises(arceos_api.RunnerError):
                arceos_api.run_case(SimpleNamespace(healthcheck=False, binary=str(binary), mode="smoke-qemu", work_dir=str(root / "workdir")))
            result_payload = json.loads(runner_result.read_text(encoding="utf-8"))
            self.assertEqual(result_payload["status"], "infra_error")
            self.assertIn("timed out", result_payload["detail"])

    def test_arceos_diff_external_state_reports_only_changed_files(self) -> None:
        base = {
            "files": [
                {"path": "etc/profile", "size": 4, "sha256": "same"},
                {"path": "tmp/keep", "size": 3, "sha256": "same-two"},
            ]
        }
        current = {
            "files": [
                {"path": "etc/profile", "size": 4, "sha256": "same"},
                {"path": "tmp/keep", "size": 5, "sha256": "changed"},
                {"path": "tmp/new", "size": 1, "sha256": "new"},
            ]
        }
        self.assertEqual(
            arceos_api.diff_external_state(base, current),
            {
                "files": [
                    {"path": "tmp/keep", "size": 5, "sha256": "changed"},
                    {"path": "tmp/new", "size": 1, "sha256": "new"},
                ]
            },
        )


if __name__ == "__main__":
    unittest.main()
