from __future__ import annotations

import os
import shlex
import shutil
import signal
import socket
import time
from pathlib import Path

from targets.asterinas import paths as path_mod


def ensure_vdso_dir(*, hooks) -> Path:
    destination = path_mod.linux_vdso_dir()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "vdso_x86_64.so"
    if target.exists():
        return destination
    candidates = sorted(Path("/usr/lib/modules").glob("*/vdso/vdso64.so"))
    if not candidates:
        raise hooks.RunnerError("failed to find host vdso64.so")
    shutil.copy2(candidates[0], target)
    return destination


def ensure_dummy_block_images(cfg: dict[str, object], *, hooks) -> None:
    build_dir = hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "test/initramfs/build"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in ("ext2.img", "exfat.img"):
        path = build_dir / name
        if path.exists():
            continue
        with path.open("wb") as handle:
            handle.truncate(1024 * 1024)


def ensure_local_mtools(*, hooks) -> Path | None:
    existing = hooks.shutil.which("mformat")
    if existing:
        return None

    tool_root = path_mod.mtools_root()
    binary = tool_root / "usr/bin/mformat"
    if binary.exists():
        return tool_root / "usr/bin"

    downloads = path_mod.host_tools_downloads_dir()
    downloads.mkdir(parents=True, exist_ok=True)
    download = hooks.subprocess.run(
        ["apt", "download", "mtools"],
        cwd=downloads,
        env={**hooks.os.environ, "TMPDIR": str(hooks.local_tmp_dir())},
        text=True,
        capture_output=True,
        check=False,
    )
    if download.returncode != 0:
        raise hooks.RunnerError(download.stderr.strip() or download.stdout.strip() or "failed to download mtools")

    packages = sorted(downloads.glob("mtools_*_amd64.deb"))
    if not packages:
        raise hooks.RunnerError("failed to locate downloaded mtools package")

    tool_root.mkdir(parents=True, exist_ok=True)
    extract = hooks.subprocess.run(
        ["dpkg-deb", "-x", str(packages[-1]), str(tool_root)],
        env={**hooks.os.environ, "TMPDIR": str(hooks.local_tmp_dir())},
        text=True,
        capture_output=True,
        check=False,
    )
    if extract.returncode != 0:
        raise hooks.RunnerError(extract.stderr.strip() or extract.stdout.strip() or "failed to extract mtools package")
    return tool_root / "usr/bin"


def selected_guest_cmdline_append(*, hooks) -> str:
    parts: list[str] = []
    extra = hooks.os.environ.get("SYZABI_GUEST_KCMD_ARGS")
    if extra:
        parts.append(extra)
    return " ".join(part for part in parts if part)


def guest_env_lines(*, hooks) -> list[str]:
    lines: list[str] = []
    if hooks.os.environ.get("SYZABI_ASTERINAS_PACKAGE_DIR") and not hooks.os.environ.get("SYZABI_ASTERINAS_PACKAGE_SLOT"):
        raise hooks.RunnerError("missing SYZABI_ASTERINAS_PACKAGE_SLOT for packaged candidate run")
    slot = hooks.os.environ.get("SYZABI_ASTERINAS_PACKAGE_SLOT")
    if slot:
        lines.append(f"SYZABI_PACKAGE_SLOT={shlex.quote(slot)}")
    inject_enabled = hooks.os.environ.get("SYZABI_INJECT_TRACE_ENABLED")
    if inject_enabled:
        lines.append(f"SYZABI_INJECT_TRACE_ENABLED={shlex.quote(inject_enabled)}")
    inject_call_index = hooks.os.environ.get("SYZABI_INJECT_TRACE_CALL_INDEX")
    if inject_call_index:
        lines.append(f"SYZABI_INJECT_TRACE_CALL_INDEX={shlex.quote(inject_call_index)}")
    inject_syscall = hooks.os.environ.get("SYZABI_INJECT_TRACE_SYSCALL")
    if inject_syscall:
        lines.append(f"SYZABI_INJECT_TRACE_SYSCALL={shlex.quote(inject_syscall)}")
    inject_field = hooks.os.environ.get("SYZABI_INJECT_TRACE_FIELD")
    if inject_field:
        lines.append(f"SYZABI_INJECT_TRACE_FIELD={shlex.quote(inject_field)}")
    inject_value = hooks.os.environ.get("SYZABI_INJECT_TRACE_VALUE")
    if inject_value:
        lines.append(f"SYZABI_INJECT_TRACE_VALUE={shlex.quote(inject_value)}")
    return lines


