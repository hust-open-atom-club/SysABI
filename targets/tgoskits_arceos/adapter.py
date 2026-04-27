from __future__ import annotations

from typing import Any

from targets.base import BaseTargetAdapter
from targets.tgoskits_arceos import api


class TGOSKitsArceOSTargetAdapter(BaseTargetAdapter):
    name = "tgoskits_arceos"

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return api.preflight_payload(cfg)

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        return api.replay_preflight_payload(cfg)

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


def build_target_adapter() -> TGOSKitsArceOSTargetAdapter:
    return TGOSKitsArceOSTargetAdapter()
