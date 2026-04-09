from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.cleanup_repo_processes import cleanup_paths, container_owned_by_repo, process_owned_by_repo, running_container_names


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