def guest_env_header_bytes(lines: list[str], *, hooks) -> bytes:
    payload = "\n".join([hooks.GUEST_ENV_HEADER_MAGIC, *lines, "__END__", ""]).encode("utf-8")
    if len(payload) > hooks.GUEST_ENV_HEADER_SIZE:
        raise hooks.RunnerError("guest env selector header exceeds reserved image space")
    return payload.ljust(hooks.GUEST_ENV_HEADER_SIZE, b" ")


def materialize_guest_env_file(ext2_image: Path, *, hooks) -> None:
    lines = hooks.guest_env_lines()
    if not lines:
        return
    with hooks.tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=hooks.local_tmp_dir()) as handle:
        handle.write("\n".join(lines) + "\n")
        temp_path = Path(handle.name)
    try:
        hooks.subprocess.run(
            ["debugfs", "-w", "-R", "rm /syzkabi.env", str(ext2_image)],
            text=True,
            capture_output=True,
            check=False,
        )
        write_result = hooks.subprocess.run(
            ["debugfs", "-w", "-R", f"write {temp_path} /syzkabi.env", str(ext2_image)],
            text=True,
            capture_output=True,
            check=False,
        )
        if write_result.returncode != 0:
            raise hooks.RunnerError(write_result.stderr.strip() or write_result.stdout.strip() or "failed to materialize guest env file")
        with ext2_image.open("r+b") as image_handle:
            image_handle.seek(0)
            image_handle.write(hooks.guest_env_header_bytes(lines))
    finally:
        temp_path.unlink(missing_ok=True)


def selected_initramfs(cfg: dict[str, object], binary_path: Path, work_dir: Path, *, hooks) -> Path:
    package_dir = hooks.env_path("SYZABI_ASTERINAS_PACKAGE_DIR")
    if package_dir is not None:
        return hooks.ensure_packaged_initramfs(cfg, package_dir.resolve())
    return hooks.create_minimal_initramfs(cfg, binary_path, work_dir)


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


def stop_process(process, *, hooks) -> None:
    if process.poll() is not None:
        return
    try:
        pgid = hooks.os.getpgid(process.pid)
    except ProcessLookupError:
        return
    try:
        hooks.os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except hooks.subprocess.TimeoutExpired:
        try:
            hooks.os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait(timeout=5)


