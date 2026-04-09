#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.schemas import validate_raw_trace
from orchestrator.common import config, configure_runtime, dump_json, repo_root, resolve_repo_path


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

ASTERINAS_GIT_MIRRORS = {
    "inherit-methods-macro": "https://github.com/asterinas/inherit-methods-macro",
    "inventory": "https://github.com/asterinas/inventory",
    "rust-ctor": "https://github.com/asterinas/rust-ctor",
    "smoltcp": "https://github.com/asterinas/smoltcp",
}


class RunnerError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
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


def read_workflow_config() -> dict[str, object]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "asterinas")
    configure_runtime(workflow=workflow)
    cfg = config()
    if cfg.get("workflow") != "asterinas":
        raise RunnerError(f"run_asterinas.py requires asterinas config, got {cfg.get('workflow')}")
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
            raise RunnerError(update.stderr.strip() or update.stdout.strip() or f"failed to update git mirror {name}")
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


def prime_docker_cargo_cache(cfg: dict[str, object]) -> None:
    cargo_home = docker_cargo_home()
    cargo_home.mkdir(parents=True, exist_ok=True)
    ensure_docker_cargo_cache_dirs()
    manifest_path = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "Cargo.toml"
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
            "TMPDIR": str(local_tmp_dir()),
        },
    )
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "failed to prefetch Asterinas cargo dependencies"
        raise RunnerError(detail)


def prepare_docker_gitconfig(cfg: dict[str, object]) -> Path:
    mirrors = ensure_asterinas_git_mirrors()
    config_path = resolve_repo_path("artifacts/asterinas/docker-gitconfig")
    lines: list[str] = []
    for name, remote_url in ASTERINAS_GIT_MIRRORS.items():
        mirror_path = host_path_to_container_path(mirrors[name], cfg)
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
    config_path.write_text("\n".join(lines), encoding="utf-8")
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
    path = Path.home() / "tmp" / "fuzzasterinas"
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def extract_section(console_text: str, name: str) -> str | None:
    begin = f"{MARKER_PREFIX}_BEGIN_{name}__\n"
    end = f"\n{MARKER_PREFIX}_END_{name}__"
    start = console_text.find(begin)
    if start < 0:
        return None
    start += len(begin)
    finish = console_text.find(end, start)
    if finish < 0:
        return None
    return console_text[start:finish].strip("\n")


