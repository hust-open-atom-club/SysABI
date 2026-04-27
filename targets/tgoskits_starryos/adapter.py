from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from targets.base import BaseTargetAdapter, PACKAGED_PER_CASE_EXECUTION_MODE
from targets.tgoskits_starryos import api


class TGOSKitsStarryOSTargetAdapter(BaseTargetAdapter):
    name = "tgoskits_starryos"

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (PACKAGED_PER_CASE_EXECUTION_MODE,)

    def requires_campaign_healthcheck(self, cfg: dict[str, Any]) -> bool:
        return True

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return api.preflight_payload(cfg)

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        return api.preflight_payload(cfg)

    def prepare_case_package_payload(
        self,
        cases: list[dict[str, object]],
        cfg: dict[str, Any],
        batch_metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        return {
            "workflow": str(cfg.get("workflow", "")),
            "target": str(cfg.get("target", "")),
            "arch": str(cfg.get("arch", "")),
            "preview_bytes": int(cfg["normalization"]["preview_bytes"]),
            "batch_metadata": batch_metadata or {},
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
        return (api.RunnerError,)

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
