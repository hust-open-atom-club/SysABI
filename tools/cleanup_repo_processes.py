#!/usr/bin/env python3
from __future__ import annotations

import argparse
import errno
import json
import os
import shlex
import signal
import shutil
import subprocess
from pathlib import Path


PROCESS_PATTERNS = (
    "python3 orchestrator/scheduler.py",
    "tools/run_asterinas.py",
    "qemu-system-x86_64",
    "cargo +nightly-2025-12-06 osdk",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--remove", action="append", default=[])
    return parser.parse_args()


def has_path_boundary_before(text: str, index: int) -> bool:
    if index <= 0:
        return True
    return text[index - 1] in {" ", "\t", "\n", "\r", "'", '"', "=", ":", "(", "["}


def has_path_boundary_after(text: str, index: int) -> bool:
    if index >= len(text):
        return True
    return text[index] in {" ", "\t", "\n", "\r", "/", ":", "'", '"', ")", "]"}


def cmdline_mentions_repo_path(cmdline: str, repo_root: Path) -> bool:
    needle = str(repo_root.resolve())
    start = 0
    while True:
        index = cmdline.find(needle, start)
        if index < 0:
            return False
        end = index + len(needle)
        if has_path_boundary_before(cmdline, index) and has_path_boundary_after(cmdline, end):
            return True
        start = index + 1


def process_owned_by_repo(cmdline: str, cwd: Path | None, repo_root: Path) -> bool:
    repo_root = repo_root.resolve()
    if cmdline_mentions_repo_path(cmdline, repo_root):
        return True
    if cwd is None:
        return False
    try:
        cwd.resolve().relative_to(repo_root)
    except ValueError:
        return False
    return True


def candidate_pids(pattern: str) -> list[int]:
    completed = subprocess.run(
        ["pgrep", "-f", pattern],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in {0, 1}:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"pgrep failed for {pattern!r}")
    return [int(line) for line in completed.stdout.splitlines() if line.strip()]


def read_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8").replace("\0", " ").strip()
    except OSError:
        return ""


def read_cwd(pid: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def terminate_repo_processes(repo_root: Path) -> None:
    current_pid = os.getpid()
    for pattern in PROCESS_PATTERNS:
        for pid in candidate_pids(pattern):
            if pid == current_pid:
                continue
            cmdline = read_cmdline(pid)
            cwd = read_cwd(pid)
            if not process_owned_by_repo(cmdline, cwd, repo_root):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue


def running_container_names() -> list[str]:
    try:
        completed = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def inspect_container(name: str) -> dict[str, object] | None:
    completed = subprocess.run(
        ["docker", "inspect", name],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    payload = json.loads(completed.stdout)
    if not payload:
        return None
    return dict(payload[0])


def container_owned_by_repo(container: dict[str, object], repo_root: Path) -> bool:
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        return False
    repo_root = repo_root.resolve()
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        source = mount.get("Source")
        if not isinstance(source, str) or not source:
            continue
        try:
            Path(source).resolve().relative_to(repo_root)
            return True
        except ValueError:
            continue
    return False


def terminate_repo_containers(repo_root: Path) -> None:
    for name in running_container_names():
        container = inspect_container(name)
        if container is None:
            continue
        if not container_owned_by_repo(container, repo_root):
            continue
        subprocess.run(
            ["docker", "rm", "-f", name],
            text=True,
            capture_output=True,
            check=False,
        )


def load_asterinas_docker_image(repo_root: Path) -> str | None:
    config_path = repo_root / "configs" / "asterinas_rules.json"
    if not config_path.exists():
        return None
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    asterinas = payload.get("asterinas", {})
    image = asterinas.get("docker_image")
    if not isinstance(image, str) or not image:
        return None
    return image


def remove_path(path: Path) -> bool:
    try:
        if not path.exists() and not path.is_symlink():
            return True
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        return False
    except OSError as exc:
        if exc.errno == errno.ENOTEMPTY:
            return False
        raise


def docker_remove_paths(repo_root: Path, paths: list[Path]) -> None:
    image = load_asterinas_docker_image(repo_root)
    if image is None:
        raise RuntimeError("permission-denied cleanup requires configs/asterinas_rules.json docker_image")
    relative_paths = [path.resolve().relative_to(repo_root.resolve()) for path in paths]
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{repo_root}:/workspace",
        image,
        "sh",
        "-lc",
        "rm -rf " + " ".join(shlex.quote(str(Path('/workspace') / relative_path)) for relative_path in relative_paths),
    ]
    completed = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker cleanup failed")


def cleanup_paths(repo_root: Path, targets: list[str]) -> None:
    if not targets:
        return
    repo_root = repo_root.resolve()
    pending_docker_cleanup: list[Path] = []
    for target in targets:
        path = (repo_root / target).resolve()
        try:
            path.relative_to(repo_root)
        except ValueError as exc:
            raise RuntimeError(f"cleanup target escapes repo root: {target}") from exc
        if not remove_path(path):
            pending_docker_cleanup.append(path)
    if pending_docker_cleanup:
        docker_remove_paths(repo_root, pending_docker_cleanup)


def main() -> None:
    args = parse_args()
    repo_root = Path(args.repo_root)
    terminate_repo_processes(repo_root)
    terminate_repo_containers(repo_root)
    cleanup_paths(repo_root, list(args.remove))


if __name__ == "__main__":
    main()
