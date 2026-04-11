from __future__ import annotations

import fcntl
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from orchestrator.common import resolve_repo_path
from targets.asterinas.common import RunnerError
from targets.asterinas import paths as path_mod


class BuildConfigError(RuntimeError):
    pass


GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SYNC_CACHE_TTL_SEC = 600


def build_info_path(cfg: dict[str, Any]) -> Path:
    return resolve_repo_path(cfg["asterinas"]["build_info_path"])


def asterinas_repo_dir(cfg: dict[str, Any]) -> Path:
    return resolve_repo_path(cfg["asterinas"]["repo_dir"])


def configured_asterinas_ref(cfg: dict[str, Any]) -> str:
    return str(cfg["asterinas"]["revision"])


def repo_sync_lock_path(cfg: dict[str, Any]) -> Path:
    return build_info_path(cfg).with_name("repo-sync.lock")


def repo_sync_state_path(cfg: dict[str, Any]) -> Path:
    return build_info_path(cfg).with_name("repo-sync.json")


def current_asterinas_revision(cfg: dict[str, Any]) -> str:
    repo = asterinas_repo_dir(cfg)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "failed to read Asterinas revision"
        raise BuildConfigError(detail)
    return result.stdout.strip()


def current_asterinas_branch(cfg: dict[str, Any]) -> str:
    repo = asterinas_repo_dir(cfg)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "failed to read Asterinas branch"
        raise BuildConfigError(detail)
    return result.stdout.strip()


def load_repo_sync_state(cfg: dict[str, Any]) -> dict[str, object] | None:
    path = repo_sync_state_path(cfg)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def write_repo_sync_state(cfg: dict[str, Any], *, branch: str, revision: str) -> None:
    path = repo_sync_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "branch": branch,
                "revision": revision,
                "synced_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def can_skip_repo_sync(cfg: dict[str, Any], *, branch: str, revision: str) -> bool:
    state = load_repo_sync_state(cfg)
    if state is None:
        return False
    if state.get("branch") != branch:
        return False
    if state.get("revision") != revision:
        return False
    synced_at = state.get("synced_at")
    if not isinstance(synced_at, int):
        return False
    return int(time.time()) - synced_at < SYNC_CACHE_TTL_SEC


def sync_asterinas_branch(cfg: dict[str, Any], branch: str) -> str:
    repo = asterinas_repo_dir(cfg)
    lock_path = repo_sync_lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        revision = current_asterinas_revision(cfg)
        current_branch = current_asterinas_branch(cfg)
        if current_branch == branch and can_skip_repo_sync(cfg, branch=branch, revision=revision):
            return revision

        fetch = subprocess.run(
            ["git", "-C", str(repo), "fetch", "origin", branch],
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        if fetch.returncode != 0:
            detail = fetch.stderr.strip() or fetch.stdout.strip() or f"failed to fetch origin/{branch}"
            raise BuildConfigError(detail)

        checkout = subprocess.run(
            ["git", "-C", str(repo), "checkout", "-f", "-B", branch, f"origin/{branch}"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if checkout.returncode != 0:
            detail = checkout.stderr.strip() or checkout.stdout.strip() or f"failed to checkout {branch}"
            raise BuildConfigError(detail)
        reset = subprocess.run(
            ["git", "-C", str(repo), "reset", "--hard", f"origin/{branch}"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if reset.returncode != 0:
            detail = reset.stderr.strip() or reset.stdout.strip() or f"failed to reset {branch}"
            raise BuildConfigError(detail)
        revision = current_asterinas_revision(cfg)
        write_repo_sync_state(cfg, branch=branch, revision=revision)
        return revision


def ensure_revision(cfg: dict[str, Any]) -> str:
    expected = configured_asterinas_ref(cfg)
    if GIT_COMMIT_RE.fullmatch(expected):
        revision = current_asterinas_revision(cfg)
        if revision != expected:
            raise BuildConfigError(f"Asterinas revision mismatch: expected {expected}, got {revision}")
        return revision
    return sync_asterinas_branch(cfg, expected)


def ensure_host_build(cfg: dict[str, object], *, hooks) -> str:
    revision = ensure_revision(cfg)
    info_path = build_info_path(cfg)
    cargo_target_dir = path_mod.host_target_root()
    target_dir = cargo_target_dir / "osdk"
    lock_path = hooks.build_lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        hooks.ensure_host_osdk(cfg)
        probe_root = hooks.build_probe_root()
        env = hooks.host_osdk_env(probe_root, boot_method="grub-rescue-iso")
        env["CARGO_TARGET_DIR"] = str(cargo_target_dir)
        hooks.ensure_dummy_block_images(cfg)
        initramfs_path = hooks.build_probe_initramfs(cfg)

        if info_path.exists():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if (
                info.get("revision") == revision
                and info.get("mode") == "host-direct"
                and info.get("boot_method") == "qemu-direct"
                and info.get("target_dir") == str(target_dir)
                and hooks.kernel_build_ready(cfg)
            ):
                return revision

        repo = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "kernel"
        build = subprocess.run(
            hooks.osdk_qemu_direct_build_command(initramfs_path),
            cwd=repo,
            env=env,
            timeout=1800,
            text=True,
            capture_output=True,
            check=False,
        )
        if build.returncode != 0:
            raise RunnerError(build.stderr.strip() or build.stdout.strip() or "Asterinas host build failed")
        hooks.dump_json(
            info_path,
            {
                "revision": revision,
                "mode": "host-direct",
                "boot_method": "qemu-direct",
                "target_dir": str(target_dir),
                "vdso_library_dir": env["VDSO_LIBRARY_DIR"],
                "cargo_osdk_version": hooks.cargo_osdk_version(),
            },
        )
    return revision


def ensure_docker_build(cfg: dict[str, object], *, hooks) -> str:
    revision = ensure_revision(cfg)
    info_path = build_info_path(cfg)
    lock_path = hooks.build_lock_path(cfg)
    build_log_dir = info_path.parent / "build"
    build_log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = build_log_dir / "make-kernel.stdout.txt"
    stderr_path = build_log_dir / "make-kernel.stderr.txt"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if info_path.exists():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if (
                info.get("revision") == revision
                and info.get("mode") == "docker-qemu"
                and info.get("docker_image") == cfg["asterinas"]["docker_image"]
                and hooks.shared_cargo_osdk_path().exists()
                and hooks.kernel_build_ready(cfg)
            ):
                return revision

        hooks.prime_docker_cargo_cache(cfg)
        hooks.ensure_docker_cargo_osdk(cfg)
        build = subprocess.run(
            hooks.docker_make_kernel_command(cfg),
            timeout=int(cfg["asterinas"]["build_timeout_sec"]),
            text=True,
            capture_output=True,
            check=False,
        )
        stdout_path.write_text(build.stdout, encoding="utf-8")
        stderr_path.write_text(build.stderr, encoding="utf-8")
        if build.returncode != 0:
            raise RunnerError(build.stderr.strip() or build.stdout.strip() or "Asterinas Docker build failed")
        hooks.dump_json(
            info_path,
            {
                "revision": revision,
                "mode": "docker-qemu",
                "docker_image": cfg["asterinas"]["docker_image"],
                "docker_repo_dir": str(hooks.docker_repo_dir(cfg)),
                "docker_workspace_dir": str(hooks.docker_workspace_dir(cfg)),
                "target_dir": str(resolve_repo_path("third_party/asterinas/target/osdk")),
                "build_command": "make kernel",
                "build_stdout_path": str(stdout_path),
                "build_stderr_path": str(stderr_path),
            },
        )
    return revision
