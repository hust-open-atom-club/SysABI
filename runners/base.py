from __future__ import annotations

from typing import Protocol, runtime_checkable, Any

from runners.common import RunnerExecution


@runtime_checkable
class RunnerProtocol(Protocol):
    def prepare(self, **kwargs: Any) -> None:
        ...

    def healthcheck(self, **kwargs: Any) -> dict[str, Any]:
        ...

    def run_case(self, **kwargs: Any) -> RunnerExecution:
        ...

    def run_batch(self, **kwargs: Any) -> list[RunnerExecution]:
        ...

    def collect_outputs(self, **kwargs: Any) -> dict[str, Any]:
        ...