def matching_qemu_pids(work_dir: Path, *, hooks) -> list[int]:
    marker = str((work_dir / "OVMF_VARS.fd").resolve())
    result = hooks.subprocess.run(
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


def stop_qemu_processes(work_dir: Path, *, hooks) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        pids = hooks.matching_qemu_pids(work_dir)
        if not pids:
            return
        for pid in pids:
            try:
                hooks.os.kill(pid, sig)
            except ProcessLookupError:
                continue
        time.sleep(1)


def host_osdk_env(work_dir: Path, *, hooks, boot_method: str = "qemu-direct") -> dict[str, str]:
    env = hooks.os.environ.copy()
    env["TMPDIR"] = str(hooks.local_tmp_dir())
    env["VDSO_LIBRARY_DIR"] = str(hooks.ensure_vdso_dir())
    env["CONSOLE"] = "hvc0"
    env["BOOT_METHOD"] = boot_method
    env["OVMF"] = "off"
    env["NETDEV"] = env.get("SYZABI_ASTERINAS_NETDEV", "user")
    env["QEMU_DISPLAY"] = "none"
    env["VNC_PORT"] = env.get("SYZABI_ASTERINAS_VNC_PORT", str(hooks.choose_available_tcp_port()))
    env["SMP"] = env.get("SYZABI_ASTERINAS_SMP", "1")
    env["MEM"] = env.get("SYZABI_ASTERINAS_MEM", "2G")
    for port_env in hooks.NETWORK_PORT_ENV_NAMES:
        env[port_env] = env.get(port_env, str(hooks.choose_available_tcp_port()))
    qemu_log_path, qemu_serial_log_path = hooks.qemu_log_paths(work_dir)
    env["QEMU_LOG_FILE"] = str(qemu_log_path)
    env["QEMU_SERIAL_LOG_FILE"] = str(qemu_serial_log_path)
    toolchain = hooks.asterinas_rust_toolchain()
    if toolchain:
        env["RUSTUP_TOOLCHAIN"] = toolchain
    mtools_bin = hooks.ensure_local_mtools()
    if mtools_bin is not None:
        env["PATH"] = f"{mtools_bin}:{env['PATH']}"
    return env


def reserve_tcp_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, int(sock.getsockname()[1])


def choose_available_tcp_port() -> int:
    sock, port = reserve_tcp_port()
    try:
        return port
    finally:
        try:
            sock.close()
        except OSError:
            pass


def reserve_qemu_ports(*, hooks) -> tuple[list[socket.socket], dict[str, int]]:
    sockets: list[socket.socket] = []
    ports: dict[str, int] = {}
    for name in hooks.NETWORK_PORT_ENV_NAMES:
        sock, port = hooks.reserve_tcp_port()
        sockets.append(sock)
        ports[name] = port
    return sockets, ports


def release_reserved_ports(sockets: list[socket.socket]) -> None:
    for sock in sockets:
        try:
            sock.close()
        except OSError:
            continue


def system_ovmf_code_path(*, hooks) -> Path:
    for candidate in (Path("/usr/share/OVMF/OVMF_CODE_4M.fd"), Path("/usr/share/ovmf/OVMF.fd")):
        if candidate.exists():
            return candidate
    # Fallback to user-local OVMF for non-root environments
    user_ovmf = Path.home() / ".local/share/OVMF/OVMF_CODE_4M.fd"
    if user_ovmf.exists():
        return user_ovmf
    raise hooks.RunnerError("failed to locate system OVMF code image")


def prepare_ovmf_vars(work_dir: Path, *, hooks) -> Path:
    candidates = [
        Path("/usr/share/OVMF/OVMF_VARS_4M.fd"),
        Path("/usr/share/OVMF/OVMF_VARS.fd"),
        Path.home() / ".local/share/OVMF/OVMF_VARS.fd",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        target = work_dir / "OVMF_VARS.fd"
        if not target.exists():
            shutil.copy2(candidate, target)
        return target
    raise hooks.RunnerError("failed to locate system OVMF vars image")


def prepare_run_block_images(cfg: dict[str, object], work_dir: Path, *, hooks) -> tuple[Path, Path]:
    source_dir = hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "test/initramfs/build"
    ext2_image = work_dir / "ext2.img"
    exfat_image = work_dir / "exfat.img"
    shutil.copy2(source_dir / "ext2.img", ext2_image)
    shutil.copy2(source_dir / "exfat.img", exfat_image)
    hooks.materialize_guest_env_file(ext2_image)
    return ext2_image, exfat_image


def qemu_args_tokens(cfg: dict[str, object], env: dict[str, str], *, hooks) -> list[str]:
    repo = hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"])
    result = hooks.subprocess.run(
        ["bash", "tools/qemu_args.sh", "normal"],
        cwd=repo,
        env=env,
        timeout=30,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise hooks.RunnerError(result.stderr.strip() or result.stdout.strip() or "failed to render qemu args")
    tokens = shlex.split(result.stdout.strip())
    qemu_log_file = env.get("QEMU_LOG_FILE")
    qemu_serial_log_file = env.get("QEMU_SERIAL_LOG_FILE")
    ext2_image = env.get("EXT2_IMAGE")
    exfat_image = env.get("EXFAT_IMAGE")
    tcg_cpu_model = env.get("SYZABI_ASTERINAS_TCG_CPU_MODEL", "max")
    rewritten: list[str] = []
    force_headless = env.get("QEMU_DISPLAY") == "none"
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if force_headless and token == "-display" and index + 1 < len(tokens) and tokens[index + 1].startswith("vnc="):
            rewritten.extend(["-display", "none"])
            index += 2
            continue
        if qemu_log_file:
            token = token.replace("logfile=qemu.log", f"logfile={qemu_log_file}")
        if qemu_serial_log_file:
            token = token.replace("file:qemu-serial.log", f"file:{qemu_serial_log_file}")
        if ext2_image:
            token = token.replace("file=./test/initramfs/build/ext2.img", f"file={ext2_image}")
        if exfat_image:
            token = token.replace("file=./test/initramfs/build/exfat.img", f"file={exfat_image}")
        if "hostfwd=" in token:
            token = ",".join(part for part in token.split(",") if not part.startswith("hostfwd="))
        if not hooks.kvm_accessible() and token == "Icelake-Server,+x2apic":
            token = tcg_cpu_model
        rewritten.append(token)
        index += 1
    return rewritten


def kvm_accessible(*, hooks) -> bool:
    return hooks.Path("/dev/kvm").exists() and hooks.os.access("/dev/kvm", hooks.os.R_OK | hooks.os.W_OK)


def kvm_enabled(env: dict[str, str]) -> bool:
    return env.get("SYZABI_ASTERINAS_ENABLE_KVM", "1") != "0"


def qemu_direct_command(cfg: dict[str, object], initramfs_path: Path, env: dict[str, str], *, hooks) -> tuple[list[str], Path]:
    manifest = hooks.load_bundle_manifest(cfg)
    config_section = manifest.get("config")
    if not isinstance(config_section, dict):
        raise hooks.RunnerError("invalid OSDK bundle manifest: missing config")
    run_section = config_section.get("run")
    if not isinstance(run_section, dict):
        raise hooks.RunnerError("invalid OSDK bundle manifest: missing run section")
    qemu_section = run_section.get("qemu")
    if not isinstance(qemu_section, dict):
        raise hooks.RunnerError("invalid OSDK bundle manifest: missing qemu section")

    kcmdline = hooks.bundle_kcmdline(manifest)
    extra_kcmd_args = env.get("SYZABI_GUEST_KCMD_ARGS", "").strip()
    if extra_kcmd_args:
        kcmdline = f"{kcmdline} {extra_kcmd_args}".strip()
    command = [
        str(qemu_section["path"]),
        "-kernel",
        str(hooks.shared_bzimage_path(cfg)),
        "-initrd",
        str(initramfs_path),
        "-append",
        kcmdline,
    ]
    command.extend(hooks.qemu_args_tokens(cfg, env))
    if hooks.kvm_enabled(env) and hooks.kvm_accessible() and "-accel" not in command and "-enable-kvm" not in command:
        command.extend(["-accel", "kvm"])
    return command, hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"])


def grub_iso_qemu_command(cfg: dict[str, object], bundle_dir: Path, env: dict[str, str], *, hooks) -> tuple[list[str], Path]:
    manifest = hooks.load_external_bundle_manifest(bundle_dir)
    image_path = hooks.bundle_grub_iso_path(bundle_dir, manifest)
    command = [
        hooks.bundle_qemu_path(manifest),
        "-drive",
        f"file={image_path},format=raw,index=2,media=cdrom",
    ]
    command.extend(hooks.qemu_args_tokens(cfg, env))
    if hooks.kvm_enabled(env) and hooks.kvm_accessible() and "-accel" not in command and "-enable-kvm" not in command:
        command.extend(["-accel", "kvm"])
    return command, hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"])


def container_ovmf_code_path() -> str:
    return "/root/ovmf/release/OVMF_CODE.fd"


def container_ovmf_vars_seed_path() -> str:
    return "/root/ovmf/release/OVMF_VARS.fd"


def docker_run_env(cfg: dict[str, object], work_dir: Path, *, hooks) -> dict[str, str]:
    qemu_log_path, qemu_serial_log_path = hooks.qemu_log_paths(work_dir)
    osdk_output_dir = work_dir / "osdk-output"
    env = {
        "BOOT_METHOD": "grub-rescue-iso",
        "CONSOLE": "hvc0",
        "EXT2_IMAGE": str(hooks.host_path_to_container_path(work_dir / "ext2.img", cfg)),
        "EXFAT_IMAGE": str(hooks.host_path_to_container_path(work_dir / "exfat.img", cfg)),
        "MEM": hooks.os.environ.get("SYZABI_ASTERINAS_MEM", "2G"),
        "NETDEV": hooks.os.environ.get("SYZABI_ASTERINAS_NETDEV", "user"),
        "OSDK_OUTPUT_DIR": str(hooks.host_path_to_container_path(osdk_output_dir, cfg)),
        "OVMF": "on",
        "OVMF_CODE_FILE": hooks.container_ovmf_code_path(),
        "OVMF_VARS_FILE": str(hooks.host_path_to_container_path(work_dir / "OVMF_VARS.fd", cfg)),
        "QEMU_DISPLAY": "none",
        "QEMU_LOG_FILE": str(hooks.host_path_to_container_path(qemu_log_path, cfg)),
        "QEMU_SERIAL_LOG_FILE": str(hooks.host_path_to_container_path(qemu_serial_log_path, cfg)),
        "VNC_PORT": hooks.os.environ.get("SYZABI_ASTERINAS_VNC_PORT", str(hooks.choose_available_tcp_port())),
        "SMP": hooks.os.environ.get("SYZABI_ASTERINAS_SMP", "1"),
    }
    for port_env in hooks.NETWORK_PORT_ENV_NAMES:
        env[port_env] = hooks.os.environ.get(port_env, str(hooks.choose_available_tcp_port()))
    return env
