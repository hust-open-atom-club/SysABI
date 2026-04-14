from __future__ import annotations

from pathlib import Path
from typing import Any

from core.capabilities import CapabilitySet, capabilities_from_config
from targets.base import SINGLE_COMMAND_EXECUTION_MODE


class LinuxTargetAdapter:
    name = "linux"

    def capabilities(self, cfg: dict[str, Any]) -> CapabilitySet:
        return capabilities_from_config(cfg)

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (SINGLE_COMMAND_EXECUTION_MODE,)

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "arch": str(cfg.get("arch", "")),
            "target_os": str(cfg.get("target_os", "")),
            "supported_execution_modes": list(self.execution_modes(cfg)),
        }

    def prepare_campaign_assets(self, cfg: dict[str, Any]) -> dict[str, object]:
        target_cfg = cfg.get("target_config", {})
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "build_info_path": str(target_cfg.get("build_info_path", "")),
            "eligible_file": str(cfg.get("paths", {}).get("eligible_file", "")),
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
            "execution_mode": SINGLE_COMMAND_EXECUTION_MODE,
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
        return {}

    def packaged_candidate_env(self, package_dir: Path, slot: int) -> dict[str, str]:
        return {}

    def prewarm_candidate_batch(
        self,
        *,
        prepared_cases: list[dict[str, object]],
        package_dir: Path,
        cfg: dict[str, Any],
    ) -> None:
        return None

    def prepare_target(self, **kwargs: Any) -> object:
        return None

    def healthcheck(self, *args: Any, **kwargs: Any) -> object:
        return None

    def run_case(self, *args: Any, **kwargs: Any) -> object:
        raise NotImplementedError("linux target does not provide an external runner entrypoint")

    def run_batch(self, *args: Any, **kwargs: Any) -> object:
        raise NotImplementedError("linux target does not provide an external batch runner")


def build_target_adapter() -> LinuxTargetAdapter:
    return LinuxTargetAdapter()
