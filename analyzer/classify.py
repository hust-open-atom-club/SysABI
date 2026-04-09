from __future__ import annotations

from orchestrator.common import config


def classify_result(
    *,
    reference_stable: bool,
    reference_status: str,
    candidate_status: str,
    comparison: dict[str, object] | None,
) -> str:
    classes = config()["classification"]
    if not reference_stable or reference_status != "ok":
        return classes["baseline_invalid"]
    if candidate_status == "unsupported":
        return classes["unsupported_feature"]
    if comparison is None:
        if candidate_status in {"crash", "timeout"}:
            return classes["bug_likely"]
        if candidate_status == "infra_error":
            return classes["weak_spec_or_env_noise"]
        return classes["unsupported_feature"]
    if comparison["equivalent"]:
        return classes["no_diff"]
    if candidate_status in {"crash", "timeout"}:
        return classes["bug_likely"]
    if candidate_status == "infra_error":
        return classes["weak_spec_or_env_noise"]
    if comparison["noise_only"]:
        return classes["weak_spec_or_env_noise"]
    return classes["bug_likely"]
