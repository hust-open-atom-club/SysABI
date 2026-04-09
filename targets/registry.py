from __future__ import annotations

from pathlib import Path
from typing import Any

from targets.asterinas.adapter import AsterinasTargetAdapter


class GenericTargetAdapter:
    name = "generic"

    def compose_template_inputs(self, cfg: dict[str, Any]) -> dict[str, object]:
        raise NotImplementedError("generic target has no packaged initramfs template inputs")

    def packaged_candidate_env(self, package_dir: Path, slot: int) -> dict[str, str]:
        return {}


def active_target_name(cfg: dict[str, Any]) -> str:
    target = cfg.get("target")
    if isinstance(target, str) and target:
        return target
    workflow = str(cfg.get("workflow", "baseline"))
    if workflow.startswith("asterinas"):
        return "asterinas"
    return "linux"


def get_target_adapter(cfg: dict[str, Any]) -> GenericTargetAdapter | AsterinasTargetAdapter:
    target = active_target_name(cfg)
    if target == "asterinas":
        return AsterinasTargetAdapter()
    return GenericTargetAdapter()
