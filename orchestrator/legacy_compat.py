from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from core.paths import resolve_repo_path


_WARNED_DEPRECATIONS: set[str] = set()


def emit_deprecation_warning_once(tag: str, message: str) -> None:
    if tag in _WARNED_DEPRECATIONS:
        return
    _WARNED_DEPRECATIONS.add(tag)
    sys.stderr.write(f"warning: deprecated compatibility path in use: {message}\n")


def infer_legacy_target(payload: dict[str, Any], *, workflow: str) -> str:
    inferred_workflow = str(payload.get("workflow", workflow))
    if inferred_workflow.startswith("asterinas"):
        return "asterinas"
    return "linux"


def default_presentation(*, target: str, workflow: str) -> dict[str, str]:
    canonical_path = resolve_repo_path(Path("configs") / "workflows" / f"{workflow}.json")
    if canonical_path.exists():
        payload = json.loads(canonical_path.read_text(encoding="utf-8"))
        presentation = payload.get("presentation")
        if isinstance(presentation, dict):
            reference_label = presentation.get("reference_label")
            candidate_label = presentation.get("candidate_label")
            if isinstance(reference_label, str) and isinstance(candidate_label, str):
                return {
                    "reference_label": reference_label,
                    "candidate_label": candidate_label,
                }
    if workflow == "baseline":
        return {"reference_label": "Linux(reference)", "candidate_label": "Linux(candidate)"}
    return {"reference_label": "Reference", "candidate_label": "Candidate"}
