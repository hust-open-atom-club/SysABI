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

    def prepare_target(self, **kwargs: Any) -> object:
        ...

    def healthcheck(self, *args: Any, **kwargs: Any) -> object:
        ...

    def run_case(self, *args: Any, **kwargs: Any) -> object:
        ...

    def run_batch(self, *args: Any, **kwargs: Any) -> object:
        ...
