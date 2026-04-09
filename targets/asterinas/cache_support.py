from __future__ import annotations

import fcntl
import os
import re
import shlex
import shutil
from pathlib import Path

from targets.asterinas import paths as path_mod


def asterinas_git_mirror_root(*, hooks) -> Path:
    return path_mod.git_mirror_root()


def ensure_git_mirror(name: str, remote_url: str, *, hooks) -> Path:
    mirror_root = hooks.asterinas_git_mirror_root()
    mirror_root.mkdir(parents=True, exist_ok=True)
    mirror_path = mirror_root / f"{name}.git"
    if mirror_path.exists():
        update = hooks.subprocess.run(
            ["git", "--git-dir", str(mirror_path), "remote", "update", "--prune"],
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
        if update.returncode != 0:
            detail = update.stderr.strip() or update.stdout.strip() or f"failed to update git mirror {name}"
            if (mirror_path / "HEAD").exists():
                hooks.sys.stderr.write(f"warning: using stale git mirror {name}: {detail}\n")
                return mirror_path
            raise hooks.RunnerError(detail)
        return mirror_path
    clone = hooks.subprocess.run(
        ["git", "clone", "--mirror", remote_url, str(mirror_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if clone.returncode != 0:
        raise hooks.RunnerError(clone.stderr.strip() or clone.stdout.strip() or f"failed to clone git mirror {name}")
    return mirror_path


def ensure_asterinas_git_mirrors(*, hooks) -> dict[str, Path]:
    return {
        name: hooks.ensure_git_mirror(name, remote_url)
        for name, remote_url in hooks.ASTERINAS_GIT_MIRRORS.items()
    }


def existing_asterinas_git_mirrors(*, hooks) -> dict[str, Path]:
    mirror_root = hooks.asterinas_git_mirror_root()
    mirrors: dict[str, Path] = {}
    for name in hooks.ASTERINAS_GIT_MIRRORS:
        mirror_path = mirror_root / f"{name}.git"
        if (mirror_path / "HEAD").exists():
            mirrors[name] = mirror_path
    return mirrors


def docker_cargo_home(*, hooks) -> Path:
    return path_mod.docker_cargo_home()


def shared_cargo_osdk_path(*, hooks) -> Path:
    return hooks.docker_cargo_home() / "bin" / "cargo-osdk"


def container_cargo_home(cfg: dict[str, object], *, hooks) -> Path:
    return hooks.host_path_to_container_path(hooks.docker_cargo_home(), cfg)


def ensure_docker_cargo_cache_dirs(*, hooks) -> tuple[Path, Path]:
    cargo_root = hooks.docker_cargo_home()
    git_dir = cargo_root / "git"
    registry_dir = cargo_root / "registry"
    (cargo_root / "bin").mkdir(parents=True, exist_ok=True)
    git_dir.mkdir(parents=True, exist_ok=True)
    registry_dir.mkdir(parents=True, exist_ok=True)
    package_cache = cargo_root / ".package-cache"
    package_cache.touch(exist_ok=True)
    return git_dir, registry_dir


def prepare_run_cargo_home(work_dir: Path, *, hooks) -> Path:
    shared_home = hooks.docker_cargo_home()
    hooks.ensure_docker_cargo_cache_dirs()
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


def ensure_shared_package_cargo_home(package_dir: Path, *, hooks, refresh: bool = False) -> Path:
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
        shared_home = hooks.docker_cargo_home()
        hooks.ensure_docker_cargo_cache_dirs()
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


def prime_docker_cargo_cache(cfg: dict[str, object], *, hooks) -> None:
    cargo_home = hooks.docker_cargo_home()
    cargo_home.mkdir(parents=True, exist_ok=True)
    hooks.ensure_docker_cargo_cache_dirs()
    manifest_path = hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"]) / "Cargo.toml"
    gitconfig_path = hooks.prepare_host_gitconfig(cfg)
    fetch = hooks.subprocess.run(
        ["cargo", "fetch", "--locked", "--manifest-path", str(manifest_path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=int(cfg["asterinas"]["build_timeout_sec"]),
        env={
            **hooks.os.environ,
            "CARGO_HOME": str(cargo_home),
            "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
            "CARGO_TERM_PROGRESS_WHEN": "never",
            "GIT_CONFIG_GLOBAL": str(gitconfig_path),
            "TMPDIR": str(hooks.local_tmp_dir()),
        },
    )
    if fetch.returncode != 0:
        detail = fetch.stderr.strip() or fetch.stdout.strip() or "failed to prefetch Asterinas cargo dependencies"
        raise hooks.RunnerError(detail)


def gitconfig_lines(
    cfg: dict[str, object],
    *,
    hooks,
    path_transform,
    ensure_mirrors: bool,
) -> list[str]:
    mirrors = hooks.ensure_asterinas_git_mirrors() if ensure_mirrors else hooks.existing_asterinas_git_mirrors()
    lines: list[str] = []
    for name, remote_url in hooks.ASTERINAS_GIT_MIRRORS.items():
        mirror = mirrors.get(name)
        if mirror is None:
            continue
        mirror_path = path_transform(mirror, cfg)
        lines.extend([
            "[safe]",
            f"\tdirectory = {mirror_path}",
            "",
        ])
        for source_url in (remote_url, f"{remote_url}.git"):
            lines.extend([
                f'[url "file://{mirror_path}"]',
                f"\tinsteadOf = {source_url}",
                "",
            ])
    return lines


def prepare_host_gitconfig(cfg: dict[str, object], *, hooks) -> Path:
    config_path = path_mod.host_gitconfig_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(hooks.gitconfig_lines(cfg, path_transform=lambda path, _: path, ensure_mirrors=True)),
        encoding="utf-8",
    )
    return config_path


def prepare_docker_gitconfig(cfg: dict[str, object], *, hooks) -> Path:
    config_path = path_mod.docker_gitconfig_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(hooks.gitconfig_lines(cfg, path_transform=hooks.host_path_to_container_path, ensure_mirrors=False)),
        encoding="utf-8",
    )
    return config_path


def host_path_to_container_path(path: Path, cfg: dict[str, object], *, hooks) -> Path:
    resolved = path.resolve()
    workspace_root = hooks.repo_root().resolve()
    try:
        relative = resolved.relative_to(workspace_root)
    except ValueError as exc:
        raise hooks.RunnerError(f"path is outside workspace and cannot be mounted into Docker: {resolved}") from exc
    return hooks.docker_workspace_dir(cfg) / relative


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
    hooks,
    extra_env: dict[str, str] | None = None,
    workdir: Path | None = None,
    container_name: str | None = None,
) -> list[str]:
    workspace_root = hooks.repo_root().resolve()
    asterinas_repo = hooks.resolve_repo_path(cfg["asterinas"]["repo_dir"]).resolve()
    hooks.ensure_docker_cargo_cache_dirs()
    gitconfig_path = hooks.prepare_docker_gitconfig(cfg)
    shared_cargo_home = hooks.container_cargo_home(cfg)
    prefixed_script = "\n".join([
        f"export PATH={shlex.quote(str(shared_cargo_home / 'bin'))}:$PATH",
        script,
    ])
    command = [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--network=host",
        "-v",
        "/dev:/dev",
        "-v",
        f"{asterinas_repo}:{hooks.docker_repo_dir(cfg)}",
        "-v",
        f"{workspace_root}:{hooks.docker_workspace_dir(cfg)}",
    ]
    if workdir is not None:
        command.extend(["-w", str(workdir)])
    if container_name:
        command.extend(["--name", container_name])
    merged_env = {
        "CARGO_HOME": str(shared_cargo_home),
        "CARGO_HTTP_TIMEOUT": hooks.os.environ.get("SYZABI_ASTERINAS_CARGO_HTTP_TIMEOUT", "600"),
        "CARGO_NET_GIT_FETCH_WITH_CLI": "true",
        "CARGO_NET_RETRY": hooks.os.environ.get("SYZABI_ASTERINAS_CARGO_NET_RETRY", "10"),
        "CARGO_REGISTRIES_CRATES_IO_PROTOCOL": hooks.os.environ.get(
            "SYZABI_ASTERINAS_CARGO_REGISTRY_PROTOCOL",
            "sparse",
        ),
        "CARGO_TERM_PROGRESS_WHEN": "never",
        "GIT_CONFIG_GLOBAL": str(hooks.host_path_to_container_path(gitconfig_path, cfg)),
    }
    if extra_env:
        merged_env.update(extra_env)
    command.extend(hooks.docker_env_options(merged_env))
    command.extend([
        str(cfg["asterinas"]["docker_image"]),
        "bash",
        "-lc",
        prefixed_script,
    ])
    return command


def docker_make_kernel_command(cfg: dict[str, object], *, hooks) -> list[str]:
    shared_cargo_home = hooks.container_cargo_home(cfg)
    return hooks.docker_run_command(
        cfg,
        f"set -euo pipefail; make CARGO_OSDK={shlex.quote(str(shared_cargo_home / 'bin' / 'cargo-osdk'))} kernel",
        workdir=hooks.docker_repo_dir(cfg),
    )


def sanitize_container_component(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-._")
    if not sanitized:
        return "run"
    return sanitized[:48]


def container_name_for_run(program_id: str, run_id: str) -> str:
    return f"syzabi-{sanitize_container_component(program_id)}-{sanitize_container_component(run_id)}"


def force_remove_container(container_name: str, *, hooks) -> None:
    hooks.subprocess.run(
        ["docker", "rm", "-f", container_name],
        text=True,
        capture_output=True,
        check=False,
    )


def docker_osdk_build_script(
    cfg: dict[str, object],
    work_dir: Path,
    initramfs_path: Path,
    *,
    hooks,
    kcmd_args: str = "console=hvc0",
) -> str:
    container_work_dir = hooks.host_path_to_container_path(work_dir, cfg)
    container_initramfs = hooks.host_path_to_container_path(initramfs_path, cfg)
    container_osdk_output = hooks.host_path_to_container_path(work_dir / "osdk-output", cfg)
    return "\n".join([
        "set -euo pipefail",
        f"mkdir -p {shlex.quote(str(container_work_dir))} {shlex.quote(str(container_osdk_output))}",
        f"cd {shlex.quote(str(hooks.docker_repo_dir(cfg) / 'kernel'))}",
        " ".join(shlex.quote(part) for part in hooks.osdk_build_command(container_initramfs, kcmd_args=kcmd_args)),
    ])


def ensure_packaged_docker_bundle(
    cfg: dict[str, object],
    package_dir: Path,
    initramfs_path: Path,
    *,
    hooks,
    kcmd_args: str,
) -> None:
    cargo_target_dir, osdk_output_dir = hooks.shared_package_runtime_dirs(package_dir)
    build_root = package_dir / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    lock_path = package_dir / ".osdk-build.lock"
    bundle_dir = hooks.shared_package_bundle_dir(package_dir)
    ready_stamp = package_dir / ".osdk-build.ready"
    metadata_path = hooks.packaged_bundle_metadata_path(package_dir)
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        expected_metadata = hooks.packaged_bundle_metadata(cfg, initramfs_path, kcmd_args=kcmd_args)
        if ready_stamp.exists() and (bundle_dir / "bundle.toml").exists() and hooks.packaged_bundle_metadata_matches(metadata_path, expected_metadata):
            return
        ready_stamp.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        hooks.prime_docker_cargo_cache(cfg)
        run_cargo_home = hooks.ensure_shared_package_cargo_home(package_dir, refresh=True)
        build_env = {
            "CARGO_HOME": str(hooks.host_path_to_container_path(run_cargo_home, cfg)),
            "CARGO_TARGET_DIR": str(hooks.host_path_to_container_path(cargo_target_dir, cfg)),
            "OSDK_OUTPUT_DIR": str(hooks.host_path_to_container_path(osdk_output_dir, cfg)),
            "CARGO_NET_OFFLINE": "true",
        }
        container_name = hooks.container_name_for_run("bundle", hooks.sha256_text(str(package_dir))[:12])
        hooks.force_remove_container(container_name)
        build_command = hooks.docker_run_command(
            cfg,
            hooks.docker_osdk_build_script(cfg, package_dir, initramfs_path, kcmd_args=kcmd_args),
            extra_env=build_env,
            workdir=hooks.docker_repo_dir(cfg),
            container_name=container_name,
        )
        try:
            completed = hooks.subprocess.run(
                build_command,
                cwd=build_root,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            hooks.force_remove_container(container_name)
        (build_root / "build.stdout.txt").write_text(completed.stdout, encoding="utf-8")
        (build_root / "build.stderr.txt").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            raise hooks.RunnerError(completed.stderr.strip() or completed.stdout.strip() or "failed to prebuild packaged docker bundle")
        hooks.dump_json(metadata_path, expected_metadata)
        ready_stamp.write_text("ready\n", encoding="utf-8")
