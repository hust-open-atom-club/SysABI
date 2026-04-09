from __future__ import annotations

from typing import Any

from runners.command import CommandRunner
from runners.local import LocalRunner


def build_runner(profile: dict[str, Any]):
    kind = str(profile.get("kind", "local"))
    if kind == "command":
        return CommandRunner(profile)
    return LocalRunner(profile)
