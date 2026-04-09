from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.cleanup_repo_processes import (
    canonical_cleanup_targets,
    cleanup_paths,
    container_owned_by_repo,
    default_cleanup_targets,
    load_asterinas_docker_image,
    process_owned_by_repo,
    running_container_names,
)


class CleanupRepoProcessesTests(unittest.TestCase):
    def test_process_owned_by_repo_matches_cmdline_reference(self) -> None:
        repo_root = Path("/workspace/FuzzAsterinas")
        self.assertTrue(
            process_owned_by_repo(
                "qemu-system-x86_64 -kernel /workspace/FuzzAsterinas/artifacts/bzImage",
                None,
                repo_root,
            )
        )

    def test_process_owned_by_repo_rejects_unrelated_process(self) -> None:
        repo_root = Path("/workspace/FuzzAsterinas")
        self.assertFalse(
            process_owned_by_repo(
                "qemu-system-x86_64 -kernel /tmp/other/bzImage",
                Path("/tmp/other"),
                repo_root,
            )
        )

    def test_process_owned_by_repo_rejects_prefix_only_match(self) -> None:
        repo_root = Path("/workspace/FuzzAsterinas")
        self.assertFalse(
            process_owned_by_repo(
                "python3 /workspace/FuzzAsterinas-old/orchestrator/scheduler.py",
                None,
                repo_root,
            )
        )

    def test_cleanup_paths_falls_back_to_docker_for_permission_denied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            target = repo_root / "artifacts" / "sandboxes" / "asterinas"
            target.mkdir(parents=True, exist_ok=True)
            with patch("tools.cleanup_repo_processes.remove_path", side_effect=[False]), patch(
                "tools.cleanup_repo_processes.docker_remove_paths"
            ) as docker_remove_paths:
                cleanup_paths(repo_root, ["artifacts/sandboxes/asterinas"])
            docker_remove_paths.assert_called_once()

    def test_cleanup_paths_rejects_targets_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            with self.assertRaises(RuntimeError):
                cleanup_paths(repo_root, ["../outside"])

    def test_container_owned_by_repo_matches_mount_under_repo(self) -> None:
        repo_root = Path("/workspace/FuzzAsterinas")
        container = {
            "Mounts": [
                {"Source": "/workspace/FuzzAsterinas"},
            ]
        }
        self.assertTrue(container_owned_by_repo(container, repo_root))

    def test_container_owned_by_repo_rejects_other_checkout(self) -> None:
        repo_root = Path("/workspace/FuzzAsterinas")
        container = {
            "Mounts": [
                {"Source": "/workspace/FuzzAsterinas-old"},
            ]
        }
        self.assertFalse(container_owned_by_repo(container, repo_root))

    def test_running_container_names_returns_empty_when_docker_unavailable(self) -> None:
        with patch(
            "tools.cleanup_repo_processes.subprocess.run",
            side_effect=OSError("docker missing"),
        ):
            self.assertEqual(running_container_names(), [])

    def test_load_asterinas_docker_image_reads_canonical_target_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            workflow_dir = repo_root / "configs" / "workflows"
            target_dir = repo_root / "configs" / "targets" / "asterinas"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            (workflow_dir / "asterinas.json").write_text(
                '{"target":"asterinas","target_config_path":"configs/targets/asterinas/target.json"}\n',
                encoding="utf-8",
            )
            (target_dir / "target.json").write_text(
                '{"docker_image":"asterinas/asterinas:test"}\n',
                encoding="utf-8",
            )
            self.assertEqual(load_asterinas_docker_image(repo_root), "asterinas/asterinas:test")

    def test_load_asterinas_docker_image_falls_back_to_legacy_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config_dir = repo_root / "configs"
            config_dir.mkdir(parents=True, exist_ok=True)
            (config_dir / "asterinas_rules.json").write_text(
                '{"asterinas":{"docker_image":"legacy/image:test"}}\n',
                encoding="utf-8",
            )
            self.assertEqual(load_asterinas_docker_image(repo_root), "legacy/image:test")

    def test_default_cleanup_targets_merge_canonical_and_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            workflow_dir = repo_root / "configs" / "workflows"
            target_dir = repo_root / "configs" / "targets" / "asterinas"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            target_dir.mkdir(parents=True, exist_ok=True)
            (workflow_dir / "baseline.json").write_text(
                '{"paths":{"build_dir":"build/targets/linux/baseline/testcases","artifacts_dir":"artifacts/runs/targets/linux/baseline","reports_dir":"reports/targets/linux/baseline","eligible_file":"eligible_programs/targets/linux/baseline/default.jsonl"}}\n',
                encoding="utf-8",
            )
            (workflow_dir / "asterinas.json").write_text(
                '{"target":"asterinas","paths":{"candidate_initramfs_packages_dir":"artifacts/targets/asterinas/initramfs-packages"},"target_config_path":"configs/targets/asterinas/target.json"}\n',
                encoding="utf-8",
            )
            (target_dir / "target.json").write_text(
                '{"build_info_path":"artifacts/targets/asterinas/build-info.json"}\n',
                encoding="utf-8",
            )

            canonical = canonical_cleanup_targets(repo_root)
            merged = default_cleanup_targets(repo_root)

        self.assertIn("build/targets/linux/baseline/testcases", canonical)
        self.assertIn("artifacts/targets/asterinas/initramfs-packages", canonical)
        self.assertIn("artifacts/targets/asterinas/build", canonical)
        self.assertIn("artifacts/targets/asterinas/docker-cargo-home", canonical)
        self.assertIn("artifacts/targets/asterinas/build-probe", canonical)
        self.assertIn("reports/asterinas", merged)
        self.assertIn("build/targets/linux/baseline/testcases", merged)
