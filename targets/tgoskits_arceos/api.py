#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.common import config, configure_runtime, dump_json, resolve_repo_path
from orchestrator.vm_runner import extract_framed_events


class RunnerError(RuntimeError):
    pass


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode", default=os.environ.get("SYZABI_TGOSKITS_ARCEOS_MODE", "smoke-qemu"))
    parser.add_argument("--work-dir")
    return parser.parse_args()


def env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def runner_result_path() -> Path | None:
    return env_path("SYZABI_RUNNER_RESULT_PATH")


def write_runner_result(payload: dict[str, object]) -> None:
    path = runner_result_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    dump_json(path, payload)


def read_workflow_config() -> dict[str, Any]:
    workflow = os.environ.get("SYZABI_WORKFLOW", "tgoskits_arceos_smoke")
    configure_runtime(workflow=workflow)
    cfg = config()
    if cfg.get("target") != "tgoskits_arceos":
        raise RunnerError(f"tgoskits_arceos entrypoint requires a tgoskits_arceos workflow, got {cfg.get('workflow')}")
    return cfg


def target_config(cfg: dict[str, Any]) -> dict[str, Any]:
    payload = cfg.get("target_config")
    if not isinstance(payload, dict):
        raise RunnerError("tgoskits_arceos workflow is missing target_config")
    return payload


def repo_dir(cfg: dict[str, Any]) -> Path:
    env_name = str(target_config(cfg).get("repo_dir_env", "SYZABI_TGOSKITS_DIR"))
    selected = os.environ.get(env_name, str(target_config(cfg).get("repo_dir", "")))
    if not selected:
        raise RunnerError(f"missing TGOSKits workspace; set {env_name} or configure target_config.repo_dir")
    path = Path(selected).expanduser()
    if not path.exists():
        raise RunnerError(f"configured TGOSKits workspace does not exist: {path}")
    return path


def workspace_dir(cfg: dict[str, Any]) -> Path:
    relative = str(target_config(cfg).get("workspace_subdir", "os/arceos"))
    workspace = repo_dir(cfg) / relative
    if not workspace.exists():
        raise RunnerError(f"configured ArceOS workspace does not exist: {workspace}")
    return workspace


def require_feature_flag(cfg: dict[str, Any]) -> None:
    feature_flag_env = str(target_config(cfg).get("feature_flag_env", ""))
    if feature_flag_env and os.environ.get(feature_flag_env) != "1":
        raise RunnerError(f"set {feature_flag_env}=1 to enable TGOSKits external target workflows")


def shutil_which(tool: str) -> str | None:
    return subprocess.run(
        ["sh", "-lc", f"command -v {shlex.quote(tool)}"],
        check=False,
        text=True,
        capture_output=True,
    ).stdout.strip() or None


def ensure_toolchain_probes(cfg: dict[str, Any]) -> None:
    missing = [tool for tool in target_config(cfg).get("toolchain_probes", []) if shutil_which(str(tool)) is None]
    if missing:
        raise RunnerError(f"missing required ArceOS tools: {', '.join(missing)}")


