from __future__ import annotations

from pathlib import Path
from typing import Any


class LinuxTargetAdapter:
    name = "linux"

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
