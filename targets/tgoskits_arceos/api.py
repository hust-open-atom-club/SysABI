#!/usr/bin/env python3
from __future__ import annotations

import argparse
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


def ensure_disk_image(cfg: dict[str, Any], *, timeout_sec: int) -> Path:
    image = disk_image_path(cfg)
    if image.exists():
        return image
    completed = subprocess.run(
        ["make", f"DISK_IMG={image}", "disk_img"],
        cwd=str(workspace_dir(cfg)),
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    if completed.returncode != 0 or not image.exists():
        raise RunnerError(completed.stderr.strip() or completed.stdout.strip() or f"failed to create ArceOS disk image at {image}")
    return image


def run_case_make_args(cfg: dict[str, Any], *, app_dir: Path) -> list[str]:
    arch = str(cfg.get("arch", "riscv64"))
    feature_string = ",".join(line.strip() for line in (app_dir / "features.txt").read_text(encoding="utf-8").splitlines() if line.strip())
    image = disk_image_path(cfg)
    return [
        f"A={app_dir}",
        f"ARCH={arch}",
        f"PLAT_CONFIG={platform_config_path(cfg)}",
        f"FEATURES={feature_string}",
        "BLK=y",
        f"DISK_IMG={image}",
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
) -> None:
    normalized_console = ANSI_ESCAPE_RE.sub("", console_text).replace("\x00", "")
    events = extract_framed_events(normalized_console)
    dump_json(external_state_path, {"files": []})
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
            "exit_code": exit_code,
            "timed_out": False,
        },
    }
    dump_json(raw_trace_path, raw_trace)
    dump_json(
        runner_result_path_value,
        {
            "status": "ok",
            "exit_code": exit_code,
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
    raw_trace_path = env_path("SYZABI_RAW_TRACE_PATH")
    external_state_path = env_path("SYZABI_EXTERNAL_STATE_PATH")
    runner_result = runner_result_path()
    if raw_trace_path is None or external_state_path is None or runner_result is None:
        raise RunnerError("missing SysABI output paths for ArceOS candidate run")
    console_path = env_path("SYZABI_CONSOLE_LOG_PATH")
    work_dir = resolve_work_dir(args)
    app_dir = materialize_app_tree(cfg, binary_path=binary_path, work_dir=work_dir)
    timeout_sec = int(target_config(cfg).get("command_timeout_sec", 300))
    ensure_disk_image(cfg, timeout_sec=timeout_sec)
    config_path, previous_config = write_managed_cargo_config(cfg)
    make_args = run_case_make_args(cfg, app_dir=app_dir)
    try:
        completed_defconfig = subprocess.run(
            ["make", *make_args, "defconfig"],
            cwd=str(workspace_dir(cfg)),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
        console_text = completed_defconfig.stdout + completed_defconfig.stderr
        if completed_defconfig.returncode != 0:
            if console_path is not None:
                console_path.parent.mkdir(parents=True, exist_ok=True)
                console_path.write_text(console_text, encoding="utf-8")
            raise RunnerError(completed_defconfig.stderr.strip() or completed_defconfig.stdout.strip() or "ArceOS defconfig failed")
        completed_run = subprocess.run(
            ["make", *make_args, "run"],
            cwd=str(workspace_dir(cfg)),
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
        console_text += completed_run.stdout + completed_run.stderr
        if completed_run.returncode != 0:
            if console_path is not None:
                console_path.parent.mkdir(parents=True, exist_ok=True)
                console_path.write_text(console_text, encoding="utf-8")
            raise RunnerError(completed_run.stderr.strip() or completed_run.stdout.strip() or "ArceOS experimental replay failed")
    finally:
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