def ensure_pinned_revision(cfg: dict[str, Any]) -> str:
    revision = str(target_config(cfg).get("revision", "")).strip()
    if not revision:
        raise RunnerError("target_config.revision must pin the TGOSKits checkout")
    result = subprocess.run(
        ["git", "-C", str(repo_dir(cfg)), "rev-parse", "HEAD"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RunnerError(result.stderr.strip() or result.stdout.strip() or "failed to read TGOSKits revision")
    current = result.stdout.strip()
    if current != revision:
        raise RunnerError(f"TGOSKits revision mismatch: expected {revision}, got {current}")
    return current


def target_triple(cfg: dict[str, Any]) -> str:
    configured = str(target_config(cfg).get("default_target", "")).strip()
    if configured:
        return configured
    supported = target_config(cfg).get("supported_targets", [])
    if isinstance(supported, list) and supported:
        return str(supported[0])
    raise RunnerError("target_config must provide default_target or supported_targets")


def preflight_payload(cfg: dict[str, Any]) -> dict[str, object]:
    require_feature_flag(cfg)
    revision = ensure_pinned_revision(cfg)
    ensure_toolchain_probes(cfg)
    workspace = workspace_dir(cfg)
    return {
        "target": "tgoskits_arceos",
        "workflow": str(cfg.get("workflow", "")),
        "arch": str(cfg.get("arch", "")),
        "repo_dir": str(repo_dir(cfg)),
        "workspace_dir": str(workspace),
        "revision": revision,
        "target_triple": target_triple(cfg),
        "mode": "experimental-c-app",
    }


def prepare_target(cfg: dict[str, Any]) -> str:
    payload = preflight_payload(cfg)
    for command in target_config(cfg).get("prepare_commands", []):
        completed = subprocess.run(command, cwd=str(repo_dir(cfg)), check=False, text=True, capture_output=True)
        if completed.returncode != 0:
            raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or "ArceOS prepare command failed")
    dump_json(
        resolve_repo_path(target_config(cfg)["build_info_path"]),
        {
            "target": "tgoskits_arceos",
            "revision": payload["revision"],
            "mode": payload["mode"],
            "target_triple": payload["target_triple"],
        },
    )
    return f"tgoskits-arceos@{str(payload['revision'])[:12]}"


def resolve_command(template: object, values: dict[str, str]) -> list[str]:
    if isinstance(template, str):
        return [token.format(**values) for token in shlex.split(template)]
    if isinstance(template, list):
        return [str(token).format(**values) for token in template]
    raise RunnerError(f"unsupported command template type: {type(template)!r}")


def healthcheck(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    payload = preflight_payload(cfg)
    label = prepare_target(cfg)
    values = {
        "arch": str(cfg.get("arch", "riscv64")),
        "target": str(payload["target_triple"]),
        "repo_dir": str(repo_dir(cfg)),
        "workspace_dir": str(workspace_dir(cfg)),
    }
    command = resolve_command(target_config(cfg).get("healthcheck_command", []), values)
    completed = subprocess.run(command, cwd=str(repo_dir(cfg)), check=False, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or "ArceOS smoke healthcheck failed")
    console_path = env_path("SYZABI_CONSOLE_LOG_PATH")
    if console_path is not None:
        console_path.parent.mkdir(parents=True, exist_ok=True)
        console_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": label})


def resolve_work_dir(args: argparse.Namespace) -> Path:
    selected = args.work_dir or os.environ.get("SYZABI_WORK_DIR")
    if selected:
        path = Path(selected)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="syzabi-arceos-"))


def instrumented_source_path(binary_path: Path) -> Path:
    sibling = binary_path.with_name("testcase.instrumented.c")
    if sibling.exists():
        return sibling
    if binary_path.suffix == ".c" and binary_path.exists():
        return binary_path
    raise RunnerError(f"missing instrumented testcase source beside {binary_path}")


def reject_unsupported_source(binary_path: Path) -> None:
    source = instrumented_source_path(binary_path).read_text(encoding="utf-8", errors="ignore")
    if '"openat"' in source:
        raise RunnerError("ArceOS experimental replay does not support openat testcases")


def materialize_app_tree(cfg: dict[str, Any], *, binary_path: Path, work_dir: Path) -> Path:
    app_dir = work_dir / "arceos-app"
    if app_dir.exists():
        shutil.rmtree(app_dir)
    include_dir = app_dir / "include" / "sys"
    include_dir.mkdir(parents=True, exist_ok=True)
    trace_source = resolve_repo_path("agent/arceos/trace.c")
    trace_header = resolve_repo_path("agent/arceos/trace.h")
    syscall_header = resolve_repo_path("agent/arceos/include/sys/syscall.h")
    shutil.copy2(instrumented_source_path(binary_path), app_dir / "main.c")
    shutil.copy2(trace_source, app_dir / "trace.c")
    shutil.copy2(trace_header, app_dir / "trace.h")
    shutil.copy2(syscall_header, include_dir / "syscall.h")
    (app_dir / "axbuild.mk").write_text(
        "APP_CFLAGS += -I$(APP) -I$(APP)/include\napp-objs := main.o trace.o\n",
        encoding="utf-8",
    )
    features = target_config(cfg).get("app_features", ["alloc", "fd", "fs"])
    feature_lines = [str(item).strip() for item in features if str(item).strip()]
    (app_dir / "features.txt").write_text("\n".join(feature_lines) + "\n", encoding="utf-8")
    return app_dir


