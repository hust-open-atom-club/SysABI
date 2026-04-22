from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable, Any

from core.capabilities import CapabilitySet


SINGLE_COMMAND_EXECUTION_MODE = "single_command"
PACKAGED_PER_CASE_EXECUTION_MODE = "packaged_per_case"
SHARED_RUNTIME_BATCH_EXECUTION_MODE = "shared_runtime_batch"
LEGACY_SHARED_GUEST_SHELL_EXECUTION_MODE = "shared_guest_shell"


def canonical_execution_mode(mode: str | None) -> str | None:
    if mode == LEGACY_SHARED_GUEST_SHELL_EXECUTION_MODE:
        return SHARED_RUNTIME_BATCH_EXECUTION_MODE
    return mode


@runtime_checkable
class TargetAdapter(Protocol):
    name: str

    def capabilities(self, cfg: dict[str, Any]) -> CapabilitySet:
        ...

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        ...

    def requires_campaign_healthcheck(self, cfg: dict[str, Any]) -> bool:
        ...

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        ...

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        ...

    def prepare_case(self, entry: dict[str, object], cfg: dict[str, Any]) -> dict[str, object]:
        ...

    def prepare_batch(self, cases: list[dict[str, object]], cfg: dict[str, Any]) -> dict[str, object] | None:
        ...

    def collect_result(self, result: object, cfg: dict[str, Any]) -> dict[str, object]:
        ...

    def finalize_result(self, result: dict[str, object], cfg: dict[str, Any]) -> dict[str, object]:
        ...

    def prepare_case_package_payload(
        self,
        cases: list[dict[str, object]],
        cfg: dict[str, Any],
        batch_metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        ...

    def prepare_batch_manifest_payload(
        self,
        cases: list[dict[str, object]],
        cfg: dict[str, Any],
        batch_metadata: dict[str, object] | None,
    ) -> dict[str, object] | None:
        ...

    def compose_template_inputs(self, cfg: dict[str, Any]) -> dict[str, object]:
        ...

    def packaged_candidate_env(self, package_dir: Path, slot: int) -> dict[str, str]:
        ...

    def prewarm_candidate_batch(
        self,
        *,
        prepared_cases: list[dict[str, object]],
        package_dir: Path,
        cfg: dict[str, Any],
    ) -> None:
        ...

    def prepare_target(self, **kwargs: Any) -> object:
        ...

    def healthcheck(self, *args: Any, **kwargs: Any) -> object:
        ...

    def run_case(self, *args: Any, **kwargs: Any) -> object:
        ...

    def run_batch(self, *args: Any, **kwargs: Any) -> object:
        ...
