from __future__ import annotations

import os
import shlex

from tools.run_asterinas_shared import compose_packaged_autorun


MARKER_PREFIX = "__SYZABI"


def compose_profile() -> str:
    return """#!/bin/sh
if [ -f /etc/profile.d/init.sh ]; then
    . /etc/profile.d/init.sh
fi
"""


def compose_init() -> str:
    return """#!/bin/sh
exec /syzkabi/autorun.sh
"""


def compose_init_hook() -> str:
    return """#!/bin/sh
/syzkabi/autorun.sh
"""


def compose_autorun(preview_bytes: int) -> str:
    injected_env = []
    for name in (
        "SYZABI_INJECT_TRACE_ENABLED",
        "SYZABI_INJECT_TRACE_CALL_INDEX",
        "SYZABI_INJECT_TRACE_SYSCALL",
        "SYZABI_INJECT_TRACE_FIELD",
        "SYZABI_INJECT_TRACE_VALUE",
    ):
        value = os.environ.get(name)
        if value is None or value == "":
            continue
        injected_env.append(f"export {name}={shlex.quote(value)}")
    injected_block = "\n".join(injected_env)
    if injected_block:
        injected_block += "\n"
    return f"""#!/bin/sh
set +e

BUSYBOX=/usr/bin/busybox
RUNTIME_DIR=/tmp/syzabi
WORK_DIR="$RUNTIME_DIR/work"

$BUSYBOX mkdir -p /proc /sys "$RUNTIME_DIR" "$WORK_DIR"
$BUSYBOX mount -t proc proc /proc >/dev/null 2>&1 || true
$BUSYBOX mount -t sysfs sysfs /sys >/dev/null 2>&1 || true

export SYZABI_SIDE=candidate
export SYZABI_TRACE_EVENTS_PATH="$RUNTIME_DIR/raw-trace.events.jsonl"
export SYZABI_TRACE_PREVIEW_BYTES="{preview_bytes}"
{injected_block}

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

$BUSYBOX chmod +x /syzkabi/testcase.bin 2>/dev/null || true
cd "$WORK_DIR" || exit 125
/syzkabi/testcase.bin > "$RUNTIME_DIR/stdout.txt" 2> "$RUNTIME_DIR/stderr.txt"
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
