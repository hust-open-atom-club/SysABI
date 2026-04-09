from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _stable_view(payload: object) -> object:
    if isinstance(payload, dict):
        return {
            key: _stable_view(value)
            for key, value in payload.items()
            if key not in {"duration_ns", "start_ns", "end_ns"}
        }
    if isinstance(payload, list):
        return [_stable_view(item) for item in payload]
    return payload


def canonical_trace_hash(trace: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(_stable_view(trace), sort_keys=True).encode("utf-8")).hexdigest()


def all_equal(values: list[str]) -> bool:
    return bool(values) and len(set(values)) == 1


def build_status_ok(build_result_path: Path) -> bool:
    if not build_result_path.exists():
        return False
    payload = json.loads(build_result_path.read_text(encoding="utf-8"))
    return payload.get("status") == "ok"
