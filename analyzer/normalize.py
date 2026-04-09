from __future__ import annotations

import argparse
import json
from pathlib import Path

from analyzer.schemas import validate_canonical_trace, validate_raw_trace
from orchestrator.common import dump_json, load_json


FD_RETURN_SYSCALLS = {"open", "openat", "eventfd", "eventfd2"}
PID_RETURN_SYSCALLS = {"clone", "getpid", "getppid"}
ADDR_RETURN_SYSCALLS = {"mmap", "brk"}
FD_ARG_POSITIONS = {
    "close": {0},
    "read": {0},
    "write": {0},
    "pread64": {0},
    "pwrite64": {0},
    "lseek": {0},
    "fstat": {0},
    "newfstatat": {0},
    "openat": {0},
    "mmap": {4},
}
PID_ARG_POSITIONS = {"wait4": {0}}
ADDR_ARG_POSITIONS = {
    "open": {0},
    "openat": {1},
    "read": {1},
    "write": {1},
    "pread64": {1},
    "pwrite64": {1},
    "fstat": {1},
    "newfstatat": {1, 2},
    "mkdir": {0},
    "unlink": {0},
    "rename": {0, 1},
    "mmap": {0},
    "munmap": {0},
    "mprotect": {0},
    "brk": {0},
    "clone": {1, 2, 3, 4},
    "wait4": {1, 3},
    "pipe": {0},
    "pipe2": {0},
    "socketpair": {3},
}


class CanonicalContext:
    def __init__(self) -> None:
        self.fd_map: dict[int, str] = {}
        self.addr_map: dict[int, str] = {}
        self.pid_map: dict[int, str] = {}

    def token(self, kind: str, value: int) -> str | int:
        mapping = {
            "fd": self.fd_map,
            "addr": self.addr_map,
            "pid": self.pid_map,
        }[kind]
        if value in mapping:
            return mapping[value]
        token = f"{kind}#{len(mapping)}"
        mapping[value] = token
        return token


def normalize_scalar(value: int, kind: str | None, ctx: CanonicalContext) -> str | int:
    if kind is not None and value in {0, -1, -100}:
        return value
    if kind is not None:
        return ctx.token(kind, value)
    return value


def normalize_outputs(outputs: list[dict[str, object]], ctx: CanonicalContext) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for output in outputs:
        item = {
            "label": output["label"],
            "arg_index": output["arg_index"],
            "length": output["length"],
            "preview_hex": output["preview_hex"],
        }
        if output["label"] == "stat":
            # Keep stat previews compatible with older traces by masking the
            # volatile device/inode prefix, but retain the digest because the
            # tracer now sanitizes those fields before hashing the full buffer.
            preview_hex = str(output["preview_hex"])
            if len(preview_hex) >= 32:
                item["preview_hex"] = ("0" * 32) + preview_hex[32:]
        item["sha256"] = output["sha256"]
        if output.get("resource_kind") == "fd":
            item["resource_kind"] = "fd"
            item["resource_values"] = [ctx.token("fd", int(value)) for value in output["resource_values"]]
        normalized.append(item)
    return normalized


def normalize_event(event: dict[str, object], actual_index: int, ctx: CanonicalContext) -> dict[str, object]:
    name = event["syscall_name"]
    args = []
    fd_positions = FD_ARG_POSITIONS.get(name, set())
    pid_positions = PID_ARG_POSITIONS.get(name, set())
    addr_positions = ADDR_ARG_POSITIONS.get(name, set())
    for index, value in enumerate(event["args"]):
        kind = None
        if index in fd_positions and int(value) >= 0:
            kind = "fd"
        elif index in pid_positions and int(value) > 0:
            kind = "pid"
        elif index in addr_positions and int(value) > 0:
            kind = "addr"
        args.append(normalize_scalar(int(value), kind, ctx))

    ret_kind = None
    if name in FD_RETURN_SYSCALLS and int(event["return_value"]) >= 0:
        ret_kind = "fd"
    elif name in PID_RETURN_SYSCALLS and int(event["return_value"]) > 0:
        ret_kind = "pid"
    elif name in ADDR_RETURN_SYSCALLS and int(event["return_value"]) > 0:
        ret_kind = "addr"

    normalized = {
        "index": actual_index,
        "source_event_index": event["event_index"],
        "syscall_name": name,
        "syscall_number": event["syscall_number"],
        "args": args,
        "return_value": normalize_scalar(int(event["return_value"]), ret_kind, ctx)
        if int(event["return_value"]) != 0
        else 0,
        "errno": event["errno"],
        "duration_ns": int(event["end_ns"]) - int(event["start_ns"]),
        "outputs": normalize_outputs(event["outputs"], ctx),
    }
    return normalized


def canonicalize(raw_trace: dict[str, object], external_state: dict[str, object]) -> dict[str, object]:
    validate_raw_trace(raw_trace)
    ctx = CanonicalContext()
    events = [normalize_event(event, index, ctx) for index, event in enumerate(raw_trace["events"])]
    payload = {
        "program_id": raw_trace["program_id"],
        "side": raw_trace["side"],
        "event_count": len(events),
        "events": events,
        "final_state": external_state,
        "process_exit": raw_trace["process_exit"],
    }
    validate_canonical_trace(payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-trace", required=True)
    parser.add_argument("--external-state", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    raw_trace = load_json(args.raw_trace)
    external_state = load_json(args.external_state)
    dump_json(args.output, canonicalize(raw_trace, external_state))


if __name__ == "__main__":
    main()
