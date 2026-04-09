from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

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
        from targets.asterinas import runner_impl

        if mode == "docker-qemu":
            return runner_impl.ensure_docker_build(cfg)
        if mode in {"host-direct", "unconfigured"}:
            return runner_impl.ensure_host_build(cfg)
        if mode == "local-proxy":
            return "local-proxy"
        raise runner_impl.RunnerError(f"unsupported Asterinas mode: {mode}")

    def healthcheck(self, args) -> None:
        from targets.asterinas import runner_impl

        if args.mode == "unconfigured":
            raise runner_impl.RunnerError("asterinas runner is not configured")
        if args.mode == "local-proxy":
            runner_impl.write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": "local-proxy"})
            return
        cfg = runner_impl.read_workflow_config()
        if args.mode == "docker-qemu":
            try:
                revision = self.prepare_target(cfg=cfg, mode="docker-qemu")
            except runner_impl.RunnerError as exc:
                if not runner_impl.should_fallback_to_host_direct(exc):
                    raise
                revision = self.prepare_target(cfg=cfg, mode="host-direct")
        else:
            revision = self.prepare_target(cfg=cfg, mode="host-direct")
        runner_impl.write_runner_result({"status": "ok", "exit_code": 0, "kernel_build": f"asterinas@{revision[:12]}"})

    def run_batch(self, args) -> None:
        from targets.asterinas import runner_impl

        if args.mode != "docker-qemu":
            raise runner_impl.RunnerError("batch manifest mode currently supports docker-qemu only")
        runner_impl.docker_qemu_batch_run(args)

    def run_case(self, args) -> None:
        from targets.asterinas import runner_impl

        if args.mode == "unconfigured":
            raise runner_impl.RunnerError("asterinas runner is not configured")
        if args.mode == "local-proxy":
            runner_impl.local_proxy(args)
            return
        if args.mode == "docker-qemu":
            try:
                runner_impl.docker_qemu_run(args)
            except runner_impl.RunnerError as exc:
                if not runner_impl.should_fallback_to_host_direct(exc):
                    raise
                runner_impl.host_direct_run(args)
            return
        runner_impl.host_direct_run(args)
