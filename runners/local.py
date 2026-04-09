from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runners.command import CommandRunner


@dataclass(slots=True)
class LocalRunner(CommandRunner):
    pass
