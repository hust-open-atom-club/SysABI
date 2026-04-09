from __future__ import annotations

import json
import shlex
from pathlib import Path

from analyzer.schemas import validate_raw_trace
from orchestrator.common import dump_json


MARKER_PREFIX = "__SYZABI"


def compose_batch_autorun(preview_bytes: int, cases: list[dict[str, object]]) -> str:
    lines = [
        "#!/bin/sh",
        "set +e",
        "",
        "BUSYBOX=/usr/bin/busybox",
        "RUNTIME_DIR=/tmp/syzabi",
        "",
        "$BUSYBOX mkdir -p /proc /sys \"$RUNTIME_DIR\"",
        "$BUSYBOX mount -t proc proc /proc >/dev/null 2>&1 || true",
        "$BUSYBOX mount -t sysfs sysfs /sys >/dev/null 2>&1 || true",
        "",
        "emit_file_section() {",
        "    name=\"$1\"",
        "    path=\"$2\"",
        f"    echo \"{MARKER_PREFIX}_BEGIN_${{name}}__\"",
        "    if [ -f \"$path\" ]; then",
        "        $BUSYBOX cat \"$path\"",
        "    fi",
        "    echo",
        f"    echo \"{MARKER_PREFIX}_END_${{name}}__\"",
        "}",
        "",
        "emit_external_state() {",
        "    work_dir=\"$1\"",
        f"    echo \"{MARKER_PREFIX}_BEGIN_EXTERNAL_STATE__\"",
        "    printf '{\"files\":['",
        "    sep=\"\"",
        "    $BUSYBOX find \"$work_dir\" -type f | $BUSYBOX sort > \"$RUNTIME_DIR/filelist.txt\"",
        "    while IFS= read -r path; do",
        "        rel=\"${path#$work_dir/}\"",
        "        size=\"$($BUSYBOX stat -c %s \"$path\" 2>/dev/null || echo 0)\"",
        "        sha=\"$($BUSYBOX sha256sum \"$path\" 2>/dev/null | $BUSYBOX awk '{print $1}')\"",
        "        printf '%s{\"path\":\"%s\",\"size\":%s,\"sha256\":\"%s\"}' \"$sep\" \"$rel\" \"$size\" \"$sha\"",
        "        sep=\",\"",
        "    done < \"$RUNTIME_DIR/filelist.txt\"",
        "    printf ']}'",
        "    echo",
        f"    echo \"{MARKER_PREFIX}_END_EXTERNAL_STATE__\"",
        "}",
        "",
    ]
    for index, case in enumerate(cases):
        program_id = shlex.quote(str(case["program_id"]))
        binary_path = shlex.quote(f"/syzkabi/batch/{index}.bin")
        header = json.dumps({"case_index": index, "program_id": str(case["program_id"])})
        inject_trace = case.get("inject_trace")
        lines.extend(
            [
                f"PROGRAM_ID={program_id}",
                f"BINARY_PATH={binary_path}",
                f"WORK_DIR=\"$RUNTIME_DIR/work-{index}\"",
                f"echo \"{MARKER_PREFIX}_BEGIN_BATCH_CASE__\"",
                f"printf '%s\\n' {shlex.quote(header)}",
                "$BUSYBOX mkdir -p \"$WORK_DIR\"",
                "$BUSYBOX chmod +x \"$BINARY_PATH\" 2>/dev/null || true",
                "unset SYZABI_INJECT_TRACE_ENABLED SYZABI_INJECT_TRACE_CALL_INDEX SYZABI_INJECT_TRACE_SYSCALL SYZABI_INJECT_TRACE_FIELD SYZABI_INJECT_TRACE_VALUE",
                "export SYZABI_SIDE=candidate",
                "export SYZABI_TRACE_PREVIEW_BYTES=" + shlex.quote(str(preview_bytes)),
            ]
        )
        if inject_trace:
            lines.extend(
                [
                    "export SYZABI_INJECT_TRACE_ENABLED=1",
                    "export SYZABI_INJECT_TRACE_CALL_INDEX=" + shlex.quote(str(inject_trace.get("call_index", -1))),
                    "export SYZABI_INJECT_TRACE_SYSCALL=" + shlex.quote(str(inject_trace.get("syscall_name", ""))),
                    "export SYZABI_INJECT_TRACE_FIELD=" + shlex.quote(str(inject_trace.get("field", "return"))),
                    "export SYZABI_INJECT_TRACE_VALUE=" + shlex.quote(str(inject_trace.get("value", 0))),
                ]
            )
        lines.extend(
            [
                "export SYZABI_TRACE_EVENTS_PATH=\"$RUNTIME_DIR/raw-trace.events.jsonl\"",
                "$BUSYBOX rm -f \"$RUNTIME_DIR/stdout.txt\" \"$RUNTIME_DIR/stderr.txt\" \"$RUNTIME_DIR/raw-trace.events.jsonl\" \"$RUNTIME_DIR/filelist.txt\"",
                "cd \"$WORK_DIR\" || exit 125",
                "\"$BINARY_PATH\" > \"$RUNTIME_DIR/stdout.txt\" 2> \"$RUNTIME_DIR/stderr.txt\"",
                "EXIT_CODE=$?",
                "PROC_STATUS=ok",
                "if [ \"$EXIT_CODE\" -ge 128 ]; then",
                "    PROC_STATUS=crash",
                "fi",
                f"echo \"{MARKER_PREFIX}_BEGIN_PROCESS_EXIT__\"",
                "printf '{\"status\":\"%s\",\"exit_code\":%s,\"timed_out\":false}\\n' \"$PROC_STATUS\" \"$EXIT_CODE\"",
                f"echo \"{MARKER_PREFIX}_END_PROCESS_EXIT__\"",
                "emit_file_section STDOUT \"$RUNTIME_DIR/stdout.txt\"",
                "emit_file_section STDERR \"$RUNTIME_DIR/stderr.txt\"",
                "emit_file_section EVENTS \"$RUNTIME_DIR/raw-trace.events.jsonl\"",
                "emit_external_state \"$WORK_DIR\"",
                f"echo \"{MARKER_PREFIX}_END_BATCH_CASE__\"",
                "",
            ]
        )
    lines.extend(
        [
            "$BUSYBOX sync",
            "$BUSYBOX poweroff -f >/dev/null 2>&1 || $BUSYBOX halt -f >/dev/null 2>&1 || $BUSYBOX reboot -f >/dev/null 2>&1 || echo o > /proc/sysrq-trigger",
            "",
        ]
    )
    return "\n".join(lines)


