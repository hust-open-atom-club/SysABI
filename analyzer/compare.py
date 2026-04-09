from __future__ import annotations


def compare_canonical(reference: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    if reference["event_count"] != candidate["event_count"]:
        return {
            "equivalent": False,
            "noise_only": False,
            "first_divergence_index": min(reference["event_count"], candidate["event_count"]),
            "reason": "event_count_mismatch",
        }

    first_divergence_index = None
    noise_only = True
    for left, right in zip(reference["events"], candidate["events"], strict=True):
        if left["syscall_name"] != right["syscall_name"]:
            first_divergence_index = left["index"]
            noise_only = False
            break
        for field in ("args", "return_value", "errno", "outputs"):
            if left[field] != right[field]:
                first_divergence_index = left["index"]
                noise_only = False
                break
        if first_divergence_index is not None:
            break

    final_state_equal = reference["final_state"] == candidate["final_state"]
    process_exit_equal = reference["process_exit"] == candidate["process_exit"]
    if not process_exit_equal:
        noise_only = False
        if first_divergence_index is None:
            first_divergence_index = reference["event_count"]
    equivalent = first_divergence_index is None and final_state_equal and process_exit_equal
    if equivalent:
        noise_only = False

    return {
        "equivalent": equivalent,
        "noise_only": noise_only and final_state_equal,
        "first_divergence_index": first_divergence_index,
        "reason": "no_diff" if equivalent else "content_mismatch",
        "final_state_equal": final_state_equal,
        "process_exit_equal": process_exit_equal,
    }
