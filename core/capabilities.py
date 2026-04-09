from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    supports_batch_execution: bool = False
    supports_preflight: bool = False
    supports_snapshot_reuse: bool = False


def capabilities_from_config(cfg: dict[str, Any]) -> CapabilitySet:
    raw = cfg.get("capabilities", {})
    if not isinstance(raw, dict):
        return CapabilitySet()
    return CapabilitySet(
        supports_batch_execution=bool(raw.get("supports_batch_execution", False)),
        supports_preflight=bool(raw.get("supports_preflight", False)),
        supports_snapshot_reuse=bool(raw.get("supports_snapshot_reuse", False)),
    )