def compose_packaged_autorun(preview_bytes: int) -> str:
    return f"""#!/bin/sh
set +e

BUSYBOX=/usr/bin/busybox
RUNTIME_DIR=/tmp/syzabi
WORK_DIR="$RUNTIME_DIR/work"
EXT2_MOUNT=/ext2
RAW_SELECTOR_MAGIC=SYZABI_ENV_V1

emit_file_section() {{
    name="$1"
    path="$2"
    echo "{MARKER_PREFIX}_BEGIN_${{name}}__"
    if [ -f "$path" ]; then
        $BUSYBOX cat "$path"
    fi
    echo
    echo "{MARKER_PREFIX}_END_${{name}}__"
}}

emit_external_state() {{
    echo "{MARKER_PREFIX}_BEGIN_EXTERNAL_STATE__"
    printf '{{"files":['
    sep=""
    $BUSYBOX find "$WORK_DIR" -type f | $BUSYBOX sort > "$RUNTIME_DIR/filelist.txt"
    while IFS= read -r path; do
        rel="${{path#$WORK_DIR/}}"
        size="$($BUSYBOX stat -c %s "$path" 2>/dev/null || echo 0)"
        sha="$($BUSYBOX sha256sum "$path" 2>/dev/null | $BUSYBOX awk '{{print $1}}')"
        printf '%s{{"path":"%s","size":%s,"sha256":"%s"}}' "$sep" "$rel" "$size" "$sha"
        sep=","
    done < "$RUNTIME_DIR/filelist.txt"
    printf ']}}'
    echo
    echo "{MARKER_PREFIX}_END_EXTERNAL_STATE__"
}}

fail_and_poweroff() {{
    status="$1"
    exit_code="$2"
    message="$3"
    echo "{MARKER_PREFIX}_BEGIN_PROCESS_EXIT__"
    printf '{{"status":"%s","exit_code":%s,"timed_out":false}}\\n' "$status" "$exit_code"
    echo "{MARKER_PREFIX}_END_PROCESS_EXIT__"
    echo "{MARKER_PREFIX}_BEGIN_STDOUT__"
    echo
    echo "{MARKER_PREFIX}_END_STDOUT__"
    echo "{MARKER_PREFIX}_BEGIN_STDERR__"
    if [ -n "$message" ]; then
        printf '%s\\n' "$message"
    fi
    echo "{MARKER_PREFIX}_END_STDERR__"
    echo "{MARKER_PREFIX}_BEGIN_EVENTS__"
    echo
    echo "{MARKER_PREFIX}_END_EVENTS__"
    emit_external_state
    $BUSYBOX sync
    $BUSYBOX poweroff -f >/dev/null 2>&1 || $BUSYBOX halt -f >/dev/null 2>&1 || $BUSYBOX reboot -f >/dev/null 2>&1 || echo o > /proc/sysrq-trigger
}}

load_selector_from_raw_devices() {{
    selector_file="$RUNTIME_DIR/syzkabi.raw.env"
    header_file="$RUNTIME_DIR/syzkabi.raw.header"
    for device in /dev/vda /dev/vdb /dev/vdc /dev/sda /dev/sdb /dev/sdc $AVAILABLE_BLOCK_DEVICES; do
        if [ ! -b "$device" ]; then
            continue
        fi
        $BUSYBOX dd if="$device" of="$header_file" bs=1024 count=1 2>/dev/null || continue
        if ! $BUSYBOX grep -q "^$RAW_SELECTOR_MAGIC$" "$header_file"; then
            continue
        fi
        $BUSYBOX sed -n "/^$RAW_SELECTOR_MAGIC$/,/^__END__$/p" "$header_file" | $BUSYBOX sed '1d;$d' > "$selector_file"
        if [ ! -s "$selector_file" ]; then
            continue
        fi
        . "$selector_file"
        return 0
    done
    return 1
}}

$BUSYBOX mkdir -p /proc /sys "$RUNTIME_DIR" "$WORK_DIR" "$EXT2_MOUNT"
$BUSYBOX mount -t proc proc /proc >/dev/null 2>&1 || true
$BUSYBOX mount -t sysfs sysfs /sys >/dev/null 2>&1 || true
MOUNTED_EXT2_DEVICE=""
AVAILABLE_BLOCK_DEVICES=""
RAW_SELECTOR_LOADED=0
attempt=0
while [ "$attempt" -lt 30 ]; do
    AVAILABLE_BLOCK_DEVICES="$($BUSYBOX find /dev -maxdepth 1 -type b 2>/dev/null | $BUSYBOX sort | $BUSYBOX tr '\n' ' ')"
    for device in /dev/vda /dev/vdb /dev/vdc /dev/sda /dev/sdb /dev/sdc $AVAILABLE_BLOCK_DEVICES; do
        if [ ! -b "$device" ]; then
            continue
        fi
        if $BUSYBOX mount -t ext2 "$device" "$EXT2_MOUNT" >/dev/null 2>&1; then
            MOUNTED_EXT2_DEVICE="$device"
            break
        fi
    done
    if [ -n "$MOUNTED_EXT2_DEVICE" ]; then
        break
    fi
    if load_selector_from_raw_devices; then
        RAW_SELECTOR_LOADED=1
        break
    fi
    attempt=$((attempt + 1))
    $BUSYBOX sleep 1
done
if [ -z "$MOUNTED_EXT2_DEVICE" ] && [ "$RAW_SELECTOR_LOADED" -ne 1 ]; then
    if ! load_selector_from_raw_devices; then
        fail_and_poweroff infra_error 125 "failed to mount ext2 package disk on $EXT2_MOUNT; block devices: $AVAILABLE_BLOCK_DEVICES"
    fi
fi

if [ -f "$EXT2_MOUNT/syzkabi.env" ]; then
    . "$EXT2_MOUNT/syzkabi.env"
elif ! load_selector_from_raw_devices; then
    fail_and_poweroff infra_error 125 "missing packaged selector file: $EXT2_MOUNT/syzkabi.env and no raw selector header found"
fi

SLOT="${{SYZABI_PACKAGE_SLOT:-}}"
if [ -z "$SLOT" ]; then
    fail_and_poweroff infra_error 125 "missing SYZABI_PACKAGE_SLOT selector"
fi
BINARY_PATH="/syzkabi/batch/$SLOT.bin"
if [ ! -x "$BINARY_PATH" ]; then
    fail_and_poweroff infra_error 125 "missing packaged testcase binary: $BINARY_PATH"
fi

export SYZABI_SIDE=candidate
export SYZABI_TRACE_EVENTS_PATH="$RUNTIME_DIR/raw-trace.events.jsonl"
export SYZABI_TRACE_PREVIEW_BYTES="{preview_bytes}"
if [ -n "${{SYZABI_INJECT_TRACE_ENABLED:-}}" ]; then
    export SYZABI_INJECT_TRACE_ENABLED="$SYZABI_INJECT_TRACE_ENABLED"
fi
if [ -n "${{SYZABI_INJECT_TRACE_CALL_INDEX:-}}" ]; then
    export SYZABI_INJECT_TRACE_CALL_INDEX="$SYZABI_INJECT_TRACE_CALL_INDEX"
fi
if [ -n "${{SYZABI_INJECT_TRACE_SYSCALL:-}}" ]; then
    export SYZABI_INJECT_TRACE_SYSCALL="$SYZABI_INJECT_TRACE_SYSCALL"
fi
if [ -n "${{SYZABI_INJECT_TRACE_FIELD:-}}" ]; then
    export SYZABI_INJECT_TRACE_FIELD="$SYZABI_INJECT_TRACE_FIELD"
fi
if [ -n "${{SYZABI_INJECT_TRACE_VALUE:-}}" ]; then
    export SYZABI_INJECT_TRACE_VALUE="$SYZABI_INJECT_TRACE_VALUE"
fi

$BUSYBOX chmod +x "$BINARY_PATH" 2>/dev/null || true
cd "$WORK_DIR" || exit 125
"$BINARY_PATH" > "$RUNTIME_DIR/stdout.txt" 2> "$RUNTIME_DIR/stderr.txt"
EXIT_CODE=$?
PROC_STATUS=ok
if [ "$EXIT_CODE" -ge 128 ]; then
    PROC_STATUS=crash
fi

echo "{MARKER_PREFIX}_BEGIN_PROCESS_EXIT__"
printf '{{"status":"%s","exit_code":%s,"timed_out":false}}\\n' "$PROC_STATUS" "$EXIT_CODE"
echo "{MARKER_PREFIX}_END_PROCESS_EXIT__"
emit_file_section STDOUT "$RUNTIME_DIR/stdout.txt"
emit_file_section STDERR "$RUNTIME_DIR/stderr.txt"
emit_file_section EVENTS "$RUNTIME_DIR/raw-trace.events.jsonl"
emit_external_state
$BUSYBOX sync
$BUSYBOX poweroff -f >/dev/null 2>&1 || $BUSYBOX halt -f >/dev/null 2>&1 || $BUSYBOX reboot -f >/dev/null 2>&1 || echo o > /proc/sysrq-trigger
"""


