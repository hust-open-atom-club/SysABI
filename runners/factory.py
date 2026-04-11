from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runners.command import CommandRunner
from runners.local import LocalRunner


RunnerBuilder = Callable[[dict[str, Any]], object]

_RUNNER_BUILDERS: dict[str, RunnerBuilder] = {}


def register_runner_builder(kind: str, builder: RunnerBuilder) -> None:
    _RUNNER_BUILDERS[kind] = builder


def available_runner_kinds() -> tuple[str, ...]:
    return tuple(sorted(_RUNNER_BUILDERS))


def build_runner(profile: dict[str, Any]):
    kind = str(profile.get("kind") or "local")
    builder = _RUNNER_BUILDERS.get(kind)
    if builder is None:
        supported = ", ".join(available_runner_kinds())
        raise ValueError(f"unsupported runner kind: {kind} (supported: {supported})")
    return builder(profile)


register_runner_builder("command", CommandRunner)
register_runner_builder("local", LocalRunner)
