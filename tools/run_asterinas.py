#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.schemas import validate_raw_trace
from orchestrator.common import config, configure_runtime, dump_json, repo_root, resolve_repo_path, sha256_text, temp_dir as runtime_temp_dir
from tools.run_asterinas_shared import (
    candidate_status_from_events,
    compose_batch_autorun,
    compose_packaged_autorun,
    extract_batch_case_blocks,
    extract_section,
    materialize_batch_case_outputs,
    parse_batch_case_results,
    parse_events,
    parse_external_state,
    parse_process_exit,
    shared_package_bundle_dir,
    shared_package_runtime_dirs,
)


MARKER_PREFIX = "__SYZABI"
NETWORK_PORT_ENV_NAMES = (
    "SSH_PORT",
    "NGINX_PORT",
    "REDIS_PORT",
    "IPERF_PORT",
    "LMBENCH_TCP_LAT_PORT",
    "LMBENCH_TCP_BW_PORT",
    "MEMCACHED_PORT",
)
GUEST_ENV_HEADER_MAGIC = "SYZABI_ENV_V1"
GUEST_ENV_HEADER_SIZE = 1024

ASTERINAS_GIT_MIRRORS = {
    "inherit-methods-macro": "https://github.com/asterinas/inherit-methods-macro",
    "inventory": "https://github.com/asterinas/inventory",
    "rust-ctor": "https://github.com/asterinas/rust-ctor",
    "smoltcp": "https://github.com/asterinas/smoltcp",
}


class RunnerError(RuntimeError):
    pass


def is_docker_access_error(detail: str) -> bool:
    normalized = detail.lower()
    return (
        "permission denied while trying to connect to the docker daemon socket" in normalized
        or "dial unix /var/run/docker.sock" in normalized
        or "cannot connect to the docker daemon" in normalized
    )


def should_fallback_to_host_direct(exc: RunnerError) -> bool:
    return is_docker_access_error(str(exc))


def guest_crash_detail(console_text: str) -> str | None:
    if "Printing stack trace:" in console_text:
        return "guest crashed before emitting autorun markers (kernel stack trace observed)"
    lowered = console_text.lower()
    if "panicked at" in lowered or "kernel panic" in lowered:
        return "guest crashed before emitting autorun markers (kernel panic observed)"
    return None


def write_missing_marker_crash_result(
    *,
    console_text: str,
    raw_trace_path: Path,
    external_state_path: Path,
    kernel_build: str,
) -> bool:
    detail = guest_crash_detail(console_text)
    if detail is None:
        return False
    if not external_state_path.exists():
        dump_json(external_state_path, {"files": []})
    raw_trace = {
        "program_id": os.environ.get("SYZABI_PROGRAM_ID", "unknown"),
        "side": os.environ.get("SYZABI_SIDE", "candidate"),
        "run_id": os.environ.get("SYZABI_RUN_ID", "unknown"),
        "status": "crash",
        "events": [],
        "process_exit": {"status": "crash", "exit_code": None, "timed_out": False},
    }
    validate_raw_trace(raw_trace)
    dump_json(raw_trace_path, raw_trace)
    write_runner_result(
        {
            "status": "crash",
            "exit_code": None,
            "status_detail": detail,
            "kernel_build": kernel_build,
        }
    )
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--batch-manifest")
    parser.add_argument("--work-dir")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["unconfigured", "local-proxy", "host-direct", "docker-qemu"],
        default=os.environ.get("SYZABI_ASTERINAS_MODE", "docker-qemu"),
    )
    return parser.parse_args()


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def required_env_path(name: str) -> Path:
    path = env_path(name)
    if path is None:
        raise RunnerError(f"missing required environment path: {name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def runner_result_path() -> Path | None:
    return env_path("SYZABI_RUNNER_RESULT_PATH")


def write_runner_result(payload: dict[str, object]) -> None:
    path = runner_result_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)


def selected_run_timeout_sec(cfg: dict[str, object]) -> int:
    override = os.environ.get("SYZABI_BATCH_TIMEOUT_SEC")
    if override:
        return int(override)
    return int(cfg["asterinas"]["run_timeout_sec"])


def read_workflow_config() -> dict[str, object]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "asterinas")
    configure_runtime(workflow=workflow)
    cfg = config()
    if "asterinas" not in cfg:
        raise RunnerError(f"run_asterinas.py requires an asterinas-capable config, got {cfg.get('workflow')}")
    return cfg


def docker_repo_dir(cfg: dict[str, object]) -> Path:
    return Path(str(cfg["asterinas"].get("docker_repo_dir", "/root/asterinas")))


def docker_workspace_dir(cfg: dict[str, object]) -> Path:
    return Path(str(cfg["asterinas"].get("docker_workspace_dir", "/workspace")))


def asterinas_git_mirror_root() -> Path:
    return resolve_repo_path("artifacts/asterinas/git-mirrors")