def shared_package_runtime_dirs(package_dir: Path) -> tuple[Path, Path]:
    return (
        package_dir / "cargo-target",
        package_dir / "osdk-output",
    )


def shared_package_bundle_dir(package_dir: Path) -> Path:
    cargo_target_dir, _ = shared_package_runtime_dirs(package_dir)
    return cargo_target_dir / "osdk" / "aster-kernel"


def extract_section(console_text: str, name: str) -> str | None:
    begin = f"{MARKER_PREFIX}_BEGIN_{name}__\n"
    end = f"\n{MARKER_PREFIX}_END_{name}__"
    start = console_text.find(begin)
    if start < 0:
        return None
    start += len(begin)
    finish = console_text.find(end, start)
    if finish < 0:
        return None
    return console_text[start:finish].strip("\n")


def parse_events(section: str | None) -> list[dict[str, object]]:
    if not section:
        return []
    events: list[dict[str, object]] = []
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def parse_process_exit(section: str | None) -> dict[str, object]:
    if not section:
        return {"status": "infra_error", "exit_code": None, "timed_out": False}
    return json.loads(section)


def parse_external_state(section: str | None) -> dict[str, object]:
    if not section:
        return {"files": []}
    return json.loads(section)


def extract_batch_case_blocks(console_text: str) -> list[dict[str, object]]:
    begin = f"{MARKER_PREFIX}_BEGIN_BATCH_CASE__\n"
    end = f"\n{MARKER_PREFIX}_END_BATCH_CASE__"
    blocks: list[dict[str, object]] = []
    search_from = 0
    while True:
        start = console_text.find(begin, search_from)
        if start < 0:
            break
        header_start = start + len(begin)
        header_end = console_text.find("\n", header_start)
        if header_end < 0:
            break
        finish = console_text.find(end, header_end + 1)
        if finish < 0:
            break
        header = json.loads(console_text[header_start:header_end].strip())
        body = console_text[header_end + 1 : finish]
        blocks.append(
            {
                "header": header,
                "body": body,
                "console_log": console_text[start : finish + len(end)],
            }
        )
        search_from = finish + len(end)
    return blocks


