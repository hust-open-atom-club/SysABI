from __future__ import annotations

from pathlib import Path
from typing import Any

from core.capabilities import CapabilitySet, capabilities_from_config
from targets.base import SHARED_RUNTIME_BATCH_EXECUTION_MODE
from targets.tgoskits_starryos import api


class TGOSKitsStarryOSTargetAdapter:
    name = "tgoskits_starryos"

    def capabilities(self, cfg: dict[str, Any]) -> CapabilitySet:
        return capabilities_from_config(cfg)

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (SHARED_RUNTIME_BATCH_EXECUTION_MODE,)

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return api.preflight_payload(cfg)

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        return api.preflight_payload(cfg)

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
            "execution_mode": SHARED_RUNTIME_BATCH_EXECUTION_MODE,
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
        cfg: dict[str, object],
    ) -> None:
        return None

    def prepare_target(self, *, cfg: dict[str, Any]) -> str:
        return api.prepare_target(cfg)

    def healthcheck(self, args) -> None:
        api.healthcheck(args)

    def run_case(self, args) -> None:
        api.run_case(args)

    def run_batch(self, args) -> None:
        api.run_batch(args)


def build_target_adapter() -> TGOSKitsStarryOSTargetAdapter:
    return TGOSKitsStarryOSTargetAdapter()
