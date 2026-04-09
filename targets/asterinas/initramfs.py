from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from targets.asterinas.common import RunnerError, local_tmp_dir
from tools.run_asterinas_shared import compose_packaged_autorun
from tools.run_asterinas_shared import compose_batch_autorun


MARKER_PREFIX = "__SYZABI"
GUEST_ENV_HEADER_MAGIC = "SYZABI_ENV_V1"
GUEST_ENV_HEADER_SIZE = 1024


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


def repack_initramfs(source_dir: Path, output_path: Path) -> None:
    command = f"find . -print0 | cpio --null -o --format=newc --quiet | gzip -9 > {output_path}"
    result = subprocess.run(
        command,
        shell=True,
        cwd=source_dir,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "TMPDIR": str(local_tmp_dir())},
    )
    if result.returncode != 0:
        raise RunnerError(result.stderr.strip() or "failed to repack initramfs")


def create_minimal_initramfs(cfg: dict[str, object], binary_path: Path, work_dir: Path) -> Path:
    root = work_dir / "asterinas-minimal-initramfs"
    if root.exists():
        shutil.rmtree(root)
    for directory in (
        root / "bin",
        root / "dev",
        root / "etc/profile.d",
        root / "proc",
        root / "root",
        root / "sbin",
        root / "sys",
        root / "tmp",
        root / "usr/bin",
        root / "syzkabi",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2("/usr/bin/busybox", root / "usr/bin/busybox")
    os.symlink("/usr/bin/busybox", root / "bin/sh")
    init_path = root / "bin/init"
    init_path.write_text(compose_init(), encoding="utf-8")
    init_path.chmod(0o755)
    os.symlink("/bin/init", root / "sbin/init")
    shutil.copy2(binary_path, root / "syzkabi/testcase.bin")
    (root / "syzkabi/testcase.bin").chmod(0o755)
    (root / "etc/profile").write_text(compose_profile(), encoding="utf-8")
    (root / "etc/profile.d/init.sh").write_text(compose_init_hook(), encoding="utf-8")
    autorun_path = root / "syzkabi/autorun.sh"
    autorun_path.write_text(compose_autorun(int(cfg["normalization"]["preview_bytes"])), encoding="utf-8")
    autorun_path.chmod(0o755)

    output_path = work_dir / "asterinas-initramfs.cpio.gz"
    repack_initramfs(root, output_path)
    return output_path


def load_initramfs_package_manifest(package_dir: Path) -> dict[str, object]:
    manifest_path = package_dir / "package-manifest.json"
    if not manifest_path.exists():
        raise RunnerError(f"missing initramfs package manifest: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RunnerError(f"invalid initramfs package manifest: {manifest_path}")
    return payload


def create_packaged_initramfs(cfg: dict[str, object], package_dir: Path, payload: dict[str, object]) -> Path:
    root = package_dir / "initramfs-root"
    if root.exists():
        shutil.rmtree(root)
    for directory in (
        root / "bin",
        root / "dev",
        root / "etc/profile.d",
        root / "proc",
        root / "root",
        root / "sbin",
        root / "sys",
        root / "tmp",
        root / "usr/bin",
        root / "syzkabi/batch",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2("/usr/bin/busybox", root / "usr/bin/busybox")
    os.symlink("/usr/bin/busybox", root / "bin/sh")
    init_path = root / "bin/init"
    init_path.write_text(compose_init(), encoding="utf-8")
    init_path.chmod(0o755)
    os.symlink("/bin/init", root / "sbin/init")
    for case in payload["cases"]:
        source = Path(str(case["binary_path"]))
        if not source.exists():
            raise RunnerError(f"missing packaged testcase binary: {source}")
        destination = root / "syzkabi" / "batch" / f"{int(case['slot'])}.bin"
        shutil.copy2(source, destination)
        destination.chmod(0o755)
    (root / "etc/profile").write_text(compose_profile(), encoding="utf-8")
    (root / "etc/profile.d/init.sh").write_text(compose_init_hook(), encoding="utf-8")
    autorun_path = root / "syzkabi/autorun.sh"
    autorun_path.write_text(compose_packaged_autorun(int(payload["preview_bytes"])), encoding="utf-8")
    autorun_path.chmod(0o755)

    output_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
    repack_initramfs(root, output_path)
    return output_path


def ensure_packaged_initramfs(cfg: dict[str, object], package_dir: Path) -> Path:
    package_dir.mkdir(parents=True, exist_ok=True)
    output_path = package_dir / "asterinas-packaged-initramfs.cpio.gz"
    if output_path.exists():
        return output_path
    lock_path = package_dir / ".build.lock"
    with lock_path.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if output_path.exists():
            return output_path
        payload = load_initramfs_package_manifest(package_dir)
        return create_packaged_initramfs(cfg, package_dir, payload)


def create_batch_initramfs(cfg: dict[str, object], cases: list[dict[str, object]], work_dir: Path) -> Path:
    root = work_dir / "asterinas-batch-initramfs"
    if root.exists():
        shutil.rmtree(root)
    for directory in (
        root / "bin",
        root / "dev",
        root / "etc/profile.d",
        root / "proc",
        root / "root",
        root / "sbin",
        root / "sys",
        root / "tmp",
        root / "usr/bin",
        root / "syzkabi/batch",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    shutil.copy2("/usr/bin/busybox", root / "usr/bin/busybox")
    os.symlink("/usr/bin/busybox", root / "bin/sh")
    init_path = root / "bin/init"
    init_path.write_text(compose_init(), encoding="utf-8")
    init_path.chmod(0o755)
    os.symlink("/bin/init", root / "sbin/init")
    for index, case in enumerate(cases):
        destination = root / "syzkabi" / "batch" / f"{index}.bin"
        shutil.copy2(case["binary_path"], destination)
        destination.chmod(0o755)
    (root / "etc/profile").write_text(compose_profile(), encoding="utf-8")
    (root / "etc/profile.d/init.sh").write_text(compose_init_hook(), encoding="utf-8")
    autorun_path = root / "syzkabi/autorun.sh"
    autorun_path.write_text(compose_batch_autorun(int(cfg["normalization"]["preview_bytes"]), cases), encoding="utf-8")
    autorun_path.chmod(0o755)

    output_path = work_dir / "asterinas-batch-initramfs.cpio.gz"
    repack_initramfs(root, output_path)
    return output_path