def parse_batch_case_results(
    console_text: str,
    cases: list[dict[str, object]],
    *,
    kernel_build: str,
    missing_status: str,
    missing_detail: str,
) -> list[dict[str, object]]:
    parsed_blocks = {
        str(block["header"]["program_id"]): block
        for block in extract_batch_case_blocks(console_text)
    }
    results: list[dict[str, object]] = []
    for case in cases:
        program_id = str(case["program_id"])
        run_id = str(case["run_id"])
        block = parsed_blocks.get(program_id)
        if block is None:
            results.append(
                {
                    "program_id": program_id,
                    "run_id": run_id,
                    "status": missing_status,
                    "exit_code": None,
                    "status_detail": missing_detail,
                    "kernel_build": kernel_build,
                    "stdout": "",
                    "stderr": "",
                    "console_log": console_text,
                    "events": [],
                    "external_state": {"files": []},
                    "process_exit": {"status": missing_status, "exit_code": None, "timed_out": missing_status == "timeout"},
                }
            )
            continue

        body = str(block["body"])
        process_exit = parse_process_exit(extract_section(body, "PROCESS_EXIT"))
        events = parse_events(extract_section(body, "EVENTS"))
        status = candidate_status_from_events(events, process_exit)
        results.append(
            {
                "program_id": program_id,
                "run_id": run_id,
                "status": status,
                "exit_code": process_exit.get("exit_code"),
                "status_detail": None,
                "kernel_build": kernel_build,
                "stdout": extract_section(body, "STDOUT") or "",
                "stderr": extract_section(body, "STDERR") or "",
                "console_log": str(block["console_log"]),
                "events": events,
                "external_state": parse_external_state(extract_section(body, "EXTERNAL_STATE")),
                "process_exit": process_exit,
            }
        )
    return results


