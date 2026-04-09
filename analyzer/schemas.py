from __future__ import annotations


RAW_TRACE_REQUIRED = {
    "program_id",
    "side",
    "run_id",
    "status",
    "events",
    "process_exit",
}

CANONICAL_TRACE_REQUIRED = {
    "program_id",
    "side",
    "event_count",
    "events",
    "final_state",
    "process_exit",
}


def validate_raw_trace(payload: dict[str, object]) -> None:
    missing = RAW_TRACE_REQUIRED - payload.keys()
    if missing:
        raise ValueError(f"raw trace missing fields: {sorted(missing)}")
    previous = -1
    for event in payload["events"]:
        if event["event_index"] <= previous:
            raise ValueError("raw trace event indexes are not strictly increasing")
        previous = event["event_index"]


def validate_canonical_trace(payload: dict[str, object]) -> None:
    missing = CANONICAL_TRACE_REQUIRED - payload.keys()
    if missing:
        raise ValueError(f"canonical trace missing fields: {sorted(missing)}")
    if payload["event_count"] != len(payload["events"]):
        raise ValueError("canonical trace event_count mismatch")
