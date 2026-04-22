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

    def requires_campaign_healthcheck(self, cfg: dict[str, Any]) -> bool:
        return True

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
            "cases": list(cases),
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

    def prepare_case_package_payload(
        self,
        cases: list[dict[str, object]],
        cfg: dict[str, Any],
        batch_metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        return None

    def prepare_batch_manifest_payload(
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
                    "run_id": str(case.get("run_id", "")),
                    "binary_path": str(case.get("binary_path", "")),
                    "stdout_path": str(case.get("stdout_path", "")),
                    "stderr_path": str(case.get("stderr_path", "")),
                    "console_path": str(case.get("console_path", "")),
                    "events_path": str(case.get("events_path", "")),
                    "raw_trace_path": str(case.get("raw_trace_path", "")),
                    "external_state_path": str(case.get("external_state_path", "")),
                    "runner_result_path": str(case.get("runner_result_path", "")),
                }
                for case in cases
            ],
        }

    def case_package_id(self, payload: dict[str, object]) -> str:
        from targets.base import case_package_id as _case_package_id
        return _case_package_id(payload)

    def batch_manifest_id(self, payload: dict[str, object]) -> str:
        from targets.base import batch_manifest_id as _batch_manifest_id
        return _batch_manifest_id(payload)

    def runner_errors(self) -> tuple[type[Exception], ...]:
        from targets.tgoskits_starryos.api import RunnerError
        return (RunnerError,)

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
