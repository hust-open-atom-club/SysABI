from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from targets.asterinas import api
from targets.asterinas.common import RunnerError

from . import initramfs


class AsterinasTargetAdapter:
    name = "asterinas"

    def compose_template_inputs(self, cfg: dict[str, Any]) -> dict[str, object]:
        preview_bytes = int(cfg["normalization"]["preview_bytes"])
        busybox_path = Path("/usr/bin/busybox")
        return {
            "compose_init": initramfs.compose_init(),
            "compose_init_hook": initramfs.compose_init_hook(),
            "compose_profile": initramfs.compose_profile(),
            "compose_packaged_autorun": initramfs.compose_packaged_autorun(preview_bytes),
            "busybox_path": str(busybox_path),
            "busybox_sha256": hashlib.sha256(busybox_path.read_bytes()).hexdigest(),
        }

    def packaged_candidate_env(self, package_dir: Path, slot: int) -> dict[str, str]:
        return {
            "SYZABI_ASTERINAS_PACKAGE_DIR": str(package_dir),
            "SYZABI_ASTERINAS_PACKAGE_SLOT": str(slot),
        }

    def prepare_target(self, *, cfg: dict[str, Any], mode: str) -> str:
        from targets.asterinas import build as build_mod

        if mode == "docker-qemu":
            return build_mod.ensure_docker_build(cfg, hooks=api)
        if mode in {"host-direct", "unconfigured"}:
            return build_mod.ensure_host_build(cfg, hooks=api)
        if mode == "local-proxy":
            return "local-proxy"
        raise RunnerError(f"unsupported Asterinas mode: {mode}")

    def healthcheck(self, args) -> None:
        if args.mode == "unconfigured":
            raise RunnerError("asterinas runner is not configured")
        if args.mode == "local-proxy":
            api.write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": "local-proxy"})
            return
        cfg = api.read_workflow_config()
        if args.mode == "docker-qemu":
            try:
                revision = self.prepare_target(cfg=cfg, mode="docker-qemu")
            except RunnerError as exc:
                if not api.should_fallback_to_host_direct(exc):
                    raise
                revision = self.prepare_target(cfg=cfg, mode="host-direct")
        else:
            revision = self.prepare_target(cfg=cfg, mode="host-direct")
        api.write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": f"asterinas@{revision[:12]}"})

    def run_batch(self, args) -> None:
        from targets.asterinas import runtime as runtime_mod

        if args.mode != "docker-qemu":
            raise RunnerError("batch manifest mode currently supports docker-qemu only")
        runtime_mod.docker_qemu_batch_run(args)

    def run_case(self, args) -> None:
        from targets.asterinas import runtime as runtime_mod

        if args.mode == "unconfigured":
            raise RunnerError("asterinas runner is not configured")
        if args.mode == "local-proxy":
            runtime_mod.local_proxy(args, hooks=api)
            return
        if args.mode == "docker-qemu":
            try:
                runtime_mod.docker_qemu_run(args, hooks=api)
            except RunnerError as exc:
                if not api.should_fallback_to_host_direct(exc):
                    raise
                runtime_mod.host_direct_run(args, hooks=api)
            return
        runtime_mod.host_direct_run(args, hooks=api)
