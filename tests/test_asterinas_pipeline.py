from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analyzer.classify import classify_result
from orchestrator.common import config, configure_runtime, dump_json, runner_profiles
from orchestrator import scheduler
from orchestrator.vm_runner import finalize_process_result, prepare_candidate_initramfs_package, sample_external_state
from tools.derive_asterinas_corpus import derive_rejection
from tools.run_asterinas import (
    GUEST_ENV_HEADER_MAGIC,
    RunnerError,
    candidate_status_from_events,
    compose_autorun,
    compose_init,
    compose_packaged_autorun,
    containerized_qemu_direct_command,
    containerized_grub_iso_command,
    docker_qemu_direct_script,
    docker_grub_bundle_script,
    ensure_git_mirror,
    guest_crash_detail,
    guest_env_lines,
    gitconfig_lines,
    host_grub_bundle_command,
    host_osdk_env,
    kvm_enabled,
    materialize_guest_env_file,
    prepare_run_cargo_home,
    parse_batch_case_results,
    qemu_direct_command,
    qemu_log_paths,
    selected_guest_cmdline_append,
    should_fallback_to_host_direct,
    target_osdk_dir,
    write_missing_marker_crash_result,
)
from targets.asterinas.adapter import AsterinasTargetAdapter


class AsterinasPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_workflow = os.environ.get("SYZABI_WORKFLOW")
        self.previous_config = os.environ.get("SYZABI_CONFIG_PATH")
        configure_runtime(workflow="asterinas", config_path=None)
        os.environ.pop("SYZABI_CONFIG_PATH", None)

    def tearDown(self) -> None:
        if self.previous_workflow is None:
            os.environ.pop("SYZABI_WORKFLOW", None)
        else:
            os.environ["SYZABI_WORKFLOW"] = self.previous_workflow
        if self.previous_config is None:
            os.environ.pop("SYZABI_CONFIG_PATH", None)
        else:
            os.environ["SYZABI_CONFIG_PATH"] = self.previous_config

    def test_asterinas_config_uses_command_candidate_profile(self) -> None:
        cfg = config()
        self.assertEqual(cfg["workflow"], "asterinas")
        self.assertEqual(cfg["paths"]["eligible_file"], "eligible_programs/targets/asterinas/asterinas/default.jsonl")
        self.assertEqual(cfg["parallel"]["jobs"], 4)
        self.assertEqual(cfg["parallel"]["candidate_batch_size"], 100)
        self.assertEqual(runner_profiles()["candidate"]["kind"], "command")
        self.assertEqual(runner_profiles()["candidate"]["binary_name"], "testcase.candidate.bin")
        self.assertEqual(runner_profiles()["candidate"]["controlled_divergence"]["match_syscall"], "openat")
        self.assertEqual(runner_profiles()["candidate"]["command_batching_mode"], "packaged_per_case")

    def test_asterinas_scml_profile_uses_distinct_sandbox_roots(self) -> None:
        previous_workflow = os.environ.get("SYZABI_WORKFLOW")
        previous_config = os.environ.get("SYZABI_CONFIG_PATH")
        try:
            configure_runtime(workflow="asterinas_scml", config_path=None)
            os.environ.pop("SYZABI_CONFIG_PATH", None)
            profiles = runner_profiles()
        finally:
            if previous_workflow is None:
                os.environ.pop("SYZABI_WORKFLOW", None)
            else:
                os.environ["SYZABI_WORKFLOW"] = previous_workflow
            if previous_config is None:
                os.environ.pop("SYZABI_CONFIG_PATH", None)
            else:
                os.environ["SYZABI_CONFIG_PATH"] = previous_config
        self.assertEqual(profiles["reference"]["work_root"], "artifacts/sandboxes/asterinas_scml/reference")
        self.assertEqual(profiles["candidate"]["work_root"], "artifacts/sandboxes/asterinas_scml/candidate")

    def test_sample_external_state_skips_paths_that_raise_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            good = root / "good.txt"
            good.write_text("ok", encoding="utf-8")
            bad = root / ("a" * 5000)
            with patch.object(Path, "rglob", return_value=[good, bad]):
                state = sample_external_state(root)
        self.assertEqual(len(state["files"]), 1)
        self.assertEqual(state["files"][0]["path"], "good.txt")

    def test_asterinas_derivation_keeps_exact_full_name_subset(self) -> None:
        cfg = config()
        allowed = {
            "full_syscall_list": ["openat", "read", "close"],
        }
        rejected_variant = {
            "full_syscall_list": ["openat$fuse"],
        }
        self.assertEqual(derive_rejection(allowed, cfg), [])
        self.assertEqual(derive_rejection(rejected_variant, cfg), ["unsupported_variant"])

    def test_asterinas_derivation_uses_stable_rejection_taxonomy(self) -> None:
        cfg = config()
        meta = {
            "full_syscall_list": ["openat", "mmap", "wait4", "socketpair$inet"],
        }
        reasons = derive_rejection(meta, cfg)
        self.assertIn("unsupported_memory_management", reasons)
        self.assertIn("unsupported_process_control", reasons)
        self.assertIn("unsupported_variant", reasons)

    def test_command_runner_result_can_report_unsupported_status(self) -> None:
        status, exit_code, detail, kernel_build = finalize_process_result(
            profile_kind="command",
            completed_returncode=1,
            runner_result={
                "status": "unsupported",
                "exit_code": None,
                "status_detail": "ENOSYS",
                "kernel_build": "asterinas-1234",
            },
            fallback_kernel_build="fallback",
        )
        self.assertEqual(status, "unsupported")
        self.assertIsNone(exit_code)
        self.assertEqual(detail, "ENOSYS")
        self.assertEqual(kernel_build, "asterinas-1234")

    def test_classifier_accepts_explicit_unsupported_status(self) -> None:
        classes = config()["classification"]
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="unsupported",
                comparison=None,
            ),
            classes["unsupported_feature"],
        )
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="unsupported",
                comparison={"equivalent": False, "noise_only": False},
            ),
            classes["unsupported_feature"],
        )

    def test_enosys_events_map_to_unsupported_candidate_status(self) -> None:
        status = candidate_status_from_events(
            [
                {
                    "return_value": -1,
                    "errno": 38,
                }
            ],
            {"status": "ok", "exit_code": 0, "timed_out": False},
        )
        self.assertEqual(status, "unsupported")

    def test_non_ok_process_exit_status_is_preserved(self) -> None:
        status = candidate_status_from_events([], {"status": "infra_error", "exit_code": None, "timed_out": False})
        self.assertEqual(status, "infra_error")

    def test_crash_process_exit_status_is_preserved(self) -> None:
        status = candidate_status_from_events([], {"status": "crash", "exit_code": 132, "timed_out": False})
        self.assertEqual(status, "crash")

    def test_signal_like_exit_code_is_not_crash_by_itself(self) -> None:
        status = candidate_status_from_events([], {"status": "ok", "exit_code": 132, "timed_out": False})
        self.assertEqual(status, "ok")

    def test_compose_init_uses_explicit_autorun_entrypoint(self) -> None:
        script = compose_init()
        self.assertTrue(script.startswith("#!/bin/sh"))
        self.assertIn("exec /syzkabi/autorun.sh", script)

    def test_compose_autorun_propagates_injected_trace_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYZABI_INJECT_TRACE_ENABLED": "1",
                "SYZABI_INJECT_TRACE_SYSCALL": "openat",
                "SYZABI_INJECT_TRACE_FIELD": "return",
                "SYZABI_INJECT_TRACE_VALUE": "-5",
            },
            clear=False,
        ):
            script = compose_autorun(32)
        self.assertIn("export SYZABI_INJECT_TRACE_ENABLED=1", script)
        self.assertIn("export SYZABI_INJECT_TRACE_SYSCALL=openat", script)
        self.assertIn("export SYZABI_INJECT_TRACE_FIELD=return", script)
        self.assertIn("export SYZABI_INJECT_TRACE_VALUE=-5", script)
        self.assertIn("PROC_STATUS=ok", script)
        self.assertIn('if [ "$EXIT_CODE" -ge 128 ]; then', script)
        self.assertIn("PROC_STATUS=crash", script)
        self.assertIn('printf \'{"status":"%s","exit_code":%s,"timed_out":false}', script)

    def test_compose_packaged_autorun_reads_injected_trace_from_cmdline(self) -> None:
        script = compose_packaged_autorun(32)
        self.assertIn("RAW_SELECTOR_MAGIC=SYZABI_ENV_V1", script)
        self.assertIn("load_selector_from_raw_devices() {", script)
        self.assertIn('$BUSYBOX dd if="$device" of="$header_file" bs=1024 count=1', script)
        self.assertIn('if ! $BUSYBOX grep -q "^$RAW_SELECTOR_MAGIC$" "$header_file"; then', script)
        self.assertIn('MOUNTED_EXT2_DEVICE=""', script)
        self.assertIn('AVAILABLE_BLOCK_DEVICES=""', script)
        self.assertIn('RAW_SELECTOR_LOADED=0', script)
        self.assertIn('while [ "$attempt" -lt 30 ]; do', script)
        self.assertIn('AVAILABLE_BLOCK_DEVICES="$($BUSYBOX find /dev -maxdepth 1 -type b 2>/dev/null | $BUSYBOX sort | $BUSYBOX tr', script)
        self.assertIn('for device in /dev/vda /dev/vdb /dev/vdc /dev/sda /dev/sdb /dev/sdc $AVAILABLE_BLOCK_DEVICES; do', script)
        self.assertIn('if load_selector_from_raw_devices; then', script)
        self.assertIn('$BUSYBOX sleep 1', script)
        self.assertIn('if [ -z "$MOUNTED_EXT2_DEVICE" ] && [ "$RAW_SELECTOR_LOADED" -ne 1 ]; then', script)
        self.assertIn('failed to mount ext2 package disk on $EXT2_MOUNT; block devices: $AVAILABLE_BLOCK_DEVICES', script)
        self.assertIn('if [ -f "$EXT2_MOUNT/syzkabi.env" ]; then', script)
        self.assertIn('. "$EXT2_MOUNT/syzkabi.env"', script)
        self.assertIn('fail_and_poweroff() {', script)
        self.assertIn('SLOT="${SYZABI_PACKAGE_SLOT:-}"', script)
        self.assertIn('missing SYZABI_PACKAGE_SLOT selector', script)
        self.assertIn('missing packaged selector file: $EXT2_MOUNT/syzkabi.env and no raw selector header found', script)
        self.assertIn('export SYZABI_INJECT_TRACE_ENABLED="$SYZABI_INJECT_TRACE_ENABLED"', script)
        self.assertIn('export SYZABI_INJECT_TRACE_SYSCALL="$SYZABI_INJECT_TRACE_SYSCALL"', script)
        self.assertIn('export SYZABI_INJECT_TRACE_FIELD="$SYZABI_INJECT_TRACE_FIELD"', script)
        self.assertIn('export SYZABI_INJECT_TRACE_VALUE="$SYZABI_INJECT_TRACE_VALUE"', script)
        self.assertNotIn("unset SYZABI_INJECT_TRACE_ENABLED", script)
        self.assertIn("PROC_STATUS=ok", script)
        self.assertIn('if [ "$EXIT_CODE" -ge 128 ]; then', script)
        self.assertIn("PROC_STATUS=crash", script)
        self.assertIn('printf \'{"status":"%s","exit_code":%s,"timed_out":false}', script)

    def test_selected_guest_cmdline_append_only_includes_extra_guest_args(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYZABI_GUEST_KCMD_ARGS": "console=hvc0 extra=1",
            },
            clear=False,
        ):
            args = selected_guest_cmdline_append()
        self.assertEqual(args, "console=hvc0 extra=1")

    def test_guest_env_lines_include_slot_and_injected_trace(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYZABI_ASTERINAS_PACKAGE_SLOT": "7",
                "SYZABI_INJECT_TRACE_ENABLED": "1",
                "SYZABI_INJECT_TRACE_CALL_INDEX": "-1",
                "SYZABI_INJECT_TRACE_SYSCALL": "openat",
                "SYZABI_INJECT_TRACE_FIELD": "return",
                "SYZABI_INJECT_TRACE_VALUE": "-5",
            },
            clear=False,
        ):
            lines = guest_env_lines()
        self.assertIn("SYZABI_PACKAGE_SLOT=7", lines)
        self.assertIn("SYZABI_INJECT_TRACE_ENABLED=1", lines)
        self.assertIn("SYZABI_INJECT_TRACE_CALL_INDEX=-1", lines)
        self.assertIn("SYZABI_INJECT_TRACE_SYSCALL=openat", lines)
        self.assertIn("SYZABI_INJECT_TRACE_FIELD=return", lines)
        self.assertIn("SYZABI_INJECT_TRACE_VALUE=-5", lines)

    def test_guest_env_lines_requires_slot_for_packaged_runs(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SYZABI_ASTERINAS_PACKAGE_DIR": "/tmp/package",
            },
            clear=False,
        ):
            with self.assertRaises(RunnerError):
                guest_env_lines()

    def test_materialize_guest_env_file_writes_selector_via_debugfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "ext2.img"
            image.write_bytes(b"placeholder")
            commands: list[list[str]] = []

            def fake_run(cmd, text, capture_output, check):
                commands.append(cmd)
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch.dict(
                os.environ,
                {
                    "SYZABI_ASTERINAS_PACKAGE_SLOT": "3",
                    "SYZABI_INJECT_TRACE_ENABLED": "1",
                },
                clear=False,
            ), patch("tools.run_asterinas.subprocess.run", side_effect=fake_run):
                materialize_guest_env_file(image)
        self.assertEqual(commands[0][:4], ["debugfs", "-w", "-R", "rm /syzkabi.env"])
        self.assertEqual(commands[1][:3], ["debugfs", "-w", "-R"])
        self.assertIn("/syzkabi.env", commands[1][3])

    def test_materialize_guest_env_file_uses_runtime_temp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image = root / "ext2.img"
            image.write_bytes(b"placeholder")
            temp_root = root / "runtime-tmp"
            temp_root.mkdir()
            seen_dirs: list[Path] = []

            real_named_tempfile = tempfile.NamedTemporaryFile

            def fake_named_tempfile(*args, **kwargs):
                seen_dirs.append(Path(kwargs["dir"]))
                return real_named_tempfile(*args, **kwargs)

            def fake_run(cmd, text, capture_output, check):
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch.dict(
                os.environ,
                {
                    "SYZABI_ASTERINAS_PACKAGE_SLOT": "3",
                },
                clear=False,
            ), patch(
                "tools.run_asterinas.runtime_temp_dir",
                return_value=temp_root,
            ), patch(
                "tools.run_asterinas.tempfile.NamedTemporaryFile",
                side_effect=fake_named_tempfile,
            ), patch(
                "tools.run_asterinas.subprocess.run",
                side_effect=fake_run,
            ):
                materialize_guest_env_file(image)

        self.assertEqual(seen_dirs, [temp_root])

    def test_materialize_guest_env_file_writes_raw_selector_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            image = Path(tmpdir) / "ext2.img"
            image.write_bytes(b"\0" * 4096)

            def fake_run(cmd, text, capture_output, check):
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with patch.dict(
                os.environ,
                {
                    "SYZABI_ASTERINAS_PACKAGE_SLOT": "7",
                    "SYZABI_INJECT_TRACE_ENABLED": "1",
                },
                clear=False,
            ), patch("tools.run_asterinas.subprocess.run", side_effect=fake_run):
                materialize_guest_env_file(image)

            header = image.read_bytes()[:1024].decode("utf-8", errors="replace")
            self.assertIn(GUEST_ENV_HEADER_MAGIC, header)
            self.assertIn("SYZABI_PACKAGE_SLOT=7", header)
            self.assertIn("SYZABI_INJECT_TRACE_ENABLED=1", header)

    def test_qemu_logs_are_scoped_to_work_dir(self) -> None:
        work_dir = Path("/tmp/asterinas-run")
        qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
        self.assertEqual(qemu_log_path, work_dir / "qemu.log")
        self.assertEqual(qemu_serial_log_path, work_dir / "qemu-serial.log")

    def test_host_osdk_env_sets_per_run_qemu_log_paths(self) -> None:
        work_dir = Path("/tmp/asterinas-run")
        with patch("tools.run_asterinas.ensure_vdso_dir", return_value=Path("/vdso")), patch(
            "tools.run_asterinas.ensure_local_mtools", return_value=None
        ):
            env = host_osdk_env(work_dir)
        self.assertEqual(env["BOOT_METHOD"], "qemu-direct")
        self.assertEqual(env["NETDEV"], "none")
        self.assertEqual(env["QEMU_LOG_FILE"], str(work_dir / "qemu.log"))
        self.assertEqual(env["QEMU_SERIAL_LOG_FILE"], str(work_dir / "qemu-serial.log"))
        self.assertEqual(env["QEMU_DISPLAY"], "none")
        self.assertEqual(env["RUSTUP_TOOLCHAIN"], "nightly-2025-12-06")

    def test_kvm_enabled_honors_explicit_disable_switch(self) -> None:
        self.assertFalse(kvm_enabled({"SYZABI_ASTERINAS_ENABLE_KVM": "0"}))
        self.assertTrue(kvm_enabled({}))

    def test_qemu_direct_command_honors_disable_kvm_env(self) -> None:
        cfg = {"asterinas": {"repo_dir": "third_party/asterinas"}}
        manifest = {"config": {"run": {"qemu": {"path": "/usr/bin/qemu-system-x86_64"}}}}
        with patch("tools.run_asterinas.load_bundle_manifest", return_value=manifest), patch(
            "tools.run_asterinas.bundle_kcmdline", return_value="console=hvc0"
        ), patch("tools.run_asterinas.qemu_args_tokens", return_value=["-m", "2G"]), patch(
            "tools.run_asterinas.shared_bzimage_path", return_value=Path("/tmp/bzImage")
        ), patch("tools.run_asterinas.resolve_repo_path", return_value=Path("/tmp/asterinas")), patch(
            "tools.run_asterinas.kvm_accessible", return_value=True
        ):
            command, workdir = qemu_direct_command(
                cfg,
                Path("/tmp/initramfs.cpio.gz"),
                {"SYZABI_ASTERINAS_ENABLE_KVM": "0"},
            )
        self.assertEqual(workdir, Path("/tmp/asterinas"))
        self.assertNotIn("-accel", command)

    def test_qemu_direct_command_adds_kvm_when_enabled(self) -> None:
        cfg = {"asterinas": {"repo_dir": "third_party/asterinas"}}
        manifest = {"config": {"run": {"qemu": {"path": "/usr/bin/qemu-system-x86_64"}}}}
        with patch("tools.run_asterinas.load_bundle_manifest", return_value=manifest), patch(
            "tools.run_asterinas.bundle_kcmdline", return_value="console=hvc0"
        ), patch("tools.run_asterinas.qemu_args_tokens", return_value=["-m", "2G"]), patch(
            "tools.run_asterinas.shared_bzimage_path", return_value=Path("/tmp/bzImage")
        ), patch("tools.run_asterinas.resolve_repo_path", return_value=Path("/tmp/asterinas")), patch(
            "tools.run_asterinas.kvm_accessible", return_value=True
        ):
            command, _ = qemu_direct_command(cfg, Path("/tmp/initramfs.cpio.gz"), {})
        self.assertEqual(command[-2:], ["-accel", "kvm"])

    def test_containerized_qemu_direct_command_forwards_guest_kcmd_args(self) -> None:
        cfg = {"asterinas": {"repo_dir": "third_party/asterinas", "docker_workspace_dir": "/workspace"}}
        observed_env = {}

        def fake_qemu_direct_command(cfg_arg, initramfs_arg, env_arg):
            observed_env.update(env_arg)
            return (["/usr/bin/qemu-system-x86_64"], Path("/tmp/asterinas"))

        with patch("tools.run_asterinas.host_osdk_env", return_value={}), patch(
            "tools.run_asterinas.qemu_direct_command",
            side_effect=fake_qemu_direct_command,
        ), patch("tools.run_asterinas.repo_root", return_value=Path("/workspace")), patch(
            "tools.run_asterinas.docker_workspace_dir",
            return_value=Path("/workspace"),
        ), patch("tools.run_asterinas.kvm_enabled", return_value=False):
            command = containerized_qemu_direct_command(
                cfg,
                Path("/tmp/work"),
                Path("/tmp/initramfs.cpio.gz"),
                guest_kcmd_args="console=hvc0 syzabi_slot=7",
            )
        self.assertEqual(command, ["/usr/bin/qemu-system-x86_64"])
        self.assertEqual(observed_env["SYZABI_GUEST_KCMD_ARGS"], "console=hvc0 syzabi_slot=7")

    def test_docker_qemu_direct_script_execs_qemu_direct_command(self) -> None:
        with patch(
            "tools.run_asterinas.containerized_qemu_direct_command",
            return_value=["/usr/bin/qemu-system-x86_64", "-initrd", "/tmp/initramfs.cpio.gz"],
        ):
            script = docker_qemu_direct_script(
                {"asterinas": {"docker_workspace_dir": "/workspace"}},
                Path("/tmp/work"),
                Path("/tmp/initramfs.cpio.gz"),
                guest_kcmd_args="console=hvc0",
            )
        self.assertTrue(script.startswith("set -euo pipefail"))
        self.assertIn("exec /usr/bin/qemu-system-x86_64 -initrd /tmp/initramfs.cpio.gz", script)

    def test_containerized_grub_iso_command_rewrites_workspace_paths(self) -> None:
        cfg = {"asterinas": {"repo_dir": "third_party/asterinas", "docker_workspace_dir": "/workspace"}}
        with patch("tools.run_asterinas.host_osdk_env", return_value={}), patch(
            "tools.run_asterinas.grub_iso_qemu_command",
            return_value=(["/usr/bin/qemu-system-x86_64", "-drive", "file=/home/plucky/FuzzAsterinas/pkg/aster.iso"], Path("/tmp/asterinas")),
        ), patch("tools.run_asterinas.repo_root", return_value=Path("/home/plucky/FuzzAsterinas")), patch(
            "tools.run_asterinas.docker_workspace_dir",
            return_value=Path("/workspace"),
        ), patch("tools.run_asterinas.kvm_enabled", return_value=False), patch(
            "tools.run_asterinas.prepare_ovmf_vars",
            return_value=Path("/tmp/work/OVMF_VARS.fd"),
        ), patch(
            "tools.run_asterinas.system_ovmf_code_path",
            return_value=Path("/usr/share/OVMF/OVMF_CODE_4M.fd"),
        ), patch(
            "tools.run_asterinas.host_path_to_container_path",
            side_effect=lambda path, cfg: Path("/workspace") / Path(path).name,
        ):
            command = containerized_grub_iso_command(cfg, Path("/home/plucky/FuzzAsterinas/pkg"), Path("/tmp/work"))
        self.assertIn("file=/workspace/pkg/aster.iso", command[2])

    def test_host_grub_bundle_command_uses_shared_package_bundle(self) -> None:
        cfg = {"asterinas": {"repo_dir": "third_party/asterinas"}}
        package_dir = Path("/tmp/package")
        with patch("tools.run_asterinas.host_osdk_env", return_value={}), patch(
            "tools.run_asterinas.shared_package_bundle_dir",
            return_value=Path("/tmp/package/bundle"),
        ), patch(
            "tools.run_asterinas.grub_iso_qemu_command",
            return_value=(["/usr/bin/qemu-system-x86_64"], Path("/tmp/asterinas")),
        ) as grub_iso_qemu_command, patch(
            "tools.run_asterinas.prepare_ovmf_vars",
            return_value=Path("/tmp/work/OVMF_VARS.fd"),
        ), patch(
            "tools.run_asterinas.system_ovmf_code_path",
            return_value=Path("/usr/share/OVMF/OVMF_CODE_4M.fd"),
        ):
            command, cwd = host_grub_bundle_command(cfg, package_dir, Path("/tmp/work"))
        self.assertEqual(command, ["/usr/bin/qemu-system-x86_64"])
        self.assertEqual(cwd, Path("/tmp/asterinas"))
        grub_iso_qemu_command.assert_called_once()
        self.assertEqual(grub_iso_qemu_command.call_args.args[1], Path("/tmp/package/bundle"))

    def test_docker_grub_bundle_script_execs_rewritten_qemu(self) -> None:
        with patch(
            "tools.run_asterinas.containerized_grub_iso_command",
            return_value=["/usr/bin/qemu-system-x86_64", "-drive", "file=/workspace/pkg/aster.iso"],
        ):
            script = docker_grub_bundle_script(
                {"asterinas": {"docker_workspace_dir": "/workspace"}},
                Path("/workspace/pkg"),
                Path("/tmp/work"),
            )
        self.assertTrue(script.startswith("set -euo pipefail"))
        self.assertIn("exec /usr/bin/qemu-system-x86_64 -drive file=/workspace/pkg/aster.iso", script)

    def test_prepare_run_cargo_home_uses_relative_registry_link_and_copies_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            shared_home = workspace / "shared"
            work_dir = workspace / "work"
            (shared_home / "registry" / "index").mkdir(parents=True)
            (shared_home / "git" / "db").mkdir(parents=True)
            (shared_home / ".crates.toml").write_text("v1", encoding="utf-8")
            (shared_home / "registry" / "index" / "config.json").write_text("{}", encoding="utf-8")
            with patch("tools.run_asterinas.docker_cargo_home", return_value=shared_home):
                run_home = prepare_run_cargo_home(work_dir)
            registry_target = run_home / "registry"
            self.assertTrue(registry_target.is_symlink())
            self.assertFalse(registry_target.readlink().is_absolute())
            self.assertTrue(registry_target.exists())
            self.assertEqual((run_home / ".crates.toml").read_text(encoding="utf-8"), "v1")
            self.assertTrue((run_home / "git" / "db").is_dir())

    def test_prime_docker_cargo_cache_uses_host_gitconfig(self) -> None:
        from tools.run_asterinas import prime_docker_cargo_cache

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cargo_home = root / "cargo-home"
            repo_dir = root / "asterinas"
            repo_dir.mkdir()
            (repo_dir / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            host_gitconfig = root / "host-gitconfig"
            host_gitconfig.write_text("", encoding="utf-8")
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("tools.run_asterinas.docker_cargo_home", return_value=cargo_home), patch(
                "tools.run_asterinas.ensure_docker_cargo_cache_dirs",
            ), patch(
                "tools.run_asterinas.prepare_host_gitconfig",
                return_value=host_gitconfig,
            ), patch(
                "tools.run_asterinas.local_tmp_dir",
                return_value=root / "tmp",
            ), patch(
                "tools.run_asterinas.subprocess.run",
                return_value=completed,
            ) as subprocess_run:
                prime_docker_cargo_cache(
                    {
                        "asterinas": {
                            "repo_dir": str(repo_dir),
                            "build_timeout_sec": 60,
                        }
                    }
                )

        env = subprocess_run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(host_gitconfig))
        self.assertEqual(env["CARGO_HOME"], str(cargo_home))

    def test_ensure_git_mirror_keeps_existing_clone_when_remote_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mirror_root = Path(tmpdir)
            mirror_path = mirror_root / "inherit-methods-macro.git"
            mirror_path.mkdir()
            (mirror_path / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            failed_update = SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="fatal: unable to access 'https://github.com/asterinas/inherit-methods-macro/': temporary failure",
            )
            with patch("tools.run_asterinas.asterinas_git_mirror_root", return_value=mirror_root), patch(
                "tools.run_asterinas.subprocess.run",
                return_value=failed_update,
            ):
                resolved = ensure_git_mirror("inherit-methods-macro", "https://github.com/asterinas/inherit-methods-macro")
        self.assertEqual(resolved, mirror_path)

    def test_docker_gitconfig_reuses_existing_mirrors_without_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mirror_root = Path(tmpdir)
            mirror_path = mirror_root / "inherit-methods-macro.git"
            mirror_path.mkdir()
            (mirror_path / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            with patch("tools.run_asterinas.asterinas_git_mirror_root", return_value=mirror_root), patch(
                "tools.run_asterinas.ensure_asterinas_git_mirrors",
                side_effect=AssertionError("unexpected mirror refresh"),
            ):
                lines = gitconfig_lines(
                    {"asterinas": {"docker_workspace_dir": "/workspace"}},
                    path_transform=lambda path, _: path,
                    ensure_mirrors=False,
                )
        joined = "\n".join(lines)
        self.assertIn("file://" + str(mirror_path), joined)
        self.assertNotIn("inventory.git", joined)

    def test_should_fallback_to_host_direct_only_for_docker_access_errors(self) -> None:
        self.assertTrue(
            should_fallback_to_host_direct(
                RunnerError(
                    "docker: permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
                )
            )
        )
        self.assertFalse(should_fallback_to_host_direct(RunnerError("failed to locate system OVMF code image")))

    def test_main_falls_back_to_host_direct_when_docker_daemon_is_unavailable(self) -> None:
        from tools import run_asterinas

        args = SimpleNamespace(
            binary="/tmp/testcase.bin",
            batch_manifest=None,
            work_dir="/tmp/work",
            healthcheck=False,
            mode="docker-qemu",
        )
        with patch("tools.run_asterinas.parse_args", return_value=args), patch(
            "tools.run_asterinas.docker_qemu_run",
            side_effect=RunnerError(
                "docker: permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
            ),
        ), patch("tools.run_asterinas.host_direct_run") as host_direct_run, patch(
            "tools.run_asterinas.write_runner_result"
        ):
            run_asterinas.main()
        host_direct_run.assert_called_once_with(args)

    def test_adapter_healthcheck_dispatches_via_prepare_target(self) -> None:
        adapter = AsterinasTargetAdapter()
        args = SimpleNamespace(mode="docker-qemu")
        with patch("targets.asterinas.runner_impl.read_workflow_config", return_value={"asterinas": {"repo_dir": "x"}}), patch(
            "targets.asterinas.adapter.AsterinasTargetAdapter.prepare_target",
            return_value="rev1234567890",
        ) as prepare_target, patch("targets.asterinas.runner_impl.write_runner_result") as write_runner_result:
            adapter.healthcheck(args)
        prepare_target.assert_called_once()
        write_runner_result.assert_called_once_with({"status": "ok", "exit_code": 0, "kernel_build": "asterinas@rev123456789"})

    def test_adapter_run_case_dispatches_modes(self) -> None:
        adapter = AsterinasTargetAdapter()
        local_args = SimpleNamespace(mode="local-proxy")
        docker_args = SimpleNamespace(mode="docker-qemu")
        with patch("targets.asterinas.runner_impl.local_proxy") as local_proxy, patch(
            "targets.asterinas.runner_impl.docker_qemu_run"
        ) as docker_qemu_run, patch("targets.asterinas.runner_impl.host_direct_run") as host_direct_run:
            adapter.run_case(local_args)
            adapter.run_case(docker_args)
        local_proxy.assert_called_once_with(local_args)
        docker_qemu_run.assert_called_once_with(docker_args)
        host_direct_run.assert_not_called()

    def test_host_direct_run_validates_packaged_bundle_before_reuse(self) -> None:
        from tools import run_asterinas

        class FakePopen:
            def __init__(self, args, **_kwargs):
                self.args = args

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work_dir = root / "work"
            work_dir.mkdir()
            binary_path = root / "testcase.bin"
            binary_path.write_text("", encoding="utf-8")
            package_dir = root / "package"
            bundle_dir = package_dir / "cargo-target" / "osdk" / "aster-kernel"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "bundle.toml").write_text("[bundle]\n", encoding="utf-8")
            initramfs_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
            initramfs_path.write_bytes(b"initramfs")
            ovmf_code = root / "OVMF_CODE.fd"
            ovmf_vars = root / "OVMF_VARS.fd"
            ovmf_code.write_text("", encoding="utf-8")
            ovmf_vars.write_text("", encoding="utf-8")
            ext2_image = root / "ext2.img"
            exfat_image = root / "exfat.img"
            ext2_image.write_text("", encoding="utf-8")
            exfat_image.write_text("", encoding="utf-8")
            env_paths = {
                "SYZABI_STDOUT_PATH": root / "stdout.txt",
                "SYZABI_STDERR_PATH": root / "stderr.txt",
                "SYZABI_CONSOLE_LOG_PATH": root / "console.log",
                "SYZABI_RAW_TRACE_PATH": root / "raw-trace.json",
                "SYZABI_EXTERNAL_STATE_PATH": root / "external-state.json",
            }
            console_text = "\n".join(
                [
                    "__SYZABI_BEGIN_PROCESS_EXIT__",
                    '{"status":"ok","exit_code":0,"timed_out":false}',
                    "__SYZABI_END_PROCESS_EXIT__",
                    "__SYZABI_BEGIN_STDOUT__",
                    "",
                    "__SYZABI_END_STDOUT__",
                    "__SYZABI_BEGIN_STDERR__",
                    "",
                    "__SYZABI_END_STDERR__",
                    "__SYZABI_BEGIN_EVENTS__",
                    "",
                    "__SYZABI_END_EVENTS__",
                    "__SYZABI_BEGIN_EXTERNAL_STATE__",
                    '{"files":[]}',
                    "__SYZABI_END_EXTERNAL_STATE__",
                ]
            )
            args = SimpleNamespace(binary=str(binary_path), work_dir=str(work_dir))

            def fake_required_env(name: str) -> Path:
                return env_paths[name]

            def fake_env_path(name: str) -> Path | None:
                if name == "SYZABI_ASTERINAS_PACKAGE_DIR":
                    return package_dir
                return None

            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.read_workflow_config",
                        return_value={
                            "asterinas": {"docker_image": "image", "run_timeout_sec": 10},
                            "normalization": {"preview_bytes": 32},
                        },
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.required_env_path",
                        side_effect=fake_required_env,
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.runner_result_path",
                        return_value=root / "runner-result.json",
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.env_path",
                        side_effect=fake_env_path,
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.selected_initramfs",
                        return_value=initramfs_path,
                    )
                )
                ensure_packaged_docker_bundle = stack.enter_context(
                    patch("tools.run_asterinas.ensure_packaged_docker_bundle")
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.ensure_revision",
                        return_value="rev123",
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.ensure_host_build",
                        side_effect=AssertionError("host build should not be used when validated bundle is reusable"),
                    )
                )
                stack.enter_context(patch("tools.run_asterinas.ensure_dummy_block_images"))
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.qemu_log_paths",
                        return_value=(root / "qemu.log", root / "qemu.serial.log"),
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.prepare_run_block_images",
                        return_value=(ext2_image, exfat_image),
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.host_osdk_env",
                        return_value={},
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.system_ovmf_code_path",
                        return_value=ovmf_code,
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.prepare_ovmf_vars",
                        return_value=ovmf_vars,
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.host_grub_bundle_command",
                        return_value=(["true"], root),
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.subprocess.Popen",
                        side_effect=lambda *popen_args, **popen_kwargs: FakePopen(*popen_args, **popen_kwargs),
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.read_console_text",
                        return_value=console_text,
                    )
                )
                stack.enter_context(patch("tools.run_asterinas.stop_qemu_processes"))
                stack.enter_context(patch("tools.run_asterinas.stop_process"))
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.selected_guest_cmdline_append",
                        return_value="",
                    )
                )
                stack.enter_context(patch("tools.run_asterinas.write_runner_result"))
                run_asterinas.host_direct_run(args)

        ensure_packaged_docker_bundle.assert_called_once_with(
            {"asterinas": {"docker_image": "image", "run_timeout_sec": 10}, "normalization": {"preview_bytes": 32}},
            package_dir.resolve(),
            initramfs_path,
            kcmd_args="console=hvc0",
        )

    def test_docker_qemu_run_skips_global_build_when_package_dir_present(self) -> None:
        from tools import run_asterinas

        class FakePopen:
            def __init__(self, args, **_kwargs):
                self.args = args

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            work_dir = root / "work"
            work_dir.mkdir()
            binary_path = root / "testcase.bin"
            binary_path.write_text("", encoding="utf-8")
            package_dir = root / "package"
            package_dir.mkdir()
            bundle_dir = package_dir / "cargo-target" / "osdk" / "aster-kernel"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "bundle.toml").write_text("[bundle]\n", encoding="utf-8")
            initramfs_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
            initramfs_path.write_bytes(b"initramfs")
            ext2_image = root / "ext2.img"
            exfat_image = root / "exfat.img"
            ext2_image.write_text("", encoding="utf-8")
            exfat_image.write_text("", encoding="utf-8")
            env_paths = {
                "SYZABI_STDOUT_PATH": root / "stdout.txt",
                "SYZABI_STDERR_PATH": root / "stderr.txt",
                "SYZABI_CONSOLE_LOG_PATH": root / "console.log",
                "SYZABI_RAW_TRACE_PATH": root / "raw-trace.json",
                "SYZABI_EXTERNAL_STATE_PATH": root / "external-state.json",
            }
            console_text = "\n".join(
                [
                    "__SYZABI_BEGIN_PROCESS_EXIT__",
                    '{"status":"ok","exit_code":0,"timed_out":false}',
                    "__SYZABI_END_PROCESS_EXIT__",
                    "__SYZABI_BEGIN_STDOUT__",
                    "",
                    "__SYZABI_END_STDOUT__",
                    "__SYZABI_BEGIN_STDERR__",
                    "",
                    "__SYZABI_END_STDERR__",
                    "__SYZABI_BEGIN_EVENTS__",
                    "",
                    "__SYZABI_END_EVENTS__",
                    "__SYZABI_BEGIN_EXTERNAL_STATE__",
                    '{"files":[]}',
                    "__SYZABI_END_EXTERNAL_STATE__",
                ]
            )
            args = SimpleNamespace(binary=str(binary_path), work_dir=str(work_dir))

            def fake_required_env(name: str) -> Path:
                return env_paths[name]

            def fake_env_path(name: str) -> Path | None:
                if name == "SYZABI_ASTERINAS_PACKAGE_DIR":
                    return package_dir
                return None

            with ExitStack() as stack:
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.read_workflow_config",
                        return_value={
                            "asterinas": {"docker_image": "image", "run_timeout_sec": 10},
                            "normalization": {"preview_bytes": 32},
                        },
                    )
                )
                stack.enter_context(patch("tools.run_asterinas.required_env_path", side_effect=fake_required_env))
                stack.enter_context(patch("tools.run_asterinas.runner_result_path", return_value=root / "runner-result.json"))
                stack.enter_context(patch("tools.run_asterinas.env_path", side_effect=fake_env_path))
                stack.enter_context(patch("tools.run_asterinas.selected_initramfs", return_value=initramfs_path))
                stack.enter_context(patch("tools.run_asterinas.ensure_dummy_block_images"))
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.qemu_log_paths",
                        return_value=(root / "qemu.log", root / "qemu.serial.log"),
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.prepare_run_block_images",
                        return_value=(ext2_image, exfat_image),
                    )
                )
                stack.enter_context(patch("tools.run_asterinas.container_name_for_run", return_value="container"))
                stack.enter_context(patch("tools.run_asterinas.docker_run_env", return_value={}))
                stack.enter_context(patch("tools.run_asterinas.host_path_to_container_path", side_effect=lambda path, cfg: path))
                stack.enter_context(patch("tools.run_asterinas.docker_repo_dir", return_value=root))
                stack.enter_context(patch("tools.run_asterinas.ensure_packaged_docker_bundle"))
                stack.enter_context(patch("tools.run_asterinas.ensure_shared_package_cargo_home", return_value=root / "cargo-home"))
                stack.enter_context(patch("tools.run_asterinas.shared_package_runtime_dirs", return_value=(root / "cargo-target", root / "osdk-output")))
                stack.enter_context(patch("tools.run_asterinas.docker_grub_bundle_script", return_value="script"))
                stack.enter_context(patch("tools.run_asterinas.docker_run_command", return_value=["true"]))
                stack.enter_context(patch("tools.run_asterinas.ensure_revision", return_value="rev123"))
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.ensure_docker_build",
                        side_effect=AssertionError("packaged runs should not require ensure_docker_build"),
                    )
                )
                stack.enter_context(
                    patch(
                        "tools.run_asterinas.subprocess.Popen",
                        side_effect=lambda *popen_args, **popen_kwargs: FakePopen(*popen_args, **popen_kwargs),
                    )
                )
                stack.enter_context(patch("tools.run_asterinas.read_console_text", return_value=console_text))
                stack.enter_context(patch("tools.run_asterinas.force_remove_container"))
                stack.enter_context(patch("tools.run_asterinas.stop_process"))
                stack.enter_context(patch("tools.run_asterinas.selected_guest_cmdline_append", return_value=""))
                stack.enter_context(patch("tools.run_asterinas.write_runner_result"))
                run_asterinas.docker_qemu_run(args)

    def test_write_missing_marker_crash_result_materializes_crash_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw_trace_path = root / "raw-trace.json"
            external_state_path = root / "external-state.json"
            runner_result = root / "runner-result.json"
            console_text = "\n".join(
                [
                    "[kernel] unpacking initramfs.cpio.gz to rootfs ...",
                    "Printing stack trace:",
                    "frame 0",
                ]
            )
            with patch.dict(
                os.environ,
                {
                    "SYZABI_PROGRAM_ID": "prog",
                    "SYZABI_SIDE": "candidate",
                    "SYZABI_RUN_ID": "run-1",
                    "SYZABI_RUNNER_RESULT_PATH": str(runner_result),
                },
                clear=False,
            ):
                handled = write_missing_marker_crash_result(
                    console_text=console_text,
                    raw_trace_path=raw_trace_path,
                    external_state_path=external_state_path,
                    kernel_build="asterinas@deadbeef",
                )
            self.assertTrue(handled)
            self.assertEqual(
                guest_crash_detail(console_text),
                "guest crashed before emitting autorun markers (kernel stack trace observed)",
            )
            self.assertEqual(json.loads(raw_trace_path.read_text(encoding="utf-8"))["status"], "crash")
            self.assertEqual(json.loads(external_state_path.read_text(encoding="utf-8")), {"files": []})
            runner_payload = json.loads(runner_result.read_text(encoding="utf-8"))
            self.assertEqual(runner_payload["status"], "crash")
            self.assertEqual(runner_payload["kernel_build"], "asterinas@deadbeef")

    def test_target_osdk_dir_prefers_recorded_build_info_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            info_path = Path(tmpdir) / "build-info.json"
            info_path.write_text(json.dumps({"target_dir": "/tmp/host-osdk"}), encoding="utf-8")
            with patch("tools.run_asterinas.build_info_path", return_value=info_path):
                self.assertEqual(target_osdk_dir({"asterinas": {"build_info_path": "unused"}}), Path("/tmp/host-osdk"))

    def test_parallel_scheduler_preserves_input_order(self) -> None:
        entries = [{"program_id": "alpha"}, {"program_id": "beta"}, {"program_id": "gamma"}]

        def fake_schedule_one(entry, args):
            delays = {"alpha": 0.05, "beta": 0.02, "gamma": 0.0}
            time.sleep(delays[entry["program_id"]])
            return {"program_id": entry["program_id"]}

        with patch("orchestrator.scheduler.schedule_one", side_effect=fake_schedule_one):
            results = scheduler.schedule_entries(entries, SimpleNamespace(candidate_batch_size=1, controlled_divergence=False), jobs=3)
        self.assertEqual([result["program_id"] for result in results], ["alpha", "beta", "gamma"])

    def test_batch_scheduler_preserves_input_order(self) -> None:
        entries = [{"program_id": "alpha"}, {"program_id": "beta"}, {"program_id": "gamma"}]
        observed_max_workers: list[int | None] = []

        def fake_prepare_case(entry, args):
            return {
                "kind": "candidate_ready",
                "entry": entry,
                "program_id": entry["program_id"],
                "run_id": f"run-{entry['program_id']}",
                "inject_trace": None,
                "reference_results": [],
                "reference_hashes": ["stable"],
                "current_reference_canonical": {"events": []},
            }

        def fake_execute_candidate_batch(*, batch_cases, timeout_sec, max_workers=None):
            observed_max_workers.append(max_workers)
            return {
                case["program_id"]: SimpleNamespace(status="ok")
                for case in batch_cases
            }

        def fake_finalize(prepared, args, candidate_result, candidate_canonical, **kwargs):
            return {"program_id": prepared["program_id"]}

        with patch("orchestrator.scheduler.prepare_case", side_effect=fake_prepare_case), patch(
            "orchestrator.scheduler.execute_candidate_batch_with_context",
            side_effect=lambda **kwargs: (
                fake_execute_candidate_batch(**kwargs),
                Path("/tmp/package"),
                {case["program_id"]: idx for idx, case in enumerate(kwargs["batch_cases"])},
            ),
        ), patch("orchestrator.scheduler.finalize_prepared_case", side_effect=fake_finalize), patch(
            "orchestrator.scheduler.load_canonical", return_value={"events": []}
        ):
            results = scheduler.schedule_entries(entries, SimpleNamespace(candidate_batch_size=2, controlled_divergence=False), jobs=2)
        self.assertEqual([result["program_id"] for result in results], ["alpha", "beta", "gamma"])
        self.assertEqual(observed_max_workers, [2, 2])

    def test_batch_scheduler_parallelizes_finalize_phase(self) -> None:
        entries = [{"program_id": "alpha"}, {"program_id": "beta"}]
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_prepare_case(entry, args):
            return {
                "kind": "candidate_ready",
                "entry": entry,
                "program_id": entry["program_id"],
                "run_id": f"run-{entry['program_id']}",
                "inject_trace": None,
                "reference_results": [],
                "reference_hashes": ["stable"],
                "current_reference_canonical": {"events": []},
            }

        def fake_execute_candidate_batch(*, batch_cases, timeout_sec, max_workers=None):
            return {
                case["program_id"]: SimpleNamespace(status="ok")
                for case in batch_cases
            }

        def fake_finalize(prepared, args, candidate_result, candidate_canonical, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"program_id": prepared["program_id"]}

        with patch("orchestrator.scheduler.prepare_case", side_effect=fake_prepare_case), patch(
            "orchestrator.scheduler.execute_candidate_batch_with_context",
            side_effect=lambda **kwargs: (
                fake_execute_candidate_batch(**kwargs),
                Path("/tmp/package"),
                {case["program_id"]: idx for idx, case in enumerate(kwargs["batch_cases"])},
            ),
        ), patch("orchestrator.scheduler.finalize_prepared_case", side_effect=fake_finalize), patch(
            "orchestrator.scheduler.load_canonical", return_value={"events": []}
        ):
            results = scheduler.schedule_entries(entries, SimpleNamespace(candidate_batch_size=2, controlled_divergence=False), jobs=2)
        self.assertEqual([result["program_id"] for result in results], ["alpha", "beta"])
        self.assertGreaterEqual(max_active, 2)

    def test_batch_scheduler_converts_prepare_exception_to_infra_error(self) -> None:
        entries = [{"program_id": "alpha"}, {"program_id": "beta"}]

        def fake_prepare_case(entry, args):
            if entry["program_id"] == "beta":
                raise OSError("boom")
            return {
                "kind": "candidate_ready",
                "entry": entry,
                "program_id": entry["program_id"],
                "run_id": f"run-{entry['program_id']}",
                "inject_trace": None,
                "reference_results": [],
                "reference_hashes": ["stable"],
                "current_reference_canonical": {"events": []},
            }

        with patch("orchestrator.scheduler.prepare_case", side_effect=fake_prepare_case), patch(
            "orchestrator.scheduler.execute_candidate_batch_with_context",
            side_effect=lambda **kwargs: (
                {case["program_id"]: SimpleNamespace(status="ok") for case in kwargs["batch_cases"]},
                Path("/tmp/package"),
                {case["program_id"]: idx for idx, case in enumerate(kwargs["batch_cases"])},
            ),
        ), patch(
            "orchestrator.scheduler.finalize_prepared_case",
            side_effect=lambda prepared, args, candidate_result, candidate_canonical, **kwargs: {
                "program_id": prepared["program_id"],
                "classification": "NO_DIFF",
            },
        ):
            results = scheduler.schedule_entries(entries, SimpleNamespace(candidate_batch_size=2, controlled_divergence=False), jobs=2)

        self.assertEqual([result["program_id"] for result in results], ["alpha", "beta"])
        self.assertEqual(results[1]["classification"], "infra_error")
        self.assertEqual(results[1]["error_stage"], "prepare_case")

    def test_batch_scheduler_converts_batch_exception_to_infra_error(self) -> None:
        entries = [{"program_id": "alpha"}, {"program_id": "beta"}]

        def fake_prepare_case(entry, args):
            return {
                "kind": "candidate_ready",
                "entry": entry,
                "program_id": entry["program_id"],
                "run_id": f"run-{entry['program_id']}",
                "inject_trace": None,
                "reference_results": [],
                "reference_hashes": ["stable"],
                "current_reference_canonical": {"events": []},
            }

        with patch("orchestrator.scheduler.prepare_case", side_effect=fake_prepare_case), patch(
            "orchestrator.scheduler.execute_candidate_batch_with_context",
            side_effect=RuntimeError("batch failed"),
        ):
            results = scheduler.schedule_entries(entries, SimpleNamespace(candidate_batch_size=2, controlled_divergence=False), jobs=2)

        self.assertEqual([result["classification"] for result in results], ["infra_error", "infra_error"])
        self.assertEqual(results[0]["error_stage"], "execute_candidate_batch_with_context")

    def test_finalize_prepared_case_reuses_packaged_candidate_for_triage(self) -> None:
        prepared = {
            "entry": {"program_id": "alpha", "normalized_path": "/tmp/alpha.syz", "meta_path": "/tmp/alpha.json", "scml_preflight_status": "passed"},
            "program_id": "alpha",
            "run_id": "run-alpha",
            "inject_trace": None,
            "reference_results": [],
            "reference_hashes": ["stable"],
            "current_reference_canonical": {"events": []},
        }

        class FakeResult(SimpleNamespace):
            def to_dict(self):
                return {"status": self.status, "trace_json_path": "/tmp/fake-trace.json"}

        with patch("orchestrator.scheduler.config", return_value={
            "stability": {"rerun_count": 1, "timeout_sec": 120},
            "classification": {
                "baseline_invalid": "BASELINE_INVALID",
                "no_diff": "NO_DIFF",
            },
        }), patch(
            "orchestrator.scheduler.run_reference_once",
            return_value=(FakeResult(status="ok"), {"events": []}),
        ), patch(
            "orchestrator.scheduler.execute_candidate_case_in_package",
            return_value=FakeResult(status="ok"),
        ) as packaged_run, patch(
            "orchestrator.scheduler.run_candidate_once",
            side_effect=AssertionError("unexpected non-packaged candidate rerun"),
        ), patch(
            "orchestrator.scheduler.load_canonical",
            return_value={"events": []},
        ), patch(
            "orchestrator.scheduler.canonical_trace_hash",
            return_value="stable",
        ), patch(
            "orchestrator.scheduler.compare_canonical",
            side_effect=[{"equivalent": False}, {"equivalent": True}, {"equivalent": True}],
        ), patch(
            "orchestrator.scheduler.classify_result",
            return_value="NO_DIFF",
        ):
            result = scheduler.finalize_prepared_case(
                prepared,
                SimpleNamespace(controlled_divergence=False),
                FakeResult(status="ok"),
                {"events": []},
                candidate_package_dir=Path("/tmp/package"),
                candidate_package_slot=3,
            )

        self.assertEqual(result["classification"], "NO_DIFF")
        packaged_run.assert_called_once()

    def test_finalize_prepared_case_marks_crash_with_comparison_as_diverged(self) -> None:
        class FakeResult(SimpleNamespace):
            def to_dict(self):
                return {"status": self.status, "trace_json_path": "/tmp/fake-trace.json"}

        prepared = {
            "entry": {"program_id": "alpha", "normalized_path": "/tmp/alpha.syz", "meta_path": "/tmp/alpha.json", "scml_preflight_status": "passed"},
            "program_id": "alpha",
            "run_id": "run-alpha",
            "inject_trace": None,
            "reference_results": [FakeResult(status="ok")],
            "reference_hashes": ["stable"],
            "current_reference_canonical": {"events": [{"index": 0, "syscall_name": "close"}]},
        }

        with patch("orchestrator.scheduler.config", return_value={
            "stability": {"rerun_count": 0, "timeout_sec": 120},
            "classification": {
                "baseline_invalid": "BASELINE_INVALID",
                "no_diff": "NO_DIFF",
                "bug_likely": "BUG_LIKELY",
                "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                "unsupported_feature": "UNSUPPORTED_FEATURE",
            },
        }), patch(
            "orchestrator.scheduler.classify_result",
            return_value="BUG_LIKELY",
        ), patch(
            "orchestrator.scheduler.compare_canonical",
            return_value={"equivalent": False, "first_divergence_index": 0, "noise_only": False},
        ):
            result = scheduler.finalize_prepared_case(
                prepared,
                SimpleNamespace(controlled_divergence=False),
                FakeResult(status="crash"),
                {"events": [{"index": 0, "syscall_name": "close"}]},
            )

        self.assertEqual(result["classification"], "BUG_LIKELY")
        self.assertEqual(result["scml_result_bucket"], "passed_scml_and_diverged")

    def test_scml_result_bucket_marks_reference_only_failures_separately(self) -> None:
        bucket = scheduler.scml_result_bucket(
            preflight_status="passed",
            candidate_status="ok",
            classification="BASELINE_INVALID",
            comparison=None,
            cfg={"classification": {"baseline_invalid": "BASELINE_INVALID"}},
        )
        self.assertEqual(bucket, "passed_scml_but_reference_failed")

    def test_scml_result_bucket_ignores_not_run_preflight_for_non_scml_workflows(self) -> None:
        bucket = scheduler.scml_result_bucket(
            preflight_status="not_run",
            candidate_status="ok",
            classification="NO_DIFF",
            comparison={"equivalent": True},
            cfg={"classification": {"baseline_invalid": "BASELINE_INVALID"}},
        )
        self.assertEqual(bucket, "")

    def test_execute_candidate_batch_runs_each_case_in_isolated_runner(self) -> None:
        prepared_cases = [
            {
                "program_id": "alpha",
                "run_id": "run-alpha",
                "sandbox_root": "/tmp/alpha",
                "artifact_root": "/tmp/alpha-artifacts",
                "binary_path": "/tmp/alpha.bin",
                "stdout_path": "/tmp/alpha.stdout",
                "stderr_path": "/tmp/alpha.stderr",
                "console_path": "/tmp/alpha.console",
                "events_path": "/tmp/alpha.events",
                "raw_trace_path": "/tmp/alpha.trace",
                "external_state_path": "/tmp/alpha.state",
                "runner_result_path": "/tmp/alpha.result",
                "effective_timeout_sec": 10,
                "role": "candidate",
                "snapshot_id": "snap",
                "runner_kind": "command",
            },
            {
                "program_id": "beta",
                "run_id": "run-beta",
                "sandbox_root": "/tmp/beta",
                "artifact_root": "/tmp/beta-artifacts",
                "binary_path": "/tmp/beta.bin",
                "stdout_path": "/tmp/beta.stdout",
                "stderr_path": "/tmp/beta.stderr",
                "console_path": "/tmp/beta.console",
                "events_path": "/tmp/beta.events",
                "raw_trace_path": "/tmp/beta.trace",
                "external_state_path": "/tmp/beta.state",
                "runner_result_path": "/tmp/beta.result",
                "effective_timeout_sec": 10,
                "role": "candidate",
                "snapshot_id": "snap",
                "runner_kind": "command",
            },
        ]

        def fake_execute_prepared_candidate_case(*, case, package_dir, slot):
            return SimpleNamespace(program_id=case["program_id"], slot=slot, package_dir=package_dir)

        with patch("orchestrator.vm_runner.prepare_candidate_batch_case", side_effect=prepared_cases), patch(
            "orchestrator.vm_runner.prepare_candidate_initramfs_package",
            return_value=(Path("/tmp/package"), {"alpha": 0, "beta": 1}),
        ), patch(
            "orchestrator.vm_runner.execute_prepared_candidate_case",
            side_effect=fake_execute_prepared_candidate_case,
        ):
            results = scheduler.execute_candidate_batch(
                batch_cases=[{"program_id": "alpha", "run_id": "run-alpha"}, {"program_id": "beta", "run_id": "run-beta"}],
                timeout_sec=10,
                max_workers=2,
            )
        self.assertEqual(set(results.keys()), {"alpha", "beta"})
        self.assertEqual(results["alpha"].slot, 0)
        self.assertEqual(results["beta"].slot, 1)

    def test_prepare_candidate_initramfs_package_cache_key_includes_template_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            binary = root / "testcase.bin"
            binary.write_bytes(b"bin")
            case = {
                "program_id": "alpha",
                "binary_path": str(binary),
            }
            cfg = {
                "workflow": "asterinas",
                "normalization": {"preview_bytes": 32},
            }
            with patch("orchestrator.vm_runner.candidate_initramfs_package_root", return_value=root), patch(
                "orchestrator.vm_runner.packaged_initramfs_template_inputs",
                side_effect=[
                    {"compose_packaged_autorun": "v1", "busybox_sha256": "a"},
                    {"compose_packaged_autorun": "v2", "busybox_sha256": "a"},
                ],
            ):
                first_dir, _ = prepare_candidate_initramfs_package([case], cfg)
                second_dir, _ = prepare_candidate_initramfs_package([case], cfg)
            self.assertNotEqual(first_dir, second_dir)
            first_manifest = json.loads((first_dir / "package-manifest.json").read_text(encoding="utf-8"))
            self.assertIn("template_inputs", first_manifest)
            self.assertEqual(first_manifest["workflow"], "asterinas")

    def test_ensure_packaged_docker_bundle_rebuilds_on_metadata_mismatch(self) -> None:
        from tools.run_asterinas import ensure_packaged_docker_bundle, packaged_bundle_metadata_path, shared_package_bundle_dir

        with tempfile.TemporaryDirectory() as tmpdir:
            package_dir = Path(tmpdir)
            initramfs_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
            initramfs_path.write_bytes(b"initramfs")
            bundle_dir = shared_package_bundle_dir(package_dir)
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "bundle.toml").write_text("[bundle]\n", encoding="utf-8")
            (package_dir / ".osdk-build.ready").write_text("ready\n", encoding="utf-8")
            packaged_bundle_metadata_path(package_dir).write_text(
                json.dumps(
                    {
                        "docker_image": "old-image",
                        "initramfs_sha256": "stale",
                        "kcmd_args": "old",
                        "revision": "old-revision",
                    }
                ),
                encoding="utf-8",
            )
            shared_cargo_home = package_dir / "shared-cargo-home"
            shared_cargo_home.mkdir()
            build_completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch(
                "tools.run_asterinas.ensure_revision",
                return_value="new-revision",
            ), patch(
                "tools.run_asterinas.prime_docker_cargo_cache",
            ), patch(
                "tools.run_asterinas.ensure_shared_package_cargo_home",
                return_value=shared_cargo_home,
            ), patch(
                "tools.run_asterinas.host_path_to_container_path",
                side_effect=lambda path, cfg: path,
            ), patch(
                "tools.run_asterinas.docker_repo_dir",
                return_value=Path("/repo"),
            ), patch(
                "tools.run_asterinas.docker_run_command",
                return_value=["docker", "run"],
            ) as docker_run_command, patch(
                "tools.run_asterinas.container_name_for_run",
                return_value="bundle-rebuild",
            ), patch(
                "tools.run_asterinas.force_remove_container",
            ), patch(
                "tools.run_asterinas.subprocess.run",
                return_value=build_completed,
            ) as subprocess_run:
                ensure_packaged_docker_bundle(
                    {"asterinas": {"docker_image": "new-image"}},
                    package_dir,
                    initramfs_path,
                    kcmd_args="console=hvc0 quiet",
                )
                metadata = json.loads(packaged_bundle_metadata_path(package_dir).read_text(encoding="utf-8"))
                subprocess_run.assert_called_once()
                self.assertEqual(
                    docker_run_command.call_args.kwargs["extra_env"]["CARGO_NET_OFFLINE"],
                    "true",
                )

        self.assertEqual(metadata["revision"], "new-revision")
        self.assertEqual(metadata["docker_image"], "new-image")
        self.assertEqual(metadata["kcmd_args"], "console=hvc0 quiet")

    def test_ensure_packaged_docker_bundle_cleans_stale_container_on_failure(self) -> None:
        from tools.run_asterinas import ensure_packaged_docker_bundle

        with tempfile.TemporaryDirectory() as tmpdir:
            package_dir = Path(tmpdir)
            initramfs_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
            initramfs_path.write_bytes(b"initramfs")
            shared_cargo_home = package_dir / "shared-cargo-home"
            shared_cargo_home.mkdir()
            failed_build = SimpleNamespace(returncode=1, stdout="", stderr="bundle failed")

            with patch(
                "tools.run_asterinas.ensure_revision",
                return_value="new-revision",
            ), patch(
                "tools.run_asterinas.prime_docker_cargo_cache",
            ), patch(
                "tools.run_asterinas.ensure_shared_package_cargo_home",
                return_value=shared_cargo_home,
            ), patch(
                "tools.run_asterinas.host_path_to_container_path",
                side_effect=lambda path, cfg: path,
            ), patch(
                "tools.run_asterinas.docker_repo_dir",
                return_value=Path("/repo"),
            ), patch(
                "tools.run_asterinas.docker_run_command",
                return_value=["docker", "run"],
            ), patch(
                "tools.run_asterinas.container_name_for_run",
                return_value="bundle-rebuild",
            ), patch(
                "tools.run_asterinas.force_remove_container",
            ) as force_remove_container, patch(
                "tools.run_asterinas.subprocess.run",
                return_value=failed_build,
            ):
                with self.assertRaisesRegex(Exception, "bundle failed"):
                    ensure_packaged_docker_bundle(
                        {"asterinas": {"docker_image": "new-image"}},
                        package_dir,
                        initramfs_path,
                        kcmd_args="console=hvc0",
                    )

        self.assertEqual(force_remove_container.call_count, 2)

    def test_shared_package_runtime_dirs_are_package_scoped(self) -> None:
        from tools.run_asterinas import shared_package_bundle_dir, shared_package_runtime_dirs

        package_dir = Path("/tmp/package")
        cargo_target_dir, osdk_output_dir = shared_package_runtime_dirs(package_dir)
        self.assertEqual(cargo_target_dir, package_dir / "cargo-target")
        self.assertEqual(osdk_output_dir, package_dir / "osdk-output")
        self.assertEqual(shared_package_bundle_dir(package_dir), package_dir / "cargo-target" / "osdk" / "aster-kernel")

    def test_ensure_shared_package_cargo_home_uses_fixed_location(self) -> None:
        from tools.run_asterinas import ensure_shared_package_cargo_home

        with tempfile.TemporaryDirectory() as tmpdir:
            package_dir = Path(tmpdir)
            shared_home = package_dir / "shared-src"
            (shared_home / "registry" / "index").mkdir(parents=True)
            (shared_home / "git" / "db").mkdir(parents=True)
            (shared_home / ".crates.toml").write_text("v1", encoding="utf-8")
            with patch("tools.run_asterinas.docker_cargo_home", return_value=shared_home), patch(
                "tools.run_asterinas.ensure_docker_cargo_cache_dirs", return_value=(shared_home / "git", shared_home / "registry")
            ):
                run_home = ensure_shared_package_cargo_home(package_dir)
        self.assertEqual(run_home, package_dir / "shared-cargo-home")

    def test_ensure_shared_package_cargo_home_refresh_replaces_stale_contents(self) -> None:
        from tools.run_asterinas import ensure_shared_package_cargo_home

        with tempfile.TemporaryDirectory() as tmpdir:
            package_dir = Path(tmpdir)
            shared_home = package_dir / "shared-src"
            (shared_home / "registry" / "index").mkdir(parents=True)
            (shared_home / "git" / "db").mkdir(parents=True)
            (shared_home / ".crates.toml").write_text("fresh", encoding="utf-8")
            (shared_home / ".package-cache").write_text("fresh-cache", encoding="utf-8")
            stale_home = package_dir / "shared-cargo-home"
            (stale_home / "registry").mkdir(parents=True)
            (stale_home / "git").mkdir(parents=True)
            (stale_home / ".crates.toml").write_text("stale", encoding="utf-8")
            with patch("tools.run_asterinas.docker_cargo_home", return_value=shared_home), patch(
                "tools.run_asterinas.ensure_docker_cargo_cache_dirs", return_value=(shared_home / "git", shared_home / "registry")
            ):
                run_home = ensure_shared_package_cargo_home(package_dir, refresh=True)
                self.assertEqual(run_home, stale_home)
                self.assertEqual((stale_home / ".crates.toml").read_text(encoding="utf-8"), "fresh")

    def test_parse_batch_case_results_marks_missing_cases_for_isolation(self) -> None:
        console = "\n".join(
            [
                "__SYZABI_BEGIN_BATCH_CASE__",
                '{"case_index":0,"program_id":"alpha"}',
                "__SYZABI_BEGIN_PROCESS_EXIT__",
                '{"status":"ok","exit_code":0,"timed_out":false}',
                "__SYZABI_END_PROCESS_EXIT__",
                "__SYZABI_BEGIN_STDOUT__",
                "hello",
                "__SYZABI_END_STDOUT__",
                "__SYZABI_BEGIN_STDERR__",
                "__SYZABI_END_STDERR__",
                "__SYZABI_BEGIN_EVENTS__",
                '{"errno":0,"return_value":3}',
                "__SYZABI_END_EVENTS__",
                "__SYZABI_BEGIN_EXTERNAL_STATE__",
                '{"files":[]}',
                "__SYZABI_END_EXTERNAL_STATE__",
                "__SYZABI_END_BATCH_CASE__",
            ]
        )
        results = parse_batch_case_results(
            console,
            [
                {"program_id": "alpha", "run_id": "run-alpha"},
                {"program_id": "beta", "run_id": "run-beta"},
            ],
            kernel_build="asterinas@deadbeef",
            missing_status="timeout",
            missing_detail="candidate batch timed out before case completed",
        )
        self.assertEqual(results[0]["program_id"], "alpha")
        self.assertEqual(results[0]["status"], "ok")
        self.assertEqual(results[0]["stdout"], "hello")
        self.assertEqual(results[1]["program_id"], "beta")
        self.assertEqual(results[1]["status"], "timeout")
        self.assertEqual(results[1]["status_detail"], "candidate batch timed out before case completed")

    def test_write_bug_likely_reports_materializes_index_and_testcase_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            normalized = root / "corpus" / "normalized" / "bug.syz"
            normalized.parent.mkdir(parents=True, exist_ok=True)
            normalized.write_text("lseek(0, 0, 0)\n", encoding="utf-8")
            meta = root / "corpus" / "meta" / "bug.json"
            meta.parent.mkdir(parents=True, exist_ok=True)
            dump_json(meta, {"full_syscall_list": ["eventfd2", "lseek"]})

            ref_trace = root / "runs" / "ref" / "raw-trace.json"
            cand_trace = root / "runs" / "cand" / "raw-trace.json"
            ref_trace.parent.mkdir(parents=True, exist_ok=True)
            cand_trace.parent.mkdir(parents=True, exist_ok=True)
            dump_json(ref_trace.with_name("canonical-trace.json"), {
                "program_id": "bug",
                "side": "reference",
                "event_count": 1,
                "events": [{
                    "index": 0,
                    "syscall_name": "lseek",
                    "syscall_number": 8,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "outputs": [],
                }],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            })
            dump_json(cand_trace.with_name("canonical-trace.json"), {
                "program_id": "bug",
                "side": "candidate",
                "event_count": 1,
                "events": [{
                    "index": 0,
                    "syscall_name": "lseek",
                    "syscall_number": 8,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": -1,
                    "errno": 29,
                    "outputs": [],
                }],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            })

            cfg = {
                "workflow": "asterinas_scml",
                "paths": {"reports_dir": str(reports_dir)},
                "classification": {"bug_likely": "BUG_LIKELY"},
            }
            result = {
                "program_id": "bug",
                "classification": "BUG_LIKELY",
                "normalized_path": str(normalized),
                "meta_path": str(meta),
                "comparison": {
                    "first_divergence_index": 0,
                    "final_state_equal": True,
                    "process_exit_equal": True,
                },
                "reference_runs": [{
                    "console_log_path": str(root / "runs" / "ref" / "console.log"),
                    "trace_json_path": str(ref_trace),
                }],
                "candidate_run": {
                    "console_log_path": str(root / "runs" / "cand" / "console.log"),
                    "trace_json_path": str(cand_trace),
                },
                "scml_result_bucket": "passed_scml_and_diverged",
            }

            scheduler.write_bug_likely_reports([result], cfg)

            summary = json.loads((reports_dir / "bug_likely" / "summary.json").read_text())
            self.assertEqual(summary["bug_likely_count"], 1)
            self.assertEqual(summary["first_divergence_syscall_counts"], {"lseek": 1})
            copied = reports_dir / "bug_likely" / "testcases" / "bug.syz"
            self.assertEqual(copied.read_text(encoding="utf-8"), "lseek(0, 0, 0)\n")
            case_summary = json.loads((reports_dir / "bug_likely" / "cases" / "bug" / "summary.json").read_text())
            self.assertEqual(case_summary["first_divergence_syscall_name"], "lseek")
            self.assertEqual(case_summary["full_syscall_list"], ["eventfd2", "lseek"])

    def test_write_failure_reports_only_collects_non_no_diff_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            ref_trace = root / "runs" / "ref" / "raw-trace.json"
            cand_trace = root / "runs" / "cand" / "raw-trace.json"
            ref_trace.parent.mkdir(parents=True, exist_ok=True)
            cand_trace.parent.mkdir(parents=True, exist_ok=True)
            dump_json(ref_trace.with_name("canonical-trace.json"), {
                "program_id": "bug",
                "side": "reference",
                "event_count": 1,
                "events": [{
                    "index": 0,
                    "syscall_name": "lseek",
                    "syscall_number": 8,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "outputs": [],
                }],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            })
            dump_json(cand_trace.with_name("canonical-trace.json"), {
                "program_id": "bug",
                "side": "candidate",
                "event_count": 1,
                "events": [{
                    "index": 0,
                    "syscall_name": "lseek",
                    "syscall_number": 8,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": -1,
                    "errno": 29,
                    "outputs": [],
                }],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            })

            no_diff = {
                "program_id": "ok",
                "classification": "NO_DIFF",
                "reference_runs": [{"status": "ok"}],
                "candidate_run": {"status": "ok"},
            }
            bug = {
                "program_id": "bug",
                "classification": "BUG_LIKELY",
                "normalized_path": str(root / "corpus" / "normalized" / "bug.syz"),
                "meta_path": str(root / "corpus" / "meta" / "bug.json"),
                "comparison": {
                    "first_divergence_index": 0,
                    "final_state_equal": True,
                    "process_exit_equal": True,
                },
                "reference_runs": [{
                    "status": "ok",
                    "console_log_path": str(root / "runs" / "ref" / "console.log"),
                    "trace_json_path": str(ref_trace),
                }],
                "candidate_run": {
                    "status": "timeout",
                    "console_log_path": str(root / "runs" / "cand" / "console.log"),
                    "trace_json_path": str(cand_trace),
                },
            }

            previous_workflow = os.environ.get("SYZABI_WORKFLOW")
            previous_config = os.environ.get("SYZABI_CONFIG_PATH")
            try:
                config_path = root / "rules.json"
                config_path.write_text(json.dumps({
                    "workflow": "reporting",
                    "paths": {"reports_dir": str(reports_dir)},
                    "classification": {"no_diff": "NO_DIFF"},
                }), encoding="utf-8")
                configure_runtime(workflow="reporting", config_path=str(config_path))
                scheduler.write_failure_reports([no_diff, bug], "smoke")
            finally:
                if previous_workflow is None:
                    os.environ.pop("SYZABI_WORKFLOW", None)
                else:
                    os.environ["SYZABI_WORKFLOW"] = previous_workflow
                if previous_config is None:
                    os.environ.pop("SYZABI_CONFIG_PATH", None)
                else:
                    os.environ["SYZABI_CONFIG_PATH"] = previous_config

            failure_report = json.loads((reports_dir / "failure-report.json").read_text(encoding="utf-8"))
            self.assertEqual(failure_report["failed_results"], 1)
            self.assertEqual(failure_report["classification_counts"], {"BUG_LIKELY": 1})
            case = failure_report["failures_by_classification"]["BUG_LIKELY"][0]
            self.assertEqual(case["program_id"], "bug")
            self.assertEqual(case["candidate_status"], "timeout")
            self.assertEqual(case["first_divergence_syscall_name"], "lseek")
            failure_md = (reports_dir / "failure-report.md").read_text(encoding="utf-8")
            self.assertIn("BUG_LIKELY", failure_md)
            self.assertNotIn("ok:", failure_md)

    def test_scheduler_main_writes_summary_signoff_and_failure_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            reports_dir = root / "reports"
            eligible_file = root / "eligible.jsonl"
            config_path = root / "reporting_rules.json"

            eligible_file.write_text('{"program_id":"bug"}\n', encoding="utf-8")
            (reports_dir / "build-summary.json").parent.mkdir(parents=True, exist_ok=True)
            (reports_dir / "build-summary.json").write_text(json.dumps({"success": 1, "total": 1}), encoding="utf-8")

            ref_trace = root / "runs" / "ref" / "raw-trace.json"
            cand_trace = root / "runs" / "cand" / "raw-trace.json"
            dump_json(ref_trace.with_name("canonical-trace.json"), {
                "program_id": "bug",
                "side": "reference",
                "event_count": 1,
                "events": [{
                    "index": 0,
                    "syscall_name": "close",
                    "syscall_number": 3,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "outputs": [],
                }],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            })
            dump_json(cand_trace.with_name("canonical-trace.json"), {
                "program_id": "bug",
                "side": "candidate",
                "event_count": 1,
                "events": [{
                    "index": 0,
                    "syscall_name": "close",
                    "syscall_number": 3,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": -1,
                    "errno": 5,
                    "outputs": [],
                }],
                "final_state": {"files": []},
                "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            })

            config_path.write_text(json.dumps({
                "workflow": "reporting",
                "paths": {
                    "reports_dir": str(reports_dir),
                    "eligible_file": str(eligible_file),
                },
                "classification": {
                    "no_diff": "NO_DIFF",
                    "baseline_invalid": "BASELINE_INVALID",
                    "weak_spec_or_env_noise": "WEAK_SPEC_OR_ENV_NOISE",
                    "unsupported_feature": "UNSUPPORTED_FEATURE",
                    "bug_likely": "BUG_LIKELY",
                },
                "thresholds": {
                    "smoke": {
                        "build_success_rate": 0.0,
                        "dual_execution_completion_rate": 0.0,
                        "trace_success_rate": 0.0,
                        "canonical_success_rate": 0.0,
                        "baseline_invalid_rate": 1.0,
                        "total_min": 0,
                        "eligible_program_count_min": 0,
                    },
                    "signoff": {
                        "build_success_rate": 0.0,
                        "dual_execution_completion_rate": 0.0,
                        "trace_success_rate": 0.0,
                        "canonical_success_rate": 0.0,
                        "baseline_invalid_rate": 1.0,
                        "total_min": 0,
                        "eligible_program_count_min": 0,
                    },
                },
            }), encoding="utf-8")

            result = {
                "program_id": "bug",
                "classification": "BUG_LIKELY",
                "normalized_path": str(root / "bug.syz"),
                "meta_path": str(root / "bug.json"),
                "comparison": {
                    "first_divergence_index": 0,
                    "final_state_equal": True,
                    "process_exit_equal": True,
                },
                "reference_runs": [{
                    "status": "ok",
                    "trace_json_path": str(ref_trace),
                    "console_log_path": str(root / "runs" / "ref" / "console.log"),
                }],
                "candidate_run": {
                    "status": "ok",
                    "runner_kind": "qemu",
                    "kernel_build": "kernel-a",
                    "trace_json_path": str(cand_trace),
                    "console_log_path": str(root / "runs" / "cand" / "console.log"),
                },
                "candidate_runs": [{
                    "status": "ok",
                    "trace_json_path": str(cand_trace),
                    "console_log_path": str(root / "runs" / "cand" / "console.log"),
                }],
            }

            args = SimpleNamespace(
                workflow="reporting",
                campaign="smoke",
                eligible_file=str(eligible_file),
                limit=None,
                jobs=None,
                candidate_batch_size=None,
                program_id=None,
                controlled_divergence=False,
            )

            previous_workflow = os.environ.get("SYZABI_WORKFLOW")
            previous_config = os.environ.get("SYZABI_CONFIG_PATH")
            try:
                with patch("orchestrator.scheduler.parse_args", return_value=args), patch(
                    "orchestrator.scheduler.selected_entries", return_value=[{"program_id": "bug"}]
                ), patch("orchestrator.scheduler.schedule_entries", return_value=[result]):
                    os.environ["SYZABI_CONFIG_PATH"] = str(config_path)
                    scheduler.main()
            finally:
                if previous_workflow is None:
                    os.environ.pop("SYZABI_WORKFLOW", None)
                else:
                    os.environ["SYZABI_WORKFLOW"] = previous_workflow
                if previous_config is None:
                    os.environ.pop("SYZABI_CONFIG_PATH", None)
                else:
                    os.environ["SYZABI_CONFIG_PATH"] = previous_config

            self.assertTrue((reports_dir / "campaign-results.jsonl").exists())
            self.assertTrue((reports_dir / "summary.json").exists())
            self.assertTrue((reports_dir / "summary.md").exists())
            self.assertTrue((reports_dir / "signoff.md").exists())
            self.assertTrue((reports_dir / "failure-report.json").exists())
            self.assertTrue((reports_dir / "failure-report.md").exists())
            summary = json.loads((reports_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["campaign"], "smoke")
            failure_report = json.loads((reports_dir / "failure-report.json").read_text(encoding="utf-8"))
            self.assertEqual(failure_report["failed_results"], 1)


if __name__ == "__main__":
    unittest.main()
