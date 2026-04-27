from __future__ import annotations

from typing import Any

from targets.base import BaseTargetAdapter


class LinuxTargetAdapter(BaseTargetAdapter):
    name = "linux"

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        target_cfg = cfg.get("target_config", {})
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "build_info_path": str(target_cfg.get("build_info_path", "")),
            "eligible_file": str(cfg.get("paths", {}).get("eligible_file", "")),
        }

    def runner_errors(self) -> tuple[type[Exception], ...]:
        return ()

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
