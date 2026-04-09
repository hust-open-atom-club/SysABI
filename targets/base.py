from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable, Any


@runtime_checkable
class TargetAdapter(Protocol):
    name: str

    def compose_template_inputs(self, cfg: dict[str, Any]) -> dict[str, object]:
        ...

    def packaged_candidate_env(self, package_dir: Path, slot: int) -> dict[str, str]:
        ...

