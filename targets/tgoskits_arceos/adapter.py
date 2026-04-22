from __future__ import annotations

from pathlib import Path
from typing import Any

from core.capabilities import CapabilitySet, capabilities_from_config
from targets.base import SINGLE_COMMAND_EXECUTION_MODE
from targets.tgoskits_arceos import api


class TGOSKitsArceOSTargetAdapter:
    name = "tgoskits_arceos"

    def capabilities(self, cfg: dict[str, Any]) -> CapabilitySet:
        return capabilities_from_config(cfg)

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (SINGLE_COMMAND_EXECUTION_MODE,)

    def requires_campaign_healthcheck(self, cfg: dict[str, Any]) -> bool:
        return False

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return api.preflight_payload(cfg)

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        if args is not None and (getattr(args, "limit", None) != 1 or getattr(args, "jobs", None) != 1):
            raise api.RunnerError("ArceOS experimental campaign is single-case only; use `--limit 1 --jobs 1`.")
        return api.replay_preflight_payload(cfg)

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
        return None

    def case_package_id(self, payload: dict[str, object]) -> str:
        from targets.base import case_package_id as _case_package_id
        return _case_package_id(payload)

    def batch_manifest_id(self, payload: dict[str, object]) -> str:
        from targets.base import batch_manifest_id as _batch_manifest_id
        return _batch_manifest_id(payload)

    def runner_errors(self) -> tuple[type[Exception], ...]:
        from targets.tgoskits_arceos.api import RunnerError
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


def build_target_adapter() -> TGOSKitsArceOSTargetAdapter:
    return TGOSKitsArceOSTargetAdapter()
