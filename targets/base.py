from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol, runtime_checkable, Any

from core.capabilities import CapabilitySet, capabilities_from_config
from core.constants import ExecutionMode


SINGLE_COMMAND_EXECUTION_MODE = ExecutionMode.SINGLE_COMMAND
PACKAGED_PER_CASE_EXECUTION_MODE = ExecutionMode.PACKAGED_PER_CASE
SHARED_RUNTIME_BATCH_EXECUTION_MODE = ExecutionMode.SHARED_RUNTIME_BATCH
LEGACY_SHARED_GUEST_SHELL_EXECUTION_MODE = "shared_guest_shell"


def canonical_execution_mode(mode: str | None) -> str | None:
    if mode == LEGACY_SHARED_GUEST_SHELL_EXECUTION_MODE:
        return SHARED_RUNTIME_BATCH_EXECUTION_MODE
    return mode


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json_sha256(payload: dict[str, object]) -> str:
    return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


case_package_id = canonical_json_sha256
batch_manifest_id = canonical_json_sha256


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

    def case_package_id(self, payload: dict[str, object]) -> str:
        ...

    def batch_manifest_id(self, payload: dict[str, object]) -> str:
        ...

    def runner_errors(self) -> tuple[type[Exception], ...]:
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


class BaseTargetAdapter:
    """Base class providing sensible defaults for all TargetAdapter methods.

    Subclasses should override only the methods they actually need to
    customize. This eliminates the boilerplate copy-paste seen in most
    adapter implementations.
    """

    name = ""

    def capabilities(self, cfg: dict[str, Any]) -> CapabilitySet:
        return capabilities_from_config(cfg)

    def execution_modes(self, cfg: dict[str, Any]) -> tuple[str, ...]:
        return (SINGLE_COMMAND_EXECUTION_MODE,)

    def requires_campaign_healthcheck(self, cfg: dict[str, Any]) -> bool:
        return False

    def preflight_payload(self, cfg: dict[str, Any]) -> dict[str, object]:
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "arch": str(cfg.get("arch", "")),
            "target_os": str(cfg.get("target_os", "")),
            "supports_preflight": self.capabilities(cfg).supports_preflight,
            "supported_execution_modes": list(self.execution_modes(cfg)),
        }

    def prepare_campaign_assets(self, cfg: dict[str, Any], args: Any | None = None) -> dict[str, object]:
        return {"target": self.name, "workflow": str(cfg.get("workflow", ""))}

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
        modes = self.execution_modes(cfg)
        primary_mode = modes[0] if modes else SINGLE_COMMAND_EXECUTION_MODE
        return {
            "target": self.name,
            "workflow": str(cfg.get("workflow", "")),
            "execution_mode": primary_mode,
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
        return case_package_id(payload)

    def batch_manifest_id(self, payload: dict[str, object]) -> str:
        return batch_manifest_id(payload)

    def runner_errors(self) -> tuple[type[Exception], ...]:
        return (Exception,)

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
        pass

    def prepare_target(self, **kwargs: Any) -> object:
        raise NotImplementedError

    def healthcheck(self, *args: Any, **kwargs: Any) -> object:
        raise NotImplementedError

    def run_case(self, *args: Any, **kwargs: Any) -> object:
        raise NotImplementedError

    def run_batch(self, *args: Any, **kwargs: Any) -> object:
        raise NotImplementedError
