from __future__ import annotations

from pathlib import Path
from typing import Any

from targets.tgoskits_starryos import api


class TGOSKitsStarryOSTargetAdapter:
    name = "tgoskits_starryos"

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
        raise api.RunnerError("tgoskits_starryos does not implement batch execution yet")


def build_target_adapter() -> TGOSKitsStarryOSTargetAdapter:
    return TGOSKitsStarryOSTargetAdapter()
