from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from orchestrator.common import runner_profiles
from targets.base import BaseTargetAdapter, PACKAGED_PER_CASE_EXECUTION_MODE
from targets.asterinas import api
from targets.asterinas.common import RunnerError

from . import initramfs


class AsterinasTargetAdapter(BaseTargetAdapter):
    name = "asterinas"

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (PACKAGED_PER_CASE_EXECUTION_MODE,)

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        paths = cfg.get("paths", {})
        target_cfg = cfg.get("target_config", {})
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "build_info_path": str(target_cfg.get("build_info_path", "")),
            "candidate_initramfs_packages_dir": str(paths.get("candidate_initramfs_packages_dir", "")),
        }

    def prepare_case_package_payload(
        self,
        cases: list[dict[str, object]],
        cfg: dict[str, Any],
        batch_metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        template_inputs = self.compose_template_inputs(cfg)
        return {
            "workflow": str(cfg.get("workflow", "")),
            "preview_bytes": int(cfg["normalization"]["preview_bytes"]),
            "template_inputs": template_inputs,
            "cases": [
                {
                    "program_id": str(case.get("program_id", "")),
                    "binary_sha256": hashlib.sha256(
                        Path(str(case["binary_path"])).read_bytes()
                    ).hexdigest(),
                }
                for case in cases
            ],
        }

    def runner_errors(self) -> tuple[type[Exception], ...]:
        return (RunnerError,)

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

    def prewarm_candidate_batch(
        self,
        *,
        prepared_cases: list[dict[str, object]],
        package_dir: Path,
        cfg: dict[str, object],
    ) -> None:
        if not prepared_cases:
            return
        profile = runner_profiles()["candidate"]
        if profile.get("kind") != "command":
            return
        first_case = prepared_cases[0]
        binary_path = Path(str(first_case["binary_path"])).resolve()
        sandbox_root = Path(str(first_case["sandbox_root"])).resolve()
        custom_initramfs = api.selected_initramfs(cfg, binary_path, sandbox_root)
        guest_kcmd_args = " ".join(part for part in ("console=hvc0", api.selected_guest_cmdline_append()) if part)
        api.ensure_packaged_docker_bundle(cfg, package_dir, custom_initramfs, kcmd_args=guest_kcmd_args)

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
        revision = self.prepare_target(cfg=cfg, mode=args.mode)
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
            runtime_mod.docker_qemu_run(args, hooks=api)
            return
        runtime_mod.host_direct_run(args, hooks=api)


def build_target_adapter() -> AsterinasTargetAdapter:
    return AsterinasTargetAdapter()
