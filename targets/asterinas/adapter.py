from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from core.capabilities import CapabilitySet, capabilities_from_config
from targets.base import PACKAGED_PER_CASE_EXECUTION_MODE
from targets.asterinas import api
from targets.asterinas.common import RunnerError
from orchestrator.common import runner_profiles

from . import initramfs


class AsterinasTargetAdapter:
    name = "asterinas"

    def capabilities(self, cfg: dict[str, Any]) -> CapabilitySet:
        return capabilities_from_config(cfg)

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (PACKAGED_PER_CASE_EXECUTION_MODE,)

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "arch": str(cfg.get("arch", "")),
            "target_os": str(cfg.get("target_os", "")),
            "supports_preflight": self.capabilities(cfg).supports_preflight,
            "supported_execution_modes": list(self.execution_modes(cfg)),
        }

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        paths = cfg.get("paths", {})
        target_cfg = cfg.get("target_config", {})
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "build_info_path": str(target_cfg.get("build_info_path", "")),
            "candidate_initramfs_packages_dir": str(paths.get("candidate_initramfs_packages_dir", "")),
        }

    def prepare_case(self, entry: dict[str, object], cfg: dict[str, Any]) -> dict[str, object]:
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "program_id": str(entry.get("program_id", "")),
            "binary_path": str(entry.get("binary_path", "")),
        }

    def prepare_batch(self, cases: list[dict[str, object]], cfg: dict[str, Any]) -> dict[str, object] | None:
        if not self.capabilities(cfg).supports_batch_execution:
            return None
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "execution_mode": PACKAGED_PER_CASE_EXECUTION_MODE,
            "case_count": len(cases),
            "program_ids": [str(case.get("program_id", "")) for case in cases],
        }

    def collect_result(self, result: object, cfg: dict[str, Any]) -> dict[str, object]:
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "result": result,
        }

    def finalize_result(self, result: dict[str, object], cfg: dict[str, Any]) -> dict[str, object]:
        finalized = dict(result)
        finalized["finalized"] = True
        return finalized

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