def managed_cargo_config_path(cfg: dict[str, Any]) -> Path:
    return workspace_dir(cfg) / ".cargo" / "config.toml"


def managed_cargo_template_path(cfg: dict[str, Any]) -> Path:
    return repo_dir(cfg) / "scripts" / "arceos-c-test-cargo-config.template.toml"


def root_cargo_manifest_path(cfg: dict[str, Any]) -> Path:
    return repo_dir(cfg) / "Cargo.toml"


def managed_cargo_marker() -> str:
    return "# axbuild-managed: arceos-c-test-cargo-config"


def render_managed_cargo_config(cfg: dict[str, Any]) -> str:
    template = managed_cargo_template_path(cfg).read_text(encoding="utf-8")
    manifest = tomllib.loads(root_cargo_manifest_path(cfg).read_text(encoding="utf-8"))
    patches = manifest.get("patch", {}).get("crates-io", {})
    arceos_dir = workspace_dir(cfg)
    rendered = template if template.endswith("\n") else template + "\n"
    rendered += "[patch.crates-io]\n"
    for crate_name in sorted(patches):
        payload = patches[crate_name]
        if not isinstance(payload, dict):
            continue
        relative_path = payload.get("path")
        if not isinstance(relative_path, str) or not relative_path:
            continue
        absolute = repo_dir(cfg) / relative_path
        rel = os.path.relpath(absolute, arceos_dir)
        rendered += f'{crate_name} = {{ path = "{rel}" }}\n'
    return rendered


def write_managed_cargo_config(cfg: dict[str, Any]) -> tuple[Path, str | None]:
    config_path = managed_cargo_config_path(cfg)
    previous = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    if previous is not None and managed_cargo_marker() not in previous:
        raise RunnerError(f"refusing to overwrite user-managed cargo config at {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(render_managed_cargo_config(cfg), encoding="utf-8")
    return config_path, previous


def restore_managed_cargo_config(config_path: Path, previous: str | None) -> None:
    if previous is None:
        config_path.unlink(missing_ok=True)
        try:
            config_path.parent.rmdir()
        except OSError:
            pass
        return
    config_path.write_text(previous, encoding="utf-8")


def platform_config_path(cfg: dict[str, Any]) -> Path:
    configured = str(target_config(cfg).get("platform_config_path", "")).strip()
    if not configured:
        raise RunnerError("target_config.platform_config_path is required for ArceOS experimental replay")
    path = repo_dir(cfg) / configured
    if not path.exists():
        raise RunnerError(f"configured ArceOS platform config does not exist: {path}")
    return path


def disk_image_path(cfg: dict[str, Any]) -> Path:
    configured = str(target_config(cfg).get("disk_image_path", "")).strip()
    if not configured:
        raise RunnerError("target_config.disk_image_path is required for ArceOS experimental replay")
    return repo_dir(cfg) / configured


def run_disk_image_path(cfg: dict[str, Any], *, work_dir: Path) -> Path:
    return work_dir / disk_image_path(cfg).name


def ensure_disk_image(cfg: dict[str, Any], *, work_dir: Path, timeout_sec: int) -> Path:
    image = run_disk_image_path(cfg, work_dir=work_dir)
    image.parent.mkdir(parents=True, exist_ok=True)
    image.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            ["make", f"DISK_IMG={image}", "disk_img"],
            cwd=str(workspace_dir(cfg)),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"ArceOS disk image creation timed out after {timeout_sec}s") from exc
    if completed.returncode != 0 or not image.exists():
        raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or f"failed to create ArceOS disk image at {image}")
    return image


def _fat_u16(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 2], "little")


def _fat_u32(payload: bytes, offset: int) -> int:
    return int.from_bytes(payload[offset : offset + 4], "little")


