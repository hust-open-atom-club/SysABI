from __future__ import annotations

from core.constants import Classification, ExecutionStatus
from orchestrator.common import config


def classify_result(
    *,
    reference_stable: bool,
    reference_status: str,
    candidate_status: str,
    comparison: dict[str, object] | None,
) -> str:
    classes = config()["classification"]
    if not reference_stable or reference_status != ExecutionStatus.OK:
        return classes[Classification.BASELINE_INVALID]
    if candidate_status == ExecutionStatus.UNSUPPORTED:
        return classes[Classification.UNSUPPORTED_FEATURE]
    # candidate_bug (e.g., kernel panic) is treated like crash for classification
    if candidate_status in {ExecutionStatus.CRASH, ExecutionStatus.TIMEOUT, ExecutionStatus.CANDIDATE_BUG}:
        return classes[Classification.BUG_LIKELY]
    if comparison is None:
        if candidate_status == ExecutionStatus.INFRA_ERROR:
            return classes[Classification.WEAK_SPEC_OR_ENV_NOISE]
        return classes[Classification.UNSUPPORTED_FEATURE]
    if comparison["equivalent"]:
        return classes[Classification.NO_DIFF]
    if candidate_status in {ExecutionStatus.CRASH, ExecutionStatus.TIMEOUT, ExecutionStatus.CANDIDATE_BUG}:
        return classes[Classification.BUG_LIKELY]
    if candidate_status == ExecutionStatus.INFRA_ERROR:
        return classes[Classification.WEAK_SPEC_OR_ENV_NOISE]
    if comparison["noise_only"]:
        return classes[Classification.WEAK_SPEC_OR_ENV_NOISE]
    return classes[Classification.BUG_LIKELY]