def materialize_batch_case_outputs(results: list[dict[str, object]], cases: list[dict[str, object]]) -> None:
    for payload, case in zip(results, cases):
        stdout_path = Path(str(case["stdout_path"]))
        stderr_path = Path(str(case["stderr_path"]))
        console_path = Path(str(case["console_path"]))
        raw_trace_path = Path(str(case["raw_trace_path"]))
        external_state_path = Path(str(case["external_state_path"]))
        runner_result_path = Path(str(case["runner_result_path"]))
        events_path = Path(str(case["events_path"]))

        stdout_path.write_text(str(payload["stdout"]), encoding="utf-8")
        stderr_path.write_text(str(payload["stderr"]), encoding="utf-8")
        console_path.write_text(str(payload["console_log"]), encoding="utf-8")
        if payload["events"]:
            events_path.write_text(
                "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in payload["events"]),
                encoding="utf-8",
            )
        else:
            events_path.write_text("", encoding="utf-8")
        raw_trace = {
            "program_id": payload["program_id"],
            "side": "candidate",
            "run_id": payload["run_id"],
            "status": payload["status"],
            "events": payload["events"],
            "process_exit": payload["process_exit"],
        }
        validate_raw_trace(raw_trace)
        dump_json(raw_trace_path, raw_trace)
        dump_json(external_state_path, payload["external_state"])
        dump_json(
            runner_result_path,
            {
                "status": payload["status"],
                "exit_code": payload["exit_code"],
                "status_detail": payload["status_detail"],
                "kernel_build": payload["kernel_build"],
            },
        )


def candidate_status_from_events(events: list[dict[str, object]], process_exit: dict[str, object]) -> str:
    process_status = str(process_exit.get("status", "ok"))
    if process_status != "ok":
        return process_status
    for event in events:
        if int(event.get("return_value", 0)) == -1 and int(event.get("errno", 0)) in {38, 95}:
            return "unsupported"
    return "ok"