def _fat_u12(payload: bytes, offset: int, cluster: int) -> int:
    value = int.from_bytes(payload[offset : offset + 2], "little")
    return (value >> 4) if (cluster & 1) else (value & 0x0FFF)


def _fat_type(image: bytes, *, bytes_per_sector: int, sectors_per_cluster: int, reserved_sectors: int, fat_count: int) -> tuple[str, int, int, int]:
    root_entry_count = _fat_u16(image, 17)
    total_sectors_16 = _fat_u16(image, 19)
    total_sectors_32 = _fat_u32(image, 32)
    sectors_per_fat_16 = _fat_u16(image, 22)
    sectors_per_fat_32 = _fat_u32(image, 36)
    total_sectors = total_sectors_16 if total_sectors_16 else total_sectors_32
    sectors_per_fat = sectors_per_fat_16 if sectors_per_fat_16 else sectors_per_fat_32
    root_dir_sectors = ((root_entry_count * 32) + (bytes_per_sector - 1)) // bytes_per_sector
    data_sectors = total_sectors - (reserved_sectors + fat_count * sectors_per_fat + root_dir_sectors)
    cluster_count = data_sectors // sectors_per_cluster if sectors_per_cluster else 0
    if cluster_count < 4085:
        return "FAT12", sectors_per_fat, root_entry_count, root_dir_sectors
    if cluster_count < 65525:
        return "FAT16", sectors_per_fat, root_entry_count, root_dir_sectors
    return "FAT32", sectors_per_fat, root_entry_count, root_dir_sectors