def ensure_git_mirror(name: str, remote_url: str) -> Path:
    mirror_root = asterinas_git_mirror_root()
    mirror_root.mkdir(parents=True, exist_ok=True)
    mirror_path = mirror_root / f"{name}.git"
    if mirror_path.exists():
        update = subprocess.run(
            ["git", "--git-dir", str(mirror_path), "remote", "update", "--prune"],
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        if update.returncode != 0:
            detail = update.stderr.strip() or update.stdout.strip() or f"failed to update git mirror {name}"
            if (mirror_path / "HEAD").exists():
                sys.stderr.write(f"warning: using stale git mirror {name}: {detail}\n")
                return mirror_path
            raise RunnerError(detail)
        return mirror_path
    clone = subprocess.run(
        ["git", "clone", "--mirror", remote_url, str(mirror_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if clone.returncode != 0:
        raise RunnerError(clone.stderr.strip() or clone.stdout.strip() or f"failed to clone git mirror {name}")
    return mirror_path


def ensure_asterinas_git_mirrors() -> dict[str, Path]:
    return {
        name: ensure_git_mirror(name, remote_url)
        for name, remote_url in ASTERINAS_GIT_MIRRORS.items()
    }


def existing_asterinas_git_mirrors() -> dict[str, Path]:
    mirror_root = asterinas_git_mirror_root()
    mirrors: dict[str, Path] = {}
    for name in ASTERINAS_GIT_MIRRORS:
        mirror_path = mirror_root / f"{name}.git"
        if (mirror_path / "HEAD").exists():
            mirrors[name] = mirror_path
    return mirrors


def docker_cargo_home() -> Path:
    return resolve_repo_path("artifacts/asterinas/docker-cargo-home")


def shared_cargo_osdk_path() -> Path:
    return docker_cargo_home() / "bin" / "cargo-osdk"


def container_cargo_home(cfg: dict[str, object]) -> Path:
    return host_path_to_container_path(docker_cargo_home(), cfg)


def ensure_docker_cargo_cache_dirs() -> tuple[Path, Path]:
    cargo_root = docker_cargo_home()
    git_dir = cargo_root / "git"
    registry_dir = cargo_root / "registry"
    (cargo_root / "bin").mkdir(parents=True, exist_ok=True)
    git_dir.mkdir(parents=True, exist_ok=True)
    registry_dir.mkdir(parents=True, exist_ok=True)
    package_cache = cargo_root / ".package-cache"
    package_cache.touch(exist_ok=True)
    return git_dir, registry_dir


def prepare_run_cargo_home(work_dir: Path) -> Path:
    shared_home = docker_cargo_home()
    ensure_docker_cargo_cache_dirs()
    run_home = work_dir / "docker-cargo-home"
    run_home.mkdir(parents=True, exist_ok=True)
    for metadata_name in (
        ".crates.toml",
        ".crates2.json",
        ".global-cache",
        ".package-cache",
        ".package-cache-mutate",
        "config.toml",
        "credentials.toml",
    ):
        source = shared_home / metadata_name
        destination = run_home / metadata_name
        if not source.exists():
            continue
        if destination.exists() or destination.is_symlink():
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.copy2(source, destination)
    registry_target = run_home / "registry"
    if registry_target.is_symlink() or registry_target.exists():
        if registry_target.is_dir() and not registry_target.is_symlink():
            shutil.rmtree(registry_target)
        else:
            registry_target.unlink()
    registry_target.symlink_to(os.path.relpath(shared_home / "registry", run_home), target_is_directory=True)

    git_target = run_home / "git"
    if git_target.exists():
        shutil.rmtree(git_target)
    shutil.copytree(shared_home / "git", git_target)
    package_cache = run_home / ".package-cache"
    package_cache.touch(exist_ok=True)
    return run_home


def ensure_shared_package_cargo_home(package_dir: Path, *, refresh: bool = False) -> Path:
    run_home = package_dir / "shared-cargo-home"
    if run_home.exists() and not refresh:
        return run_home
    lock_path = package_dir / ".cargo-home.lock"
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if run_home.exists() and not refresh:
            return run_home
        if run_home.exists():
            shutil.rmtree(run_home)
        shared_home = docker_cargo_home()
        ensure_docker_cargo_cache_dirs()
        run_home.mkdir(parents=True, exist_ok=True)
        for metadata_name in (
            ".crates.toml",
            ".crates2.json",
            ".global-cache",
            ".package-cache",
            ".package-cache-mutate",
            "config.toml",
            "credentials.toml",
        ):
            source = shared_home / metadata_name
            destination = run_home / metadata_name
            if not source.exists():
                continue
            shutil.copy2(source, destination)
        registry_target = run_home / "registry"
        registry_target.symlink_to(os.path.relpath(shared_home / "registry", run_home), target_is_directory=True)
        git_target = run_home / "git"
        shutil.copytree(shared_home / "git", git_target)
        (run_home / ".package-cache").touch(exist_ok=True)
    return run_home


def prime_docker_cargo_cache(cfg: dict[str, object]) -> None:
    cargo_home = docker_cargo_home()
    cargo_home.mkdir(parents=True, exist_ok=True)
    ensure_docker_cargo_cache_dirs()
    manifest_path = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "Cargo.toml"
    gitconfig_path = prepare_host_gitconfig(cfg)
    fetch = subprocess.run(
        ["cargo", "fetch", "--locked", "--manifest-path", str(manifest_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=int(cfg["asterinas"]["build_timeout_sec"]),
        env={
            **os.environ,
            "CARGO_HOME": str(cargo_home),
            "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
            "CARGO_TERM_PROGRESS_WHEN": "never",
            "GIT_CONFIG_GLOBAL": str(gitconfig_path),
            "TMPDIR": str(local_tmp_dir()),
        },
    )
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "failed to prefetch Asterinas cargo dependencies"
        raise RunnerError(detail)


def gitconfig_lines(
    cfg: dict[str, object],
    *,
    path_transform,
    ensure_mirrors: bool,
) -> list[str]:
    mirrors = ensure_asterinas_git_mirrors() if ensure_mirrors else existing_asterinas_git_mirrors()
    lines: list[str] = []
    for name, remote_url in ASTERINAS_GIT_MIRRORS.items():
        mirror = mirrors.get(name)
        if mirror is None:
            continue
        mirror_path = path_transform(mirror, cfg)
        lines.extend(
            [
                "[safe]",
                f"\tdirectory = {mirror_path}",
                "",
            ]
        )
        for source_url in (remote_url, f"{remote_url}.git"):
            lines.extend(
                [
                    f'[url "file://{mirror_path}"]',
                    f"\tinsteadOf = {source_url}",
                    "",
                ]
            )
    return lines


def prepare_host_gitconfig(cfg: dict[str, object]) -> Path:
    config_path = resolve_repo_path("artifacts/asterinas/host-gitconfig")
    config_path.write_text(
        "\n".join(gitconfig_lines(cfg, path_transform=lambda path, _: path, ensure_mirrors=True)),
        encoding="utf-8",
    )
    return config_path


def prepare_docker_gitconfig(cfg: dict[str, object]) -> Path:
    config_path = resolve_repo_path("artifacts/asterinas/docker-gitconfig")
    # Per-run Docker execution must stay self-contained and reuse only mirrors
    # that were already primed by an explicit cache/bootstrap flow.
    config_path.write_text(
        "\n".join(gitconfig_lines(cfg, path_transform=host_path_to_container_path, ensure_mirrors=False)),
        encoding="utf-8",
    )
    return config_path


def host_path_to_container_path(path: Path, cfg: dict[str, object]) -> Path:
    resolved = path.resolve()
    workspace_root = repo_root().resolve()
    try:
        relative = resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise RunnerError(f"path is outside workspace and cannot be mounted into Docker: {resolved}") from exc
    return docker_workspace_dir(cfg) / relative


def docker_env_options(extra_env: dict[str, str] | None = None) -> list[str]:
    options: list[str] = []
    if not extra_env:
        return options
    for key in sorted(extra_env):
        options.extend(["-e", f"{key}={extra_env[key]}"])
    return options


def docker_run_command(
    cfg: dict[str, object],
    script: str,
    *,
    extra_env: dict[str, str] | None = None,
    workdir: Path | None = None,
    container_name: str | None = None,
) -> list[str]:
    workspace_root = repo_root().resolve()
    asterinas_repo = resolve_repo_path(cfg["asterinas"]["repo_dir"]).resolve()
    ensure_docker_cargo_cache_dirs()
    gitconfig_path = prepare_docker_gitconfig(cfg)
    shared_cargo_home = container_cargo_home(cfg)
    prefixed_script = "\n".join(
        [
            f"export PATH={shlex.quote(str(shared_cargo_home / 'bin'))}:$PATH",
            script,
        ]
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--network=host",
        "-v",
        "/dev:/dev",
        "-v",
        f"{asterinas_repo}:{docker_repo_dir(cfg)}",
        "-v",
        f"{workspace_root}:{docker_workspace_dir(cfg)}",
    ]
    if workdir is not None:
        command.extend(["-w", str(workdir)])
    if container_name:
        command.extend(["--name", container_name])
    merged_env = {
        "CARGO_HOME": str(shared_cargo_home),
        "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
        "CARGO_TERM_PROGRESS_WHEN": "never",
        "GIT_CONFIG_GLOBAL": str(host_path_to_container_path(gitconfig_path, cfg)),
    }
    if extra_env:
        merged_env.update(extra_env)
    command.extend(docker_env_options(merged_env))
    command.extend(
        [
            str(cfg["asterinas"]["docker_image"]),
            "bash",
            "-lc",
            prefixed_script,
        ]
    )
    return command


def docker_make_kernel_command(cfg: dict[str, object]) -> list[str]:
    shared_cargo_home = container_cargo_home(cfg)
    return docker_run_command(
        cfg,
        f"set -euo pipefail; make CARGO_OSDK={shlex.quote(str(shared_cargo_home / 'bin' / 'cargo-osdk'))} kernel",
        workdir=docker_repo_dir(cfg),
    )


def sanitize_container_component(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-._")
    if not sanitized:
        return "run"
    return sanitized[:48]


def container_name_for_run(program_id: str, run_id: str) -> str:
    return f"syzabi-{sanitize_container_component(program_id)}-{sanitize_container_component(run_id)}"


def force_remove_container(container_name: str) -> None:
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        text=True,
        capture_output=True,
        check=False,
    )


def local_tmp_dir() -> Path:
    try:
        return runtime_temp_dir(config())
    except Exception:
        return runtime_temp_dir()


def ensure_vdso_dir() -> Path:
    destination = resolve_repo_path("artifacts/asterinas/linux_vdso")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "vdso_x86_64.so"
    if target.exists():
        return destination
    candidates = sorted(Path("/usr/lib/modules").glob("*/vdso/vdso64.so"))
    if not candidates:
        raise RunnerError("failed to find host vdso64.so")
    shutil.copy2(candidates[0], target)
    return destination


def ensure_dummy_block_images(cfg: dict[str, object]) -> None:
    build_dir = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "test/initramfs/build"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ext2.img", "exfat.img"):
        path = build_dir / name
        if path.exists():
            continue
        with path.open("wb") as handle:
            handle.truncate(1024 * 1024)


def asterinas_rust_toolchain() -> str | None:
    toolchain_path = resolve_repo_path("third_party/asterinas/rust-toolchain.toml")
    if not toolchain_path.exists():
        return None
    payload = tomllib.loads(toolchain_path.read_text(encoding="utf-8"))
    toolchain = payload.get("toolchain", {})
    if not isinstance(toolchain, dict):
        return None
    channel = toolchain.get("channel")
    return str(channel) if channel else None


def cargo_osdk_version() -> str:
    result = subprocess.run(
        [*cargo_osdk_base_command(), "--version"],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "TMPDIR": str(local_tmp_dir())},
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ensure_host_osdk(cfg: dict[str, object]) -> None:
    expected = "cargo-osdk 0.17.1"
    if cargo_osdk_version().startswith(expected):
        return
    repo = resolve_repo_path(cfg["asterinas"]["repo_dir"])
    env = {**os.environ, "TMPDIR": str(local_tmp_dir()), "OSDK_LOCAL_DEV": "1"}
    install = subprocess.run(
        ["cargo", "install", "cargo-osdk", "--path", "osdk", "--force"],
        cwd=repo,
        env=env,
        timeout=1800,
        text=True,
        capture_output=True,
        check=False,
    )
    if install.returncode != 0:
        raise RunnerError(install.stderr.strip() or install.stdout.strip() or "failed to install cargo-osdk 0.17.1")


def ensure_local_mtools() -> Path | None:
    existing = shutil.which("mformat")
    if existing:
        return None

    tool_root = resolve_repo_path("artifacts/asterinas/host-tools/mtools")
    binary = tool_root / "usr/bin/mformat"
    if binary.exists():
        return tool_root / "usr/bin"

    downloads = resolve_repo_path("artifacts/asterinas/host-tools/downloads")
    downloads.mkdir(parents=True, exist_ok=True)
    download = subprocess.run(
        ["apt", "download", "mtools"],
        cwd=downloads,
        env={**os.environ, "TMPDIR": str(local_tmp_dir())},
        text=True,
        capture_output=True,
        check=False,
    )
    if download.returncode != 0:
        raise RunnerError(download.stderr.strip() or download.stdout.strip() or "failed to download mtools")

    packages = sorted(downloads.glob("mtools_*_amd64.deb"))
    if not packages:
        raise RunnerError("failed to locate downloaded mtools package")

    tool_root.mkdir(parents=True, exist_ok=True)
    extract = subprocess.run(
        ["dpkg-deb", "-x", str(packages[-1]), str(tool_root)],
        env={**os.environ, "TMPDIR": str(local_tmp_dir())},
        text=True,
        capture_output=True,
        check=False,
    )
    if extract.returncode != 0:
        raise RunnerError(extract.stderr.strip() or extract.stdout.strip() or "failed to extract mtools package")
    return tool_root / "usr/bin"


def cargo_osdk_base_command() -> list[str]:
    command = ["cargo"]
    toolchain = asterinas_rust_toolchain()
    if toolchain:
        command.append(f"+{toolchain}")
    command.append("osdk")
    return command


def repack_initramfs(source_dir: Path, output_path: Path) -> None:
    command = f"find . -print0 | cpio --null -o --format=newc --quiet | gzip -9 > {output_path}"
    result = subprocess.run(
        command,
        shell=True,
        cwd=source_dir,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "TMPDIR": str(local_tmp_dir())},
    )
    if result.returncode != 0:
        raise RunnerError(result.stderr.strip() or "failed to repack initramfs")


def compose_profile() -> str:
    return """#!/bin/sh
if [ -f /etc/profile.d/init.sh ]; then
    . /etc/profile.d/init.sh
fi
"""


def compose_init() -> str:
    return """#!/bin/sh
exec /syzkabi/autorun.sh
"""


def compose_init_hook() -> str:
    return """#!/bin/sh
/syzkabi/autorun.sh
"""


def compose_autorun(preview_bytes: int) -> str:
    injected_env = []
    for name in (
        "SYZABI_INJECT_TRACE_ENABLED",
        "SYZABI_INJECT_TRACE_CALL_INDEX",
        "SYZABI_INJECT_TRACE_SYSCALL",
        "SYZABI_INJECT_TRACE_FIELD",
        "SYZABI_INJECT_TRACE_VALUE",
    ):
        value = os.environ.get(name)
        if value is None or value == "":
            continue
        injected_env.append(f"export {name}={shlex.quote(value)}")
    injected_block = "\n".join(injected_env)
    if injected_block:
        injected_block += "\n"
    return f"""#!/bin/sh
set +e

BUSYBOX=/usr/bin/busybox
RUNTIME_DIR=/tmp/syzabi
WORK_DIR="$RUNTIME_DIR/work"

$BUSYBOX mkdir -p /proc /sys "$RUNTIME_DIR" "$WORK_DIR"
$BUSYBOX mount -t proc proc /proc >/dev/null 2>&1 || true
$BUSYBOX mount -t sysfs sysfs /sys >/dev/null 2>&1 || true

export SYZABI_SIDE=candidate
export SYZABI_TRACE_EVENTS_PATH="$RUNTIME_DIR/raw-trace.events.jsonl"
export SYZABI_TRACE_PREVIEW_BYTES="{preview_bytes}"
{injected_block}

emit_file_section() {{
    name="$1"
    path="$2"
    echo "{MARKER_PREFIX}_BEGIN_${{name}}__"
    if [ -f "$path" ]; then
        $BUSYBOX cat "$path"
    fi
    echo
    echo "{MARKER_PREFIX}_END_${{name}}__"
}}

emit_external_state() {{
    echo "{MARKER_PREFIX}_BEGIN_EXTERNAL_STATE__"
    printf '{{"files":['
    sep=""
    $BUSYBOX find "$WORK_DIR" -type f | $BUSYBOX sort > "$RUNTIME_DIR/filelist.txt"
    while IFS= read -r path; do
        rel="${{path#$WORK_DIR/}}"
        size="$($BUSYBOX stat -c %s "$path" 2>/dev/null || echo 0)"
        sha="$($BUSYBOX sha256sum "$path" 2>/dev/null | $BUSYBOX awk '{{print $1}}')"
        printf '%s{{"path":"%s","size":%s,"sha256":"%s"}}' "$sep" "$rel" "$size" "$sha"
        sep=","
    done < "$RUNTIME_DIR/filelist.txt"
    printf ']}}'
    echo
    echo "{MARKER_PREFIX}_END_EXTERNAL_STATE__"
}}

$BUSYBOX chmod +x /syzkabi/testcase.bin 2>/dev/null || true
cd "$WORK_DIR" || exit 125
/syzkabi/testcase.bin > "$RUNTIME_DIR/stdout.txt" 2> "$RUNTIME_DIR/stderr.txt"
EXIT_CODE=$?
PROC_STATUS=ok
if [ "$EXIT_CODE" -ge 128 ]; then
    PROC_STATUS=crash
fi

echo "{MARKER_PREFIX}_BEGIN_PROCESS_EXIT__"
printf '{{"status":"%s","exit_code":%s,"timed_out":false}}\\n' "$PROC_STATUS" "$EXIT_CODE"
echo "{MARKER_PREFIX}_END_PROCESS_EXIT__"
emit_file_section STDOUT "$RUNTIME_DIR/stdout.txt"
emit_file_section STDERR "$RUNTIME_DIR/stderr.txt"
emit_file_section EVENTS "$RUNTIME_DIR/raw-trace.events.jsonl"
emit_external_state
$BUSYBOX sync
$BUSYBOX poweroff -f >/dev/null 2>&1 || $BUSYBOX halt -f >/dev/null 2>&1 || $BUSYBOX reboot -f >/dev/null 2>&1 || echo o > /proc/sysrq-trigger
"""


def create_minimal_initramfs(cfg: dict[str, object], binary_path: Path, work_dir: Path) -> Path:
    root = work_dir / "asterinas-minimal-initramfs"
    if root.exists():
        shutil.rmtree(root)
    for directory in (
        root / "bin",
        root / "dev",
        root / "etc/profile.d",
        root / "proc",
        root / "root",
        root / "sbin",
        root / "sys",
        root / "tmp",
        root / "usr/bin",
        root / "syzkabi",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2("/usr/bin/busybox", root / "usr/bin/busybox")
    os.symlink("/usr/bin/busybox", root / "bin/sh")
    init_path = root / "bin/init"
    init_path.write_text(compose_init(), encoding="utf-8")
    init_path.chmod(0o755)
    os.symlink("/bin/init", root / "sbin/init")
    shutil.copy2(binary_path, root / "syzkabi/testcase.bin")
    (root / "syzkabi/testcase.bin").chmod(0o755)
    (root / "etc/profile").write_text(compose_profile(), encoding="utf-8")
    (root / "etc/profile.d/init.sh").write_text(compose_init_hook(), encoding="utf-8")
    autorun_path = root / "syzkabi/autorun.sh"
    autorun_path.write_text(compose_autorun(int(cfg["normalization"]["preview_bytes"])), encoding="utf-8")
    autorun_path.chmod(0o755)

    output_path = work_dir / "asterinas-initramfs.cpio.gz"
    repack_initramfs(root, output_path)
    return output_path


def load_initramfs_package_manifest(package_dir: Path) -> dict[str, object]:
    manifest_path = package_dir / "package-manifest.json"
    if not manifest_path.exists():
        raise RunnerError(f"missing initramfs package manifest: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RunnerError(f"invalid initramfs package manifest: {manifest_path}")
    return payload


def create_packaged_initramfs(cfg: dict[str, object], package_dir: Path, payload: dict[str, object]) -> Path:
    root = package_dir / "initramfs-root"
    if root.exists():
        shutil.rmtree(root)
    for directory in (
        root / "bin",
        root / "dev",
        root / "etc/profile.d",
        root / "proc",
        root / "root",
        root / "sbin",
        root / "sys",
        root / "tmp",
        root / "usr/bin",
        root / "syzkabi/batch",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2("/usr/bin/busybox", root / "usr/bin/busybox")
    os.symlink("/usr/bin/busybox", root / "bin/sh")
    init_path = root / "bin/init"
    init_path.write_text(compose_init(), encoding="utf-8")
    init_path.chmod(0o755)
    os.symlink("/bin/init", root / "sbin/init")
    for case in payload["cases"]:
        source = Path(str(case["binary_path"]))
        if not source.exists():
            raise RunnerError(f"missing packaged testcase binary: {source}")
        destination = root / "syzkabi" / "batch" / f"{int(case['slot'])}.bin"
        shutil.copy2(source, destination)
        destination.chmod(0o755)
    (root / "etc/profile").write_text(compose_profile(), encoding="utf-8")
    (root / "etc/profile.d/init.sh").write_text(compose_init_hook(), encoding="utf-8")
    autorun_path = root / "syzkabi/autorun.sh"
    autorun_path.write_text(compose_packaged_autorun(int(payload["preview_bytes"])), encoding="utf-8")
    autorun_path.chmod(0o755)

    output_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
    repack_initramfs(root, output_path)
    return output_path


def ensure_packaged_initramfs(cfg: dict[str, object], package_dir: Path) -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    output_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
    if output_path.exists():
        return output_path
    lock_path = package_dir / ".build.lock"
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if output_path.exists():
            return output_path
        payload = load_initramfs_package_manifest(package_dir)
        return create_packaged_initramfs(cfg, package_dir, payload)


def selected_guest_cmdline_append() -> str:
    parts: list[str] = []
    extra = os.environ.get("SYZABI_GUEST_KCMD_ARGS")
    if extra:
        parts.append(extra)
    return " ".join(part for part in parts if part)


def guest_env_lines() -> list[str]:
    lines: list[str] = []
    if os.environ.get("SYZABI_ASTERINAS_PACKAGE_DIR") and not os.environ.get("SYZABI_ASTERINAS_PACKAGE_SLOT"):
        raise RunnerError("missing SYZABI_ASTERINAS_PACKAGE_SLOT for packaged candidate run")
    slot = os.environ.get("SYZABI_ASTERINAS_PACKAGE_SLOT")
    if slot:
        lines.append(f"SYZABI_PACKAGE_SLOT={shlex.quote(slot)}")
    inject_enabled = os.environ.get("SYZABI_INJECT_TRACE_ENABLED")
    if inject_enabled:
        lines.append(f"SYZABI_INJECT_TRACE_ENABLED={shlex.quote(inject_enabled)}")
    inject_call_index = os.environ.get("SYZABI_INJECT_TRACE_CALL_INDEX")
    if inject_call_index:
        lines.append(f"SYZABI_INJECT_TRACE_CALL_INDEX={shlex.quote(inject_call_index)}")
    inject_syscall = os.environ.get("SYZABI_INJECT_TRACE_SYSCALL")
    if inject_syscall:
        lines.append(f"SYZABI_INJECT_TRACE_SYSCALL={shlex.quote(inject_syscall)}")
    inject_field = os.environ.get("SYZABI_INJECT_TRACE_FIELD")
    if inject_field:
        lines.append(f"SYZABI_INJECT_TRACE_FIELD={shlex.quote(inject_field)}")
    inject_value = os.environ.get("SYZABI_INJECT_TRACE_VALUE")
    if inject_value:
        lines.append(f"SYZABI_INJECT_TRACE_VALUE={shlex.quote(inject_value)}")
    return lines


def guest_env_header_bytes(lines: list[str]) -> bytes:
    payload = "\n".join([GUEST_ENV_HEADER_MAGIC, *lines, "__END__", ""]).encode("utf-8")
    if len(payload) > GUEST_ENV_HEADER_SIZE:
        raise RunnerError("guest env selector header exceeds reserved image space")
    return payload.ljust(GUEST_ENV_HEADER_SIZE, b" ")


def materialize_guest_env_file(ext2_image: Path) -> None:
    lines = guest_env_lines()
    if not lines:
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=local_tmp_dir()) as handle:
        handle.write("\n".join(lines) + "\n")
        temp_path = Path(handle.name)
    try:
        subprocess.run(
            ["debugfs", "-w", "-R", "rm /syzkabi.env", str(ext2_image)],
            text=True,
            capture_output=True,
            check=False,
        )
        write_result = subprocess.run(
            ["debugfs", "-w", "-R", f"write {temp_path} /syzkabi.env", str(ext2_image)],
            text=True,
            capture_output=True,
            check=False,
        )
        if write_result.returncode != 0:
            raise RunnerError(write_result.stderr.strip() or write_result.stdout.strip() or "failed to materialize guest env file")
        with ext2_image.open("r+b") as image_handle:
            image_handle.seek(0)
            image_handle.write(guest_env_header_bytes(lines))
    finally:
        temp_path.unlink(missing_ok=True)


def docker_osdk_build_script(cfg: dict[str, object], work_dir: Path, initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> str:
    container_work_dir = host_path_to_container_path(work_dir, cfg)
    container_initramfs = host_path_to_container_path(initramfs_path, cfg)
    container_osdk_output = host_path_to_container_path(work_dir / "osdk-output", cfg)
    return "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(str(container_work_dir))} {shlex.quote(str(container_osdk_output))}",
            f"cd {shlex.quote(str(docker_repo_dir(cfg) / 'kernel'))}",
            " ".join(shlex.quote(part) for part in osdk_build_command(container_initramfs, kcmd_args=kcmd_args)),
        ]
    )


def packaged_bundle_metadata_path(package_dir: Path) -> Path:
    return package_dir / ".osdk-build.meta.json"


def packaged_bundle_metadata(cfg: dict[str, object], initramfs_path: Path, *, kcmd_args: str) -> dict[str, object]:
    return {
        "docker_image": str(cfg["asterinas"]["docker_image"]),
        "initramfs_sha256": hashlib.sha256(initramfs_path.read_bytes()).hexdigest(),
        "kcmd_args": kcmd_args,
        "revision": ensure_revision(cfg),
    }


def packaged_bundle_metadata_matches(metadata_path: Path, expected: dict[str, object]) -> bool:
    if not metadata_path.exists():
        return False
    try:
        actual = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return actual == expected


def ensure_packaged_docker_bundle(cfg: dict[str, object], package_dir: Path, initramfs_path: Path, *, kcmd_args: str) -> None:
    cargo_target_dir, osdk_output_dir = shared_package_runtime_dirs(package_dir)
    build_root = package_dir / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    lock_path = package_dir / ".osdk-build.lock"
    bundle_dir = shared_package_bundle_dir(package_dir)
    ready_stamp = package_dir / ".osdk-build.ready"
    metadata_path = packaged_bundle_metadata_path(package_dir)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        expected_metadata = packaged_bundle_metadata(cfg, initramfs_path, kcmd_args=kcmd_args)
        if (
            ready_stamp.exists()
            and (bundle_dir / "bundle.toml").exists()
            and packaged_bundle_metadata_matches(metadata_path, expected_metadata)
        ):
            return
        ready_stamp.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        # Prime the shared cargo cache before snapshotting it into a package-local home.
        # The packaged build itself runs offline to avoid nondeterministic network stalls.
        prime_docker_cargo_cache(cfg)
        run_cargo_home = ensure_shared_package_cargo_home(package_dir, refresh=True)
        build_env = {
            "CARGO_HOME": str(host_path_to_container_path(run_cargo_home, cfg)),
            "CARGO_TARGET_DIR": str(host_path_to_container_path(cargo_target_dir, cfg)),
            "OSDK_OUTPUT_DIR": str(host_path_to_container_path(osdk_output_dir, cfg)),
            "CARGO_NET_OFFLINE": "true",
        }
        container_name = container_name_for_run("bundle", sha256_text(str(package_dir))[:12])
        force_remove_container(container_name)
        build_command = docker_run_command(
            cfg,
            docker_osdk_build_script(cfg, package_dir, initramfs_path, kcmd_args=kcmd_args),
            extra_env=build_env,
            workdir=docker_repo_dir(cfg),
            container_name=container_name,
        )
        try:
            completed = subprocess.run(
                build_command,
                cwd=build_root,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            force_remove_container(container_name)
        (build_root / "build.stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (build_root / "build.stderr.txt").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or "failed to prebuild packaged docker bundle")
        dump_json(metadata_path, expected_metadata)
        ready_stamp.write_text("ready\n", encoding="utf-8")


def containerized_grub_iso_command(cfg: dict[str, object], package_dir: Path, work_dir: Path) -> list[str]:
    host_env = host_osdk_env(work_dir, boot_method="grub-rescue-iso")
    host_env["OVMF"] = "on"
    host_env["OVMF_CODE_FILE"] = str(system_ovmf_code_path())
    host_env["OVMF_VARS_FILE"] = str(prepare_ovmf_vars(work_dir))
    host_env["EXT2_IMAGE"] = str(work_dir / "ext2.img")
    host_env["EXFAT_IMAGE"] = str(work_dir / "exfat.img")
    qemu_command, _ = grub_iso_qemu_command(cfg, shared_package_bundle_dir(package_dir), host_env)
    workspace_root = repo_root().resolve()
    replaced: list[str] = []
    for token in qemu_command:
        token = str(token).replace(str(workspace_root), str(docker_workspace_dir(cfg)))
        token = token.replace(str(system_ovmf_code_path()), container_ovmf_code_path())
        token = token.replace(str((work_dir / "OVMF_VARS.fd").resolve()), str(host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)))
        replaced.append(token)
    if kvm_enabled(host_env) and "-accel" not in replaced and "-enable-kvm" not in replaced:
        replaced.extend(["-accel", "kvm"])
    return replaced


def host_grub_bundle_command(cfg: dict[str, object], package_dir: Path, work_dir: Path) -> tuple[list[str], Path]:
    host_env = host_osdk_env(work_dir, boot_method="grub-rescue-iso")
    host_env["OVMF"] = "on"
    host_env["OVMF_CODE_FILE"] = str(system_ovmf_code_path())
    host_env["OVMF_VARS_FILE"] = str(prepare_ovmf_vars(work_dir))
    host_env["EXT2_IMAGE"] = str(work_dir / "ext2.img")
    host_env["EXFAT_IMAGE"] = str(work_dir / "exfat.img")
    return grub_iso_qemu_command(cfg, shared_package_bundle_dir(package_dir), host_env)


def docker_grub_bundle_script(cfg: dict[str, object], package_dir: Path, work_dir: Path) -> str:
    container_cmd = containerized_grub_iso_command(cfg, package_dir, work_dir)
    return "\n".join(
        [
            "set -euo pipefail",
            "exec " + " ".join(shlex.quote(part) for part in container_cmd),
        ]
    )


def selected_initramfs(cfg: dict[str, object], binary_path: Path, work_dir: Path) -> Path:
    package_dir = env_path("SYZABI_ASTERINAS_PACKAGE_DIR")
    if package_dir is not None:
        return ensure_packaged_initramfs(cfg, package_dir.resolve())
    return create_minimal_initramfs(cfg, binary_path, work_dir)


def create_batch_initramfs(cfg: dict[str, object], cases: list[dict[str, object]], work_dir: Path) -> Path:
    root = work_dir / "asterinas-batch-initramfs"
    if root.exists():
        shutil.rmtree(root)
    for directory in (
        root / "bin",
        root / "dev",
        root / "etc/profile.d",
        root / "proc",
        root / "root",
        root / "sbin",
        root / "sys",
        root / "tmp",
        root / "usr/bin",
        root / "syzkabi/batch",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2("/usr/bin/busybox", root / "usr/bin/busybox")
    os.symlink("/usr/bin/busybox", root / "bin/sh")
    init_path = root / "bin/init"
    init_path.write_text(compose_init(), encoding="utf-8")
    init_path.chmod(0o755)
    os.symlink("/bin/init", root / "sbin/init")
    for index, case in enumerate(cases):
        destination = root / "syzkabi" / "batch" / f"{index}.bin"
        shutil.copy2(case["binary_path"], destination)
        destination.chmod(0o755)
    (root / "etc/profile").write_text(compose_profile(), encoding="utf-8")
    (root / "etc/profile.d/init.sh").write_text(compose_init_hook(), encoding="utf-8")
    autorun_path = root / "syzkabi/autorun.sh"
    autorun_path.write_text(compose_batch_autorun(int(cfg["normalization"]["preview_bytes"]), cases), encoding="utf-8")
    autorun_path.chmod(0o755)

    output_path = work_dir / "asterinas-batch-initramfs.cpio.gz"
    repack_initramfs(root, output_path)
    return output_path


def build_info_path(cfg: dict[str, object]) -> Path:
    return resolve_repo_path(cfg["asterinas"]["build_info_path"])


def target_osdk_dir(cfg: dict[str, object]) -> Path:
    info_path = build_info_path(cfg)
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        target_dir = info.get("target_dir")
        if target_dir:
            return Path(str(target_dir))
    return resolve_repo_path("third_party/asterinas/target/osdk")


def built_bundle_dir(cfg: dict[str, object]) -> Path:
    return target_osdk_dir(cfg) / "aster-kernel"


def build_probe_root() -> Path:
    root = resolve_repo_path("artifacts/asterinas/build-probe")
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_probe_initramfs(cfg: dict[str, object]) -> Path:
    probe_root = build_probe_root()
    shutil.copy2("/usr/bin/true", probe_root / "probe.bin")
    return create_minimal_initramfs(cfg, probe_root / "probe.bin", probe_root)


def load_bundle_manifest(cfg: dict[str, object]) -> dict[str, object]:
    manifest_path = built_bundle_dir(cfg) / "bundle.toml"
    if not manifest_path.exists():
        raise RunnerError(f"missing OSDK bundle manifest: {manifest_path}")
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def load_external_bundle_manifest(bundle_dir: Path) -> dict[str, object]:
    manifest_path = bundle_dir / "bundle.toml"
    if not manifest_path.exists():
        raise RunnerError(f"missing OSDK bundle manifest: {manifest_path}")
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def shared_bzimage_path(cfg: dict[str, object]) -> Path:
    manifest = load_bundle_manifest(cfg)
    aster_bin = manifest.get("aster_bin")
    if not isinstance(aster_bin, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing aster_bin section")
    kernel_relpath = aster_bin.get("path")
    if not isinstance(kernel_relpath, str):
        raise RunnerError("invalid OSDK bundle manifest: missing aster_bin.path")
    return built_bundle_dir(cfg) / kernel_relpath


def bundle_kcmdline(manifest: dict[str, object]) -> str:
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing run section")
    boot_section = run_section.get("boot")
    if not isinstance(boot_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing boot section")
    kcmdline = boot_section.get("kcmdline")
    if not isinstance(kcmdline, list):
        raise RunnerError("invalid OSDK bundle manifest: missing kcmdline")
    return " ".join(str(part) for part in kcmdline)


def bundle_qemu_path(manifest: dict[str, object]) -> str:
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing run section")
    qemu_section = run_section.get("qemu")
    if not isinstance(qemu_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing qemu section")
    path = qemu_section.get("path")
    if not isinstance(path, str) or not path:
        raise RunnerError("invalid OSDK bundle manifest: missing qemu path")
    return path


def bundle_grub_iso_path(bundle_dir: Path, manifest: dict[str, object]) -> Path:
    vm_image = manifest.get("vm_image")
    if not isinstance(vm_image, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing vm_image")
    image_relpath = vm_image.get("path")
    if not isinstance(image_relpath, str) or not image_relpath:
        raise RunnerError("invalid OSDK bundle manifest: missing vm_image.path")
    return bundle_dir / image_relpath


def kernel_build_ready(cfg: dict[str, object]) -> bool:
    try:
        load_bundle_manifest(cfg)
    except RunnerError:
        return False
    image_path = shared_bzimage_path(cfg)
    return image_path.exists() and image_path.stat().st_size > 1024


def qemu_log_paths(work_dir: Path) -> tuple[Path, Path]:
    return work_dir / "qemu.log", work_dir / "qemu-serial.log"


def read_console_text(*paths: Path) -> str:
    chunks: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text or text in seen:
            continue
        seen.add(text)
        chunks.append(text)
    return "\n".join(chunks).strip()


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait(timeout=5)


def matching_qemu_pids(work_dir: Path) -> list[int]:
    marker = str((work_dir / "OVMF_VARS.fd").resolve())
    result = subprocess.run(
        ["ps", "-eo", "pid=,args="],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    matches: list[int] = []
    for line in result.stdout.splitlines():
        if marker not in line or "qemu-system-" not in line:
            continue
        pid_text, _, _ = line.strip().partition(" ")
        try:
            matches.append(int(pid_text))
        except ValueError:
            continue
    return matches


def stop_qemu_processes(work_dir: Path) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = matching_qemu_pids(work_dir)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                continue
        time.sleep(1)


def current_asterinas_revision(cfg: dict[str, object]) -> str:
    repo = resolve_repo_path(cfg["asterinas"]["repo_dir"])
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RunnerError(f"failed to read Asterinas revision: {result.stderr.strip()}")
    return result.stdout.strip()


def ensure_revision(cfg: dict[str, object]) -> str:
    revision = current_asterinas_revision(cfg)
    expected = cfg["asterinas"]["revision"]
    if revision != expected:
        raise RunnerError(f"Asterinas revision mismatch: expected {expected}, got {revision}")
    return revision


def host_osdk_env(work_dir: Path, *, boot_method: str = "qemu-direct") -> dict[str, str]:
    env = os.environ.copy()
    env["TMPDIR"] = str(local_tmp_dir())
    env["VDSO_LIBRARY_DIR"] = str(ensure_vdso_dir())
    env["CONSOLE"] = "hvc0"
    env["BOOT_METHOD"] = boot_method
    env["OVMF"] = "off"
    env["NETDEV"] = env.get("SYZABI_ASTERINAS_NETDEV", "none")
    env["QEMU_DISPLAY"] = "none"
    env["SMP"] = env.get("SYZABI_ASTERINAS_SMP", "1")
    env["MEM"] = env.get("SYZABI_ASTERINAS_MEM", "2G")
    qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
    env["QEMU_LOG_FILE"] = str(qemu_log_path)
    env["QEMU_SERIAL_LOG_FILE"] = str(qemu_serial_log_path)
    toolchain = asterinas_rust_toolchain()
    if toolchain:
        env["RUSTUP_TOOLCHAIN"] = toolchain
    mtools_bin = ensure_local_mtools()
    if mtools_bin is not None:
        env["PATH"] = f"{mtools_bin}:{env['PATH']}"
    return env


def reserve_tcp_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, int(sock.getsockname()[1])


def reserve_qemu_ports() -> tuple[list[socket.socket], dict[str, int]]:
    sockets: list[socket.socket] = []
    ports: dict[str, int] = {}
    for name in NETWORK_PORT_ENV_NAMES:
        sock, port = reserve_tcp_port()
        sockets.append(sock)
        ports[name] = port
    return sockets, ports


def release_reserved_ports(sockets: list[socket.socket]) -> None:
    for sock in sockets:
        try:
            sock.close()
        except OSError:
            continue


def system_ovmf_code_path() -> Path:
    for candidate in (Path("/usr/share/OVMF/OVMF_CODE_4M.fd"), Path("/usr/share/ovmf/OVMF.fd")):
        if candidate.exists():
            return candidate
    raise RunnerError("failed to locate system OVMF code image")


def prepare_ovmf_vars(work_dir: Path) -> Path:
    for candidate in (Path("/usr/share/OVMF/OVMF_VARS_4M.fd"), Path("/usr/share/OVMF/OVMF_VARS.fd")):
        if not candidate.exists():
            continue
        target = work_dir / "OVMF_VARS.fd"
        if not target.exists():
            shutil.copy2(candidate, target)
        return target
    raise RunnerError("failed to locate system OVMF vars image")


def prepare_run_block_images(cfg: dict[str, object], work_dir: Path) -> tuple[Path, Path]:
    source_dir = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "test/initramfs/build"
    ext2_image = work_dir / "ext2.img"
    exfat_image = work_dir / "exfat.img"
    shutil.copy2(source_dir / "ext2.img", ext2_image)
    shutil.copy2(source_dir / "exfat.img", exfat_image)
    materialize_guest_env_file(ext2_image)
    return ext2_image, exfat_image


def osdk_build_command(initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> list[str]:
    return [
        *cargo_osdk_base_command(),
        "build",
        "--target-arch=x86_64",
        "--boot-method=grub-rescue-iso",
        "--grub-boot-protocol=linux",
        "--linux-x86-legacy-boot",
        f"--kcmd-args={kcmd_args}",
        "--initramfs",
        str(initramfs_path),
    ]


def osdk_qemu_direct_build_command(initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> list[str]:
    return [
        *cargo_osdk_base_command(),
        "build",
        "--target-arch=x86_64",
        "--boot-method=qemu-direct",
        f"--kcmd-args={kcmd_args}",
        "--initramfs",
        str(initramfs_path),
    ]


def osdk_run_command(initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> list[str]:
    command = [
        *cargo_osdk_base_command(),
        "run",
        "--target-arch=x86_64",
        f"--kcmd-args={kcmd_args}",
        "--initramfs",
        str(initramfs_path),
    ]
    if os.environ.get("SYZABI_ASTERINAS_ENABLE_KVM", "1") != "0":
        command.append('--qemu-args=-accel kvm')
    return command


def build_lock_path(cfg: dict[str, object]) -> Path:
    info_path = build_info_path(cfg)
    return info_path.with_name(f"{info_path.name}.lock")


def ensure_host_build(cfg: dict[str, object]) -> str:
    revision = ensure_revision(cfg)
    info_path = build_info_path(cfg)
    cargo_target_dir = resolve_repo_path("artifacts/asterinas/host-target")
    target_dir = cargo_target_dir / "osdk"
    lock_path = build_lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        ensure_host_osdk(cfg)
        probe_root = build_probe_root()
        env = host_osdk_env(probe_root, boot_method="grub-rescue-iso")
        env["CARGO_TARGET_DIR"] = str(cargo_target_dir)
        ensure_dummy_block_images(cfg)
        initramfs_path = build_probe_initramfs(cfg)

        if info_path.exists():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if (
                info.get("revision") == revision
                and info.get("mode") == "host-direct"
                and info.get("boot_method") == "qemu-direct"
                and info.get("target_dir") == str(target_dir)
                and kernel_build_ready(cfg)
            ):
                return revision

        repo = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "kernel"
        build = subprocess.run(
            osdk_qemu_direct_build_command(initramfs_path),
            cwd=repo,
            env=env,
            timeout=1800,
            text=True,
            capture_output=True,
            check=False,
        )
        if build.returncode != 0:
            raise RunnerError(build.stderr.strip() or build.stdout.strip() or "Asterinas host build failed")
        dump_json(
            info_path,
            {
                "revision": revision,
                "mode": "host-direct",
                "boot_method": "qemu-direct",
                "target_dir": str(target_dir),
                "vdso_library_dir": env["VDSO_LIBRARY_DIR"],
                "cargo_osdk_version": cargo_osdk_version(),
            },
        )
    return revision


def ensure_docker_build(cfg: dict[str, object]) -> str:
    revision = ensure_revision(cfg)
    info_path = build_info_path(cfg)
    lock_path = build_lock_path(cfg)
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
                and shared_cargo_osdk_path().exists()
                and kernel_build_ready(cfg)
            ):
                return revision

        prime_docker_cargo_cache(cfg)
        build = subprocess.run(
            docker_make_kernel_command(cfg),
            timeout=int(cfg["asterinas"]["build_timeout_sec"]),
            text=True,
            capture_output=True,
            check=False,
        )
        stdout_path.write_text(build.stdout, encoding="utf-8")
        stderr_path.write_text(build.stderr, encoding="utf-8")
        if build.returncode != 0:
            raise RunnerError(build.stderr.strip() or build.stdout.strip() or "Asterinas Docker build failed")
        dump_json(
            info_path,
            {
                "revision": revision,
                "mode": "docker-qemu",
                "docker_image": cfg["asterinas"]["docker_image"],
                "docker_repo_dir": str(docker_repo_dir(cfg)),
                "docker_workspace_dir": str(docker_workspace_dir(cfg)),
                "target_dir": str(resolve_repo_path("third_party/asterinas/target/osdk")),
                "build_command": "make kernel",
                "build_stdout_path": str(stdout_path),
                "build_stderr_path": str(stderr_path),
            },
        )
    return revision


def qemu_args_tokens(cfg: dict[str, object], env: dict[str, str]) -> list[str]:
    repo = resolve_repo_path(cfg["asterinas"]["repo_dir"])
    result = subprocess.run(
        ["bash", "tools/qemu_args.sh", "normal"],
        cwd=repo,
        env=env,
        timeout=30,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RunnerError(result.stderr.strip() or result.stdout.strip() or "failed to render qemu args")
    return shlex.split(result.stdout.strip())


def kvm_accessible() -> bool:
    return Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK)


def kvm_enabled(env: dict[str, str]) -> bool:
    return env.get("SYZABI_ASTERINAS_ENABLE_KVM", "1") != "0"


def qemu_direct_command(cfg: dict[str, object], initramfs_path: Path, env: dict[str, str]) -> tuple[list[str], Path]:
    manifest = load_bundle_manifest(cfg)
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing run section")
    qemu_section = run_section.get("qemu")
    if not isinstance(qemu_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing qemu section")

    kcmdline = bundle_kcmdline(manifest)
    extra_kcmd_args = env.get("SYZABI_GUEST_KCMD_ARGS", "").strip()
    if extra_kcmd_args:
        kcmdline = f"{kcmdline} {extra_kcmd_args}".strip()
    command = [
        str(qemu_section["path"]),
        "-kernel",
        str(shared_bzimage_path(cfg)),
        "-initrd",
        str(initramfs_path),
        "-append",
        kcmdline,
    ]
    command.extend(qemu_args_tokens(cfg, env))
    if kvm_enabled(env) and kvm_accessible() and "-accel" not in command and "-enable-kvm" not in command:
        command.extend(["-accel", "kvm"])
    return command, resolve_repo_path(cfg["asterinas"]["repo_dir"])


def grub_iso_qemu_command(cfg: dict[str, object], bundle_dir: Path, env: dict[str, str]) -> tuple[list[str], Path]:
    manifest = load_external_bundle_manifest(bundle_dir)
    image_path = bundle_grub_iso_path(bundle_dir, manifest)
    command = [
        bundle_qemu_path(manifest),
        "-drive",
        f"file={image_path},format=raw,index=2,media=cdrom",
    ]
    command.extend(qemu_args_tokens(cfg, env))
    if kvm_enabled(env) and kvm_accessible() and "-accel" not in command and "-enable-kvm" not in command:
        command.extend(["-accel", "kvm"])
    return command, resolve_repo_path(cfg["asterinas"]["repo_dir"])


def container_ovmf_code_path() -> str:
    return "/root/ovmf/release/OVMF_CODE.fd"


def container_ovmf_vars_seed_path() -> str:
    return "/root/ovmf/release/OVMF_VARS.fd"


def docker_run_env(cfg: dict[str, object], work_dir: Path) -> dict[str, str]:
    qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
    osdk_output_dir = work_dir / "osdk-output"
    return {
        "BOOT_METHOD": "grub-rescue-iso",
        "CONSOLE": "hvc0",
        "EXT2_IMAGE": str(host_path_to_container_path(work_dir / "ext2.img", cfg)),
        "EXFAT_IMAGE": str(host_path_to_container_path(work_dir / "exfat.img", cfg)),
        "MEM": os.environ.get("SYZABI_ASTERINAS_MEM", "2G"),
        "NETDEV": os.environ.get("SYZABI_ASTERINAS_NETDEV", "none"),
        "OSDK_OUTPUT_DIR": str(host_path_to_container_path(osdk_output_dir, cfg)),
        "OVMF": "on",
        "OVMF_CODE_FILE": container_ovmf_code_path(),
        "OVMF_VARS_FILE": str(host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)),
        "QEMU_DISPLAY": "none",
        "QEMU_LOG_FILE": str(host_path_to_container_path(qemu_log_path, cfg)),
        "QEMU_SERIAL_LOG_FILE": str(host_path_to_container_path(qemu_serial_log_path, cfg)),
        "SMP": os.environ.get("SYZABI_ASTERINAS_SMP", "1"),
    }


def containerized_qemu_direct_command(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    guest_kcmd_args: str = "",
) -> list[str]:
    host_env = host_osdk_env(work_dir, boot_method="qemu-direct")
    host_env["OVMF"] = "off"
    host_env["EXT2_IMAGE"] = str(work_dir / "ext2.img")
    host_env["EXFAT_IMAGE"] = str(work_dir / "exfat.img")
    if guest_kcmd_args:
        host_env["SYZABI_GUEST_KCMD_ARGS"] = guest_kcmd_args
    qemu_command, _ = qemu_direct_command(cfg, initramfs_path, host_env)
    workspace_root = repo_root().resolve()
    qemu_tokens = [str(token) for token in qemu_command]
    replaced: list[str] = []
    for token in qemu_tokens:
        token = token.replace(str(workspace_root), str(docker_workspace_dir(cfg)))
        replaced.append(token)
    if kvm_enabled(host_env) and "-accel" not in replaced and "-enable-kvm" not in replaced:
        replaced.extend(["-accel", "kvm"])
    return replaced


def docker_qemu_direct_script(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    guest_kcmd_args: str = "",
) -> str:
    container_cmd = containerized_qemu_direct_command(cfg, work_dir, initramfs_path, guest_kcmd_args=guest_kcmd_args)
    return "\n".join(
        [
            "set -euo pipefail",
            "exec " + " ".join(shlex.quote(part) for part in container_cmd),
        ]
    )


def docker_osdk_run_script(cfg: dict[str, object], work_dir: Path, initramfs_path: Path, *, kcmd_args: str = "console=hvc0") -> str:
    container_work_dir = host_path_to_container_path(work_dir, cfg)
    container_initramfs = host_path_to_container_path(initramfs_path, cfg)
    container_ovmf_vars = host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)
    return "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(str(container_work_dir))} \"${{OSDK_OUTPUT_DIR:-{shlex.quote(str(host_path_to_container_path(work_dir / 'osdk-output', cfg)))}}}\"",
            f"if [ ! -f {shlex.quote(str(container_ovmf_vars))} ]; then cp {shlex.quote(container_ovmf_vars_seed_path())} {shlex.quote(str(container_ovmf_vars))}; fi",
            f"cd {shlex.quote(str(docker_repo_dir(cfg) / 'kernel'))}",
            " ".join(shlex.quote(part) for part in osdk_run_command(container_initramfs, kcmd_args=kcmd_args)),
        ]
    )


def load_batch_manifest(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RunnerError("invalid batch manifest: missing cases")
    return [dict(case) for case in cases]


def docker_qemu_batch_run(args: argparse.Namespace) -> None:
    raise RunnerError("batch manifest mode is disabled because candidate cases must run in isolated VMs")


def local_proxy(args: argparse.Namespace) -> None:
    if not args.binary or not args.work_dir:
        raise RunnerError("--binary and --work-dir are required in local-proxy mode")
    completed = subprocess.run(
        [args.binary],
        cwd=Path(args.work_dir),
        text=True,
        capture_output=True,
        check=False,
    )
    required_env_path("SYZABI_STDOUT_PATH").write_text(completed.stdout, encoding="utf-8")
    required_env_path("SYZABI_STDERR_PATH").write_text(completed.stderr, encoding="utf-8")
    status = "crash" if completed.returncode < 0 else "ok"
    write_runner_result({"status": status, "exit_code": completed.returncode, "kernel_build": "local-proxy"})


def host_direct_run(args: argparse.Namespace) -> None:
    if not args.binary or not args.work_dir:
        raise RunnerError("--binary and --work-dir are required in host-direct mode")

    cfg = read_workflow_config()
    binary_path = Path(args.binary).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = required_env_path("SYZABI_STDOUT_PATH")
    stderr_path = required_env_path("SYZABI_STDERR_PATH")
    console_log_path = required_env_path("SYZABI_CONSOLE_LOG_PATH")
    raw_trace_path = required_env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = required_env_path("SYZABI_EXTERNAL_STATE_PATH")
    result_path = runner_result_path()
    for stale_path in (stdout_path, stderr_path, console_log_path, raw_trace_path, external_state_path, result_path):
        stale_path.unlink(missing_ok=True)

    package_dir = env_path("SYZABI_ASTERINAS_PACKAGE_DIR")
    resolved_package_dir = package_dir.resolve() if package_dir is not None else None
    custom_initramfs = selected_initramfs(cfg, binary_path, work_dir)
    guest_kcmd_args = " ".join(part for part in ("console=hvc0", selected_guest_cmdline_append()) if part)
    packaged_bundle_ready = False
    if resolved_package_dir is not None and (shared_package_bundle_dir(resolved_package_dir) / "bundle.toml").exists():
        try:
            ensure_packaged_docker_bundle(
                cfg,
                resolved_package_dir,
                custom_initramfs,
                kcmd_args=guest_kcmd_args,
            )
            packaged_bundle_ready = True
        except RunnerError as exc:
            if not should_fallback_to_host_direct(exc):
                raise
    revision = ensure_revision(cfg) if packaged_bundle_ready else ensure_host_build(cfg)
    ensure_dummy_block_images(cfg)
    qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
    qemu_log_path.unlink(missing_ok=True)
    qemu_serial_log_path.unlink(missing_ok=True)

    cargo_stdout_path = work_dir / "qemu.stdout.txt"
    cargo_stderr_path = work_dir / "qemu.stderr.txt"
    ext2_image, exfat_image = prepare_run_block_images(cfg, work_dir)
    if packaged_bundle_ready:
        run_env = host_osdk_env(work_dir, boot_method="grub-rescue-iso")
        run_env["OVMF"] = "on"
        run_env["OVMF_CODE_FILE"] = str(system_ovmf_code_path())
        run_env["OVMF_VARS_FILE"] = str(prepare_ovmf_vars(work_dir))
        run_env["EXT2_IMAGE"] = str(ext2_image)
        run_env["EXFAT_IMAGE"] = str(exfat_image)
        qemu_command, qemu_cwd = host_grub_bundle_command(cfg, resolved_package_dir, work_dir)
    else:
        run_env = host_osdk_env(work_dir, boot_method="qemu-direct")
        run_env["OVMF"] = "on"
        run_env["OVMF_CODE_FILE"] = str(system_ovmf_code_path())
        run_env["OVMF_VARS_FILE"] = str(prepare_ovmf_vars(work_dir))
        run_env["EXT2_IMAGE"] = str(ext2_image)
        run_env["EXFAT_IMAGE"] = str(exfat_image)
        run_env["SYZABI_GUEST_KCMD_ARGS"] = selected_guest_cmdline_append()
        qemu_command, qemu_cwd = qemu_direct_command(cfg, custom_initramfs, run_env)

    with cargo_stdout_path.open("a", encoding="utf-8") as cargo_stdout, cargo_stderr_path.open("a", encoding="utf-8") as cargo_stderr:
        run = subprocess.Popen(
            qemu_command,
            cwd=qemu_cwd,
            env=run_env,
            text=True,
            stdout=cargo_stdout,
            stderr=cargo_stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + selected_run_timeout_sec(cfg)
        while True:
            console_preview = read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
            markers_complete = extract_section(console_preview, "PROCESS_EXIT") is not None and extract_section(console_preview, "EXTERNAL_STATE") is not None
            if markers_complete:
                stop_qemu_processes(work_dir)
                stop_process(run)
                break
            if run.poll() is not None:
                break
            if time.monotonic() >= deadline:
                stop_qemu_processes(work_dir)
                stop_process(run)
                stdout = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
                stderr = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
                raise subprocess.TimeoutExpired(run.args, selected_run_timeout_sec(cfg), output=stdout, stderr=stderr)
            time.sleep(1)
        if run.poll() is None:
            stop_process(run)

    stdout_text = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
    console_text = read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
    console_log_path.write_text(console_text, encoding="utf-8")

    process_exit = parse_process_exit(extract_section(console_text, "PROCESS_EXIT"))
    stdout_path.write_text(extract_section(console_text, "STDOUT") or "", encoding="utf-8")
    stderr_path.write_text(extract_section(console_text, "STDERR") or "", encoding="utf-8")
    external_state = parse_external_state(extract_section(console_text, "EXTERNAL_STATE"))
    dump_json(external_state_path, external_state)

    if extract_section(console_text, "PROCESS_EXIT") is None:
        if write_missing_marker_crash_result(
            console_text=console_text,
            raw_trace_path=raw_trace_path,
            external_state_path=external_state_path,
            kernel_build=f"asterinas@{revision[:12]}",
        ):
            return
        detail = stderr_text.strip() or stdout_text.strip()
        raise RunnerError(detail or "Asterinas autorun markers not found")

    events = parse_events(extract_section(console_text, "EVENTS"))
    status = candidate_status_from_events(events, process_exit)
    raw_trace = {
        "program_id": os.environ.get("SYZABI_PROGRAM_ID", "unknown"),
        "side": os.environ.get("SYZABI_SIDE", "candidate"),
        "run_id": os.environ.get("SYZABI_RUN_ID", "unknown"),
        "status": status,
        "events": events,
        "process_exit": process_exit,
    }
    validate_raw_trace(raw_trace)
    dump_json(raw_trace_path, raw_trace)

    write_runner_result(
        {
            "status": status,
            "exit_code": process_exit.get("exit_code"),
            "kernel_build": f"asterinas@{revision[:12]}",
        }
    )


def docker_qemu_run(args: argparse.Namespace) -> None:
    if not args.binary or not args.work_dir:
        raise RunnerError("--binary and --work-dir are required in docker-qemu mode")

    cfg = read_workflow_config()
    binary_path = Path(args.binary).resolve()
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    stdout_path = required_env_path("SYZABI_STDOUT_PATH")
    stderr_path = required_env_path("SYZABI_STDERR_PATH")
    console_log_path = required_env_path("SYZABI_CONSOLE_LOG_PATH")
    raw_trace_path = required_env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = required_env_path("SYZABI_EXTERNAL_STATE_PATH")
    result_path = runner_result_path()
    for stale_path in (stdout_path, stderr_path, console_log_path, raw_trace_path, external_state_path, result_path):
        stale_path.unlink(missing_ok=True)

    custom_initramfs = selected_initramfs(cfg, binary_path, work_dir)
    ensure_dummy_block_images(cfg)
    qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
    qemu_log_path.unlink(missing_ok=True)
    qemu_serial_log_path.unlink(missing_ok=True)

    cargo_stdout_path = work_dir / "docker-qemu.stdout.txt"
    cargo_stderr_path = work_dir / "docker-qemu.stderr.txt"
    ext2_image, exfat_image = prepare_run_block_images(cfg, work_dir)
    container_name = container_name_for_run(
        os.environ.get("SYZABI_PROGRAM_ID", "program"),
        os.environ.get("SYZABI_RUN_ID", "run"),
    )
    run_env = docker_run_env(cfg, work_dir)
    guest_kcmd_args = " ".join(part for part in ("console=hvc0", selected_guest_cmdline_append()) if part)
    package_dir = env_path("SYZABI_ASTERINAS_PACKAGE_DIR")
    if package_dir is not None:
        resolved_package_dir = package_dir.resolve()
        ensure_packaged_docker_bundle(cfg, resolved_package_dir, custom_initramfs, kcmd_args=guest_kcmd_args)
        revision = ensure_revision(cfg)
        run_cargo_home = ensure_shared_package_cargo_home(resolved_package_dir)
        cargo_target_dir, osdk_output_dir = shared_package_runtime_dirs(resolved_package_dir)
    else:
        revision = ensure_docker_build(cfg)
        run_cargo_home = prepare_run_cargo_home(work_dir)
        cargo_target_dir = work_dir / "cargo-target"
        osdk_output_dir = work_dir / "osdk-output"
    run_env["CARGO_HOME"] = str(host_path_to_container_path(run_cargo_home, cfg))
    run_env["CARGO_TARGET_DIR"] = str(host_path_to_container_path(cargo_target_dir, cfg))
    run_env["OSDK_OUTPUT_DIR"] = str(host_path_to_container_path(osdk_output_dir, cfg))
    run_env["CARGO_NET_OFFLINE"] = "true"
    run_env["EXT2_IMAGE"] = str(host_path_to_container_path(ext2_image, cfg))
    run_env["EXFAT_IMAGE"] = str(host_path_to_container_path(exfat_image, cfg))
    if package_dir is not None:
        qemu_command = docker_run_command(
            cfg,
            docker_grub_bundle_script(cfg, resolved_package_dir, work_dir),
            extra_env=run_env,
            workdir=docker_repo_dir(cfg),
            container_name=container_name,
        )
    else:
        qemu_command = docker_run_command(
            cfg,
            docker_osdk_run_script(cfg, work_dir, custom_initramfs, kcmd_args=guest_kcmd_args),
            extra_env=run_env,
            workdir=docker_repo_dir(cfg),
            container_name=container_name,
        )

    with cargo_stdout_path.open("a", encoding="utf-8") as cargo_stdout, cargo_stderr_path.open("a", encoding="utf-8") as cargo_stderr:
        run = subprocess.Popen(
            qemu_command,
            text=True,
            stdout=cargo_stdout,
            stderr=cargo_stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + selected_run_timeout_sec(cfg)
        try:
            while True:
                console_preview = read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
                markers_complete = extract_section(console_preview, "PROCESS_EXIT") is not None and extract_section(console_preview, "EXTERNAL_STATE") is not None
                if markers_complete:
                    force_remove_container(container_name)
                    stop_process(run)
                    break
                if run.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    force_remove_container(container_name)
                    stop_process(run)
                    stdout = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
                    stderr = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
                    raise subprocess.TimeoutExpired(run.args, selected_run_timeout_sec(cfg), output=stdout, stderr=stderr)
                time.sleep(1)
            if run.poll() is None:
                stop_process(run)
        finally:
            force_remove_container(container_name)

    stdout_text = cargo_stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr_text = cargo_stderr_path.read_text(encoding="utf-8", errors="replace")
    console_text = read_console_text(qemu_log_path, qemu_serial_log_path, cargo_stdout_path, cargo_stderr_path)
    console_log_path.write_text(console_text, encoding="utf-8")

    process_exit = parse_process_exit(extract_section(console_text, "PROCESS_EXIT"))
    stdout_path.write_text(extract_section(console_text, "STDOUT") or "", encoding="utf-8")
    stderr_path.write_text(extract_section(console_text, "STDERR") or "", encoding="utf-8")
    external_state = parse_external_state(extract_section(console_text, "EXTERNAL_STATE"))
    dump_json(external_state_path, external_state)

    if extract_section(console_text, "PROCESS_EXIT") is None:
        if write_missing_marker_crash_result(
            console_text=console_text,
            raw_trace_path=raw_trace_path,
            external_state_path=external_state_path,
            kernel_build=f"asterinas@{revision[:12]}",
        ):
            return
        detail = stderr_text.strip() or stdout_text.strip()
        raise RunnerError(detail or "Asterinas autorun markers not found")

    events = parse_events(extract_section(console_text, "EVENTS"))
    status = candidate_status_from_events(events, process_exit)
    raw_trace = {
        "program_id": os.environ.get("SYZABI_PROGRAM_ID", "unknown"),
        "side": os.environ.get("SYZABI_SIDE", "candidate"),
        "run_id": os.environ.get("SYZABI_RUN_ID", "unknown"),
        "status": status,
        "events": events,
        "process_exit": process_exit,
    }
    validate_raw_trace(raw_trace)
    dump_json(raw_trace_path, raw_trace)

    write_runner_result(
        {
            "status": status,
            "exit_code": process_exit.get("exit_code"),
            "kernel_build": f"asterinas@{revision[:12]}",
        }
    )


def main() -> None:
    args = parse_args()
    try:
        if args.healthcheck:
            if args.mode == "unconfigured":
                raise RunnerError("asterinas runner is not configured")
            if args.mode == "local-proxy":
                write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": "local-proxy"})
                return
            cfg = read_workflow_config()
            if args.mode == "docker-qemu":
                try:
                    revision = ensure_docker_build(cfg)
                except RunnerError as exc:
                    if not should_fallback_to_host_direct(exc):
                        raise
                    sys.stderr.write(
                        "warning: docker-qemu healthcheck unavailable, falling back to host-direct\n"
                    )
                    revision = ensure_host_build(cfg)
            else:
                revision = ensure_host_build(cfg)
            write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": f"asterinas@{revision[:12]}"})
            return

        if args.mode == "unconfigured":
            raise RunnerError("asterinas runner is not configured")
        if args.mode == "local-proxy":
            local_proxy(args)
            return
        if args.batch_manifest:
            if args.mode != "docker-qemu":
                raise RunnerError("batch manifest mode currently supports docker-qemu only")
            docker_qemu_batch_run(args)
            return
        if args.mode == "docker-qemu":
            try:
                docker_qemu_run(args)
            except RunnerError as exc:
                if not should_fallback_to_host_direct(exc):
                    raise
                sys.stderr.write(
                    "warning: docker-qemu unavailable for candidate replay, falling back to host-direct\n"
                )
                host_direct_run(args)
            return
        host_direct_run(args)
    except subprocess.TimeoutExpired as exc:
        write_runner_result(
            {
                "status": "timeout",
                "exit_code": None,
                "status_detail": f"runner timed out after {exc.timeout} seconds",
                "kernel_build": "asterinas-timeout",
            }
        )
        raise SystemExit("asterinas runner timed out")
    except RunnerError as exc:
        write_runner_result(
            {
                "status": "infra_error",
                "exit_code": None,
                "status_detail": str(exc),
                "kernel_build": "asterinas-unconfigured",
            }
        )
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