def parse_events(section: str | None) -> list[dict[str, object]]:
    if not section:
        return []
    events: list[dict[str, object]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def parse_process_exit(section: str | None) -> dict[str, object]:
    if not section:
        return {"status": "infra_error", "exit_code": None, "timed_out": False}
    return json.loads(section)


def parse_external_state(section: str | None) -> dict[str, object]:
    if not section:
        return {"files": []}
    return json.loads(section)


def candidate_status_from_events(events: list[dict[str, object]], process_exit: dict[str, object]) -> str:
    if process_exit.get("status") == "crash":
        return "crash"
    for event in events:
        if int(event.get("return_value", 0)) == -1 and int(event.get("errno", 0)) in {38, 95}:
            return "unsupported"
    return "ok"


def build_info_path(cfg: dict[str, object]) -> Path:
    return resolve_repo_path(cfg["asterinas"]["build_info_path"])


def built_bundle_dir() -> Path:
    return resolve_repo_path("third_party/asterinas/target/osdk/aster-kernel")


def build_probe_root() -> Path:
    root = resolve_repo_path("artifacts/asterinas/build-probe")
    root.mkdir(parents=True, exist_ok=True)
    return root


def build_probe_initramfs(cfg: dict[str, object]) -> Path:
    probe_root = build_probe_root()
    shutil.copy2("/usr/bin/true", probe_root / "probe.bin")
    return create_minimal_initramfs(cfg, probe_root / "probe.bin", probe_root)


def load_bundle_manifest() -> dict[str, object]:
    manifest_path = built_bundle_dir() / "bundle.toml"
    if not manifest_path.exists():
        raise RunnerError(f"missing OSDK bundle manifest: {manifest_path}")
    return tomllib.loads(manifest_path.read_text(encoding="utf-8"))


def shared_bzimage_path() -> Path:
    return resolve_repo_path("third_party/asterinas/target/osdk/iso_root/boot/aster-kernel-osdk-bin")


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


def kernel_build_ready() -> bool:
    try:
        load_bundle_manifest()
    except RunnerError:
        return False
    return shared_bzimage_path().exists() and shared_bzimage_path().stat().st_size > 1024


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
    process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        process.kill()
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
    return ext2_image, exfat_image


def osdk_build_command(initramfs_path: Path) -> list[str]:
    return [
        *cargo_osdk_base_command(),
        "build",
        "--target-arch=x86_64",
        "--boot-method=grub-rescue-iso",
        "--grub-boot-protocol=linux",
        "--linux-x86-legacy-boot",
        "--kcmd-args=console=hvc0",
        "--initramfs",
        str(initramfs_path),
    ]


def osdk_run_command(initramfs_path: Path) -> list[str]:
    return [
        *cargo_osdk_base_command(),
        "run",
        "--target-arch=x86_64",
        "--kcmd-args=console=hvc0",
        "--initramfs",
        str(initramfs_path),
    ]


def build_lock_path(cfg: dict[str, object]) -> Path:
    info_path = build_info_path(cfg)
    return info_path.with_name(f"{info_path.name}.lock")


def ensure_host_build(cfg: dict[str, object]) -> str:
    revision = ensure_revision(cfg)
    info_path = build_info_path(cfg)
    lock_path = build_lock_path(cfg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        ensure_host_osdk(cfg)
        probe_root = build_probe_root()
        env = host_osdk_env(probe_root, boot_method="grub-rescue-iso")
        ensure_dummy_block_images(cfg)
        initramfs_path = build_probe_initramfs(cfg)

        if info_path.exists():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if (
                info.get("revision") == revision
                and info.get("mode") == "host-direct"
                and info.get("boot_method") == "grub-rescue-iso/linux"
                and kernel_build_ready()
            ):
                return revision

        repo = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "kernel"
        build = subprocess.run(
            osdk_build_command(initramfs_path),
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
                "boot_method": "grub-rescue-iso/linux",
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
                and kernel_build_ready()
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


def qemu_direct_command(cfg: dict[str, object], initramfs_path: Path, env: dict[str, str]) -> tuple[list[str], Path]:
    manifest = load_bundle_manifest()
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing run section")
    qemu_section = run_section.get("qemu")
    if not isinstance(qemu_section, dict):
        raise RunnerError("invalid OSDK bundle manifest: missing qemu section")

    command = [
        str(qemu_section["path"]),
        "-kernel",
        str(shared_bzimage_path()),
        "-initrd",
        str(initramfs_path),
        "-append",
        bundle_kcmdline(manifest),
    ]
    command.extend(qemu_args_tokens(cfg, env))
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


def docker_osdk_run_script(cfg: dict[str, object], work_dir: Path, initramfs_path: Path) -> str:
    container_work_dir = host_path_to_container_path(work_dir, cfg)
    container_initramfs = host_path_to_container_path(initramfs_path, cfg)
    container_osdk_output = host_path_to_container_path(work_dir / "osdk-output", cfg)
    container_ovmf_vars = host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)
    return "\n".join(
        [
            "set -euo pipefail",
            f"mkdir -p {shlex.quote(str(container_work_dir))} {shlex.quote(str(container_osdk_output))}",
            f"if [ ! -f {shlex.quote(str(container_ovmf_vars))} ]; then cp {shlex.quote(container_ovmf_vars_seed_path())} {shlex.quote(str(container_ovmf_vars))}; fi",
            f"cd {shlex.quote(str(docker_repo_dir(cfg) / 'kernel'))}",
            " ".join(shlex.quote(part) for part in osdk_run_command(container_initramfs)),
        ]
    )


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

    custom_initramfs = create_minimal_initramfs(cfg, binary_path, work_dir)
    revision = ensure_revision(cfg)
    ensure_host_osdk(cfg)
    ensure_dummy_block_images(cfg)
    qemu_log_path, qemu_serial_log_path = qemu_log_paths(work_dir)
    qemu_log_path.unlink(missing_ok=True)
    qemu_serial_log_path.unlink(missing_ok=True)

    cargo_stdout_path = work_dir / "qemu.stdout.txt"
    cargo_stderr_path = work_dir / "qemu.stderr.txt"
    ext2_image, exfat_image = prepare_run_block_images(cfg, work_dir)
    run_env = host_osdk_env(work_dir, boot_method="grub-rescue-iso")
    run_env["OVMF"] = "on"
    run_env["OVMF_CODE_FILE"] = str(system_ovmf_code_path())
    run_env["OVMF_VARS_FILE"] = str(prepare_ovmf_vars(work_dir))
    run_env["EXT2_IMAGE"] = str(ext2_image)
    run_env["EXFAT_IMAGE"] = str(exfat_image)
    run_env["OSDK_OUTPUT_DIR"] = str(work_dir / "osdk-output")
    qemu_command = osdk_run_command(custom_initramfs)
    qemu_cwd = resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "kernel"

    with cargo_stdout_path.open("a", encoding="utf-8") as cargo_stdout, cargo_stderr_path.open("a", encoding="utf-8") as cargo_stderr:
        run = subprocess.Popen(
            qemu_command,
            cwd=qemu_cwd,
            env=run_env,
            text=True,
            stdout=cargo_stdout,
            stderr=cargo_stderr,
        )
        deadline = time.monotonic() + int(cfg["asterinas"]["run_timeout_sec"])
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
                raise subprocess.TimeoutExpired(run.args, int(cfg["asterinas"]["run_timeout_sec"]), output=stdout, stderr=stderr)
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

    custom_initramfs = create_minimal_initramfs(cfg, binary_path, work_dir)
    revision = ensure_docker_build(cfg)
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
    run_env["EXT2_IMAGE"] = str(host_path_to_container_path(ext2_image, cfg))
    run_env["EXFAT_IMAGE"] = str(host_path_to_container_path(exfat_image, cfg))
    qemu_command = docker_run_command(
        cfg,
        docker_osdk_run_script(cfg, work_dir, custom_initramfs),
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
        )
        deadline = time.monotonic() + int(cfg["asterinas"]["run_timeout_sec"])
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
                    raise subprocess.TimeoutExpired(run.args, int(cfg["asterinas"]["run_timeout_sec"]), output=stdout, stderr=stderr)
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
            revision = ensure_docker_build(cfg) if args.mode == "docker-qemu" else ensure_host_build(cfg)
            write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": f"asterinas@{revision[:12]}"})
            return

        if args.mode == "unconfigured":
            raise RunnerError("asterinas runner is not configured")
        if args.mode == "local-proxy":
            local_proxy(args)
            return
        if args.mode == "docker-qemu":
            docker_qemu_run(args)
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