def _fat_entry(image: bytes, *, fat_offset: int, cluster: int, fat_type: str) -> int:
    if fat_type == "FAT12":
        return _fat_u12(image, fat_offset + (cluster * 3) // 2, cluster)
    if fat_type == "FAT16":
        return _fat_u16(image, fat_offset + cluster * 2)
    return _fat_u32(image, fat_offset + cluster * 4) & 0x0FFFFFFF


def _fat_chain(image: bytes, *, fat_offset: int, start_cluster: int, fat_type: str) -> list[int]:
    if start_cluster < 2:
        return []
    cluster = start_cluster
    seen: set[int] = set()
    chain: list[int] = []
    eof = {
        "FAT12": 0x0FF8,
        "FAT16": 0xFFF8,
        "FAT32": 0x0FFFFFF8,
    }[fat_type]
    while cluster >= 2 and cluster < eof and cluster not in seen:
        seen.add(cluster)
        chain.append(cluster)
        cluster = _fat_entry(image, fat_offset=fat_offset, cluster=cluster, fat_type=fat_type)
    return chain


def _fat_cluster_bytes(
    image: bytes,
    *,
    cluster: int,
    bytes_per_sector: int,
    sectors_per_cluster: int,
    first_data_sector: int,
) -> bytes:
    cluster_offset = (first_data_sector + (cluster - 2) * sectors_per_cluster) * bytes_per_sector
    cluster_size = bytes_per_sector * sectors_per_cluster
    return image[cluster_offset : cluster_offset + cluster_size]


def _fat_read_chain(
    image: bytes,
    *,
    start_cluster: int,
    fat_offset: int,
    fat_type: str,
    bytes_per_sector: int,
    sectors_per_cluster: int,
    first_data_sector: int,
) -> bytes:
    if start_cluster < 2:
        return b""
    return b"".join(
        _fat_cluster_bytes(
            image,
            cluster=cluster,
            bytes_per_sector=bytes_per_sector,
            sectors_per_cluster=sectors_per_cluster,
            first_data_sector=first_data_sector,
        )
        for cluster in _fat_chain(image, fat_offset=fat_offset, start_cluster=start_cluster, fat_type=fat_type)
    )


def _fat_decode_lfn_fragment(entry: bytes) -> str:
    raw = entry[1:11] + entry[14:26] + entry[28:32]
    chars: list[str] = []
    for index in range(0, len(raw), 2):
        codepoint = int.from_bytes(raw[index : index + 2], "little")
        if codepoint in {0x0000, 0xFFFF}:
            continue
        chars.append(chr(codepoint))
    return "".join(chars)


def _fat_short_name(entry: bytes) -> str:
    stem = entry[0:8].decode("ascii", errors="ignore").rstrip(" ")
    suffix = entry[8:11].decode("ascii", errors="ignore").rstrip(" ")
    return f"{stem}.{suffix}" if suffix else stem


def sample_fat_external_state(image_path: Path) -> dict[str, object]:
    try:
        image = image_path.read_bytes()
        if len(image) < 64:
            raise ValueError("disk image is too small to contain a FAT boot sector")
        bytes_per_sector = _fat_u16(image, 11)
        sectors_per_cluster = image[13]
        reserved_sectors = _fat_u16(image, 14)
        fat_count = image[16]
        fat_type, sectors_per_fat, root_entry_count, root_dir_sectors = _fat_type(
            image,
            bytes_per_sector=bytes_per_sector,
            sectors_per_cluster=sectors_per_cluster,
            reserved_sectors=reserved_sectors,
            fat_count=fat_count,
        )
        if fat_type not in {"FAT12", "FAT16", "FAT32"}:
            raise ValueError(f"unsupported filesystem type: {fat_type}")
        root_cluster = _fat_u32(image, 44) if fat_type == "FAT32" else 0
        fat_offset = reserved_sectors * bytes_per_sector
        first_root_dir_sector = reserved_sectors + fat_count * sectors_per_fat
        first_data_sector = first_root_dir_sector + root_dir_sectors
        files: list[dict[str, object]] = []

        root_directory = (
            image[first_root_dir_sector * bytes_per_sector : (first_root_dir_sector + root_dir_sectors) * bytes_per_sector]
            if fat_type != "FAT32"
            else None
        )

        def walk(prefix: str, cluster: int | None, directory_bytes: bytes | None = None) -> None:
            lfn_parts: list[str] = []
            if directory_bytes is None:
                if cluster is None:
                    return
                directory = _fat_read_chain(
                    image,
                    start_cluster=cluster,
                    fat_offset=fat_offset,
                    fat_type=fat_type,
                    bytes_per_sector=bytes_per_sector,
                    sectors_per_cluster=sectors_per_cluster,
                    first_data_sector=first_data_sector,
                )
            else:
                directory = directory_bytes
            for offset in range(0, len(directory), 32):
                entry = directory[offset : offset + 32]
                if len(entry) < 32:
                    break
                first = entry[0]
                if first == 0x00:
                    break
                if first == 0xE5:
                    lfn_parts = []
                    continue
                attributes = entry[11]
                if attributes == 0x0F:
                    lfn_parts.insert(0, _fat_decode_lfn_fragment(entry))
                    continue
                if attributes & 0x08:
                    lfn_parts = []
                    continue
                name = "".join(lfn_parts) if lfn_parts else _fat_short_name(entry)
                lfn_parts = []
                if name in {"", ".", ".."}:
                    continue
                if fat_type == "FAT32":
                    entry_cluster = (_fat_u16(entry, 20) << 16) | _fat_u16(entry, 26)
                else:
                    entry_cluster = _fat_u16(entry, 26)
                relative_path = f"{prefix}/{name}" if prefix else name
                if attributes & 0x10:
                    if entry_cluster >= 2:
                        walk(relative_path, entry_cluster)
                    continue
                size = _fat_u32(entry, 28)
                content = _fat_read_chain(
                    image,
                    start_cluster=entry_cluster,
                    fat_offset=fat_offset,
                    fat_type=fat_type,
                    bytes_per_sector=bytes_per_sector,
                    sectors_per_cluster=sectors_per_cluster,
                    first_data_sector=first_data_sector,
                )[:size]
                files.append(
                    {
                        "path": relative_path,
                        "size": size,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )

        if fat_type == "FAT32":
            walk("", root_cluster)
        else:
            walk("", None, root_directory)
        files.sort(key=lambda item: str(item["path"]))
        return {"files": files}
    except Exception as exc:
        return {"files": [], "read_error": str(exc)}


def diff_external_state(base_state: dict[str, object], current_state: dict[str, object]) -> dict[str, object]:
    if "read_error" in base_state or "read_error" in current_state:
        payload = {"files": list(current_state.get("files", []))}
        if "read_error" in current_state:
            payload["read_error"] = current_state["read_error"]
        return payload
    base_by_path = {
        str(item["path"]): item
        for item in base_state.get("files", [])
        if isinstance(item, dict) and "path" in item
    }
    changed: list[dict[str, object]] = []
    for raw_item in current_state.get("files", []):
        if not isinstance(raw_item, dict):
            continue
        path = str(raw_item.get("path", ""))
        previous = base_by_path.get(path)
        if previous is None or previous.get("sha256") != raw_item.get("sha256") or previous.get("size") != raw_item.get("size"):
            changed.append(raw_item)
    changed.sort(key=lambda item: str(item.get("path", "")))
    return {"files": changed}


def run_case_make_args(cfg: dict[str, Any], *, app_dir: Path, disk_image: Path) -> list[str]:
    arch = str(cfg.get("arch", "riscv64"))
    feature_string = ",".join(line.strip() for line in (app_dir / "features.txt").read_text(encoding="utf-8").splitlines() if line.strip())
    return [
        f"A={app_dir}",
        f"ARCH={arch}",
        f"PLAT_CONFIG={platform_config_path(cfg)}",
        f"FEATURES={feature_string}",
        "BLK=y",
        f"DISK_IMG={disk_image}",
        "ACCEL=n",
        "LOG=warn",
    ]


def write_case_outputs(
    *,
    program_id: str,
    run_id: str,
    console_text: str,
    exit_code: int,
    kernel_build: str,
    raw_trace_path: Path,
    external_state_path: Path,
    runner_result_path_value: Path,
    console_path: Path | None,
    disk_image: Path,
    initial_external_state: dict[str, object],
) -> None:
    normalized_console = ANSI_ESCAPE_RE.sub("", console_text).replace("\x00", "")
    events = extract_framed_events(normalized_console)
    guest_exit_code = exit_code
    if events:
        last_event = events[-1]
        if str(last_event.get("syscall_name", "")) in {"exit", "exit_group"}:
            args = last_event.get("args", [])
            if isinstance(args, list) and args:
                guest_exit_code = int(args[0])
    dump_json(external_state_path, diff_external_state(initial_external_state, sample_fat_external_state(disk_image)))
    if not events:
        dump_json(
            runner_result_path_value,
            {
                "status": "infra_error",
                "exit_code": exit_code,
                "detail": "missing trace markers in ArceOS command output",
                "kernel_build": kernel_build,
            },
        )
        if console_path is not None:
            console_path.parent.mkdir(parents=True, exist_ok=True)
            console_path.write_text(console_text, encoding="utf-8")
        return
    raw_trace = {
        "program_id": program_id,
        "side": "candidate",
        "run_id": run_id,
        "status": "ok",
        "events": events,
        "process_exit": {
            "status": "ok",
            "exit_code": guest_exit_code,
            "timed_out": False,
        },
    }
    dump_json(raw_trace_path, raw_trace)
    dump_json(
        runner_result_path_value,
        {
            "status": "ok",
            "exit_code": guest_exit_code,
            "kernel_build": kernel_build,
        },
    )
    if console_path is not None:
        console_path.parent.mkdir(parents=True, exist_ok=True)
        console_path.write_text(console_text, encoding="utf-8")


def write_infra_error(
    *,
    runner_result_path_value: Path,
    console_path: Path | None,
    console_text: str,
    detail: str,
    kernel_build: str,
) -> None:
    dump_json(
        runner_result_path_value,
        {
            "status": "infra_error",
            "exit_code": None,
            "detail": detail,
            "kernel_build": kernel_build,
        },
    )
    if console_path is not None:
        console_path.parent.mkdir(parents=True, exist_ok=True)
        console_path.write_text(console_text, encoding="utf-8")


def run_case(args: argparse.Namespace) -> None:
    cfg = read_workflow_config()
    label = prepare_target(cfg)
    if not args.binary:
        raise RunnerError("missing candidate binary path for ArceOS experimental replay")
    binary_path = Path(args.binary)
    if not binary_path.exists():
        raise RunnerError(f"candidate binary path does not exist: {binary_path}")
    reject_unsupported_source(binary_path)
    raw_trace_path = env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = env_path("SYZABI_EXTERNAL_STATE_PATH")
    runner_result = runner_result_path()
    if raw_trace_path is None or external_state_path is None or runner_result is None:
        raise RunnerError("missing SysABI output paths for ArceOS candidate run")
    console_path = env_path("SYZABI_CONSOLE_LOG_PATH")
    work_dir = resolve_work_dir(args)
    app_dir = materialize_app_tree(cfg, binary_path=binary_path, work_dir=work_dir)
    timeout_sec = int(target_config(cfg).get("command_timeout_sec", 300))
    console_text = ""
    try:
        disk_image = ensure_disk_image(cfg, work_dir=work_dir, timeout_sec=timeout_sec)
    except RunnerError as exc:
        write_infra_error(
            runner_result_path_value=runner_result,
            console_path=console_path,
            console_text=console_text,
            detail=str(exc),
            kernel_build=label,
        )
        raise
    initial_external_state = sample_fat_external_state(disk_image)
    config_path: Path | None = None
    previous_config: str | None = None
    try:
        config_path, previous_config = write_managed_cargo_config(cfg)
        make_args = run_case_make_args(cfg, app_dir=app_dir, disk_image=disk_image)
        try:
            completed_defconfig = subprocess.run(
                ["make", *make_args, "defconfig"],
                cwd=str(workspace_dir(cfg)),
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            console_text = (exc.stdout or "") + (exc.stderr or "")
            detail = f"ArceOS defconfig timed out after {timeout_sec}s"
            write_infra_error(
                runner_result_path_value=runner_result,
                console_path=console_path,
                console_text=console_text,
                detail=detail,
                kernel_build=label,
            )
            raise RunnerError(detail) from exc
        console_text = completed_defconfig.stdout + completed_defconfig.stderr
        if completed_defconfig.returncode != 0:
            if console_path is not None:
                console_path.parent.mkdir(parents=True, exist_ok=True)
                console_path.write_text(console_text, encoding="utf-8")
            raise RunnerError(completed_defconfig.stderr.strip() or completed_defconfig.stdout.strip() or "ArceOS defconfig failed")
        try:
            completed_run = subprocess.run(
                ["make", *make_args, "run"],
                cwd=str(workspace_dir(cfg)),
                check=False,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            console_text += (exc.stdout or "") + (exc.stderr or "")
            detail = f"ArceOS run timed out after {timeout_sec}s"
            write_infra_error(
                runner_result_path_value=runner_result,
                console_path=console_path,
                console_text=console_text,
                detail=detail,
                kernel_build=label,
            )
            raise RunnerError(detail) from exc
        console_text += completed_run.stdout + completed_run.stderr
        if completed_run.returncode != 0:
            if console_path is not None:
                console_path.parent.mkdir(parents=True, exist_ok=True)
                console_path.write_text(console_text, encoding="utf-8")
            raise RunnerError(completed_run.stderr.strip() or completed_run.stdout.strip() or "ArceOS experimental replay failed")
    finally:
        if config_path is not None:
            restore_managed_cargo_config(config_path, previous_config)
    write_case_outputs(
        program_id=os.environ.get("SYZABI_PROGRAM_ID", binary_path.parent.name),
        run_id=os.environ.get("SYZABI_RUN_ID", "arceos-run"),
        console_text=console_text,
        exit_code=0,
        kernel_build=label,
        raw_trace_path=raw_trace_path,
        external_state_path=external_state_path,
        runner_result_path_value=runner_result,
        console_path=console_path,
        disk_image=disk_image,
        initial_external_state=initial_external_state,
    )


def run_batch(args: argparse.Namespace) -> None:
    raise RunnerError("ArceOS experimental replay does not support batch execution")


def main() -> None:
    args = parse_args()
    try:
        if args.healthcheck:
            healthcheck(args)
            return
        run_case(args)
    except RunnerError as exc:
        write_runner_result({"status": "infra_error", "exit_code": None, "detail": str(exc), "kernel_build": "unknown"})
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
