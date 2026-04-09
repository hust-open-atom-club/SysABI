from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Protocol

from orchestrator.common import config, load_json, resolve_repo_path


UNSUPPORTED_PREFIX = "Unsupported syscall: "
PARSE_ERROR_PREFIX = "Strace Parse Error: "
SYSCALL_NAME_RE = re.compile(r"^\s*(?:\d+\s+)?(?P<name>[A-Za-z0-9_]+)\(")
FIELD_REASON_HINTS = {
    "flags": "unsupported_flag_pattern",
    "flags_in_events": "unsupported_flag_pattern",
    "mount_flags": "unsupported_flag_pattern",
    "event_flags": "unsupported_flag_pattern",
    "control_flags": "unsupported_flag_pattern",
    "codes": "unsupported_flag_pattern",
    "masks": "unsupported_flag_pattern",
    "who_flags": "unsupported_flag_pattern",
    "op_flags": "unsupported_flag_pattern",
}
PATH_PATTERN_SYSCALLS = {
    "mount",
    "umount",
    "umount2",
    "open",
    "openat",
    "rename",
    "renameat",
    "renameat2",
    "mkdir",
    "mkdirat",
    "link",
    "linkat",
    "symlink",
    "symlinkat",
    "unlink",
    "unlinkat",
    "newfstatat",
    "faccessat",
    "faccessat2",
    "readlinkat",
    "utimensat",
}
STRUCT_PATTERN_SYSCALLS = {
    "clone3",
}


class CapabilitySource(Protocol):
    def load_manifest(self) -> dict[str, Any]:
        ...

    def load_profile(self) -> dict[str, Any]:
        ...

    def load_manifest_index(self) -> dict[str, dict[str, Any]]:
        ...


class SequenceGate(Protocol):
    def relevant_output_lines(
        self,
        output_lines: list[str],
        *,
        target_syscalls: set[str] | None = None,
    ) -> list[str]:
        ...

    def classify_line(
        self,
        line: str,
        *,
        target_syscalls: set[str] | None = None,
    ) -> list[str]:
        ...


def apply_generation_profile(
    manifest: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    enabled_categories = set(profile["enabled_categories"])
    deferred_categories = dict(profile.get("deferred_categories", {}))
    deferred_syscalls = dict(profile.get("deferred_syscalls", {}))
    index: dict[str, dict[str, Any]] = {}
    for category_name, category in manifest["categories"].items():
        for syscall_name, entry in category["syscalls"].items():
            effective = {
                **entry,
                "category": category_name,
            }
            generation_enabled = bool(entry.get("generation_enabled", True))
            defer_reason = entry.get("defer_reason")
            if syscall_name in deferred_syscalls:
                generation_enabled = False
                defer_reason = deferred_syscalls[syscall_name]
            elif category_name not in enabled_categories:
                generation_enabled = False
                defer_reason = deferred_categories.get(category_name, "category_not_enabled")
            effective["generation_enabled"] = generation_enabled
            effective["defer_reason"] = defer_reason
            index[syscall_name] = effective
    return index


def load_manifest_index(
    manifest: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    if profile is not None:
        return apply_generation_profile(manifest, profile)
    index: dict[str, dict[str, Any]] = {}
    for category_name, category in manifest["categories"].items():
        for syscall_name, entry in category["syscalls"].items():
            index[syscall_name] = {
                **entry,
                "category": category_name,
            }
    return index


def parse_syscall_name(strace_line: str) -> str | None:
    match = SYSCALL_NAME_RE.match(strace_line)
    if match:
        return match.group("name")
    return None


def parse_sctrace_lines(stdout: str, stderr: str) -> list[str]:
    matched: list[str] = []
    for line in (stdout.splitlines() + stderr.splitlines()):
        stripped = line.strip()
        if stripped.startswith(UNSUPPORTED_PREFIX) or stripped.startswith(PARSE_ERROR_PREFIX):
            matched.append(stripped)
    return matched


def sctrace_command(scml_paths: list[Path], input_path: Path) -> list[str]:
    for candidate in (
        resolve_repo_path("third_party/asterinas/tools/sctrace/target/release/sctrace"),
        resolve_repo_path("third_party/asterinas/tools/sctrace/target/debug/sctrace"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return [str(candidate), *(str(path) for path in scml_paths), "--quiet", "--input", str(input_path)]
    installed = shutil.which("sctrace")
    if installed:
        return [installed, *(str(path) for path in scml_paths), "--quiet", "--input", str(input_path)]
    manifest_path = resolve_repo_path("third_party/asterinas/tools/sctrace/Cargo.toml")
    return [
        "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str(manifest_path),
        "--",
        *(str(path) for path in scml_paths),
        "--quiet",
        "--input",
        str(input_path),
    ]


class AsterinasSCMLSource:
    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or config()

    def load_manifest(self) -> dict[str, Any]:
        return load_json(self.cfg["compat_manifest_path"])

    def load_profile(self) -> dict[str, Any]:
        return load_json(self.cfg["generation_profile_path"])

    def load_manifest_index(self) -> dict[str, dict[str, Any]]:
        return load_manifest_index(self.load_manifest(), self.load_profile())

    def scml_files(self) -> list[Path]:
        scml_root = resolve_repo_path(self.cfg["preflight"]["scml_root"])
        return sorted(path for path in scml_root.rglob("*.scml") if path.is_file())

    def sctrace_command(self, scml_paths: list[Path], input_path: Path) -> list[str]:
        return sctrace_command(scml_paths, input_path)


class AsterinasSCMLGate:
    def __init__(
        self,
        *,
        cfg: dict[str, Any] | None = None,
        manifest_index: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.cfg = cfg or config()
        self.manifest_index = manifest_index or AsterinasSCMLSource(self.cfg).load_manifest_index()
        self.taxonomy = self.cfg["preflight"]["rejection_taxonomy"]

    def relevant_output_lines(
        self,
        output_lines: list[str],
        *,
        target_syscalls: set[str] | None = None,
    ) -> list[str]:
        if target_syscalls is None:
            return output_lines
        return [
            line
            for line in output_lines
            if not line.startswith(UNSUPPORTED_PREFIX)
            or parse_syscall_name(line[len(UNSUPPORTED_PREFIX) :].strip()) in target_syscalls
        ]

    def parse_sctrace_lines(self, stdout: str, stderr: str) -> list[str]:
        return parse_sctrace_lines(stdout, stderr)

    def classify_reason_from_entry(self, strace_line: str, entry: dict[str, Any]) -> str:
        for key, values in entry.items():
            if not key.startswith("unsupported_") or not isinstance(values, list):
                continue
            field_name = key[len("unsupported_") :]
            if any(value and value in strace_line for value in values):
                return self.taxonomy[FIELD_REASON_HINTS.get(field_name, "unsupported_flag_pattern")]
        syscall_name = entry["name"]
        if syscall_name in STRUCT_PATTERN_SYSCALLS:
            return self.taxonomy["unsupported_struct_pattern"]
        if syscall_name in PATH_PATTERN_SYSCALLS:
            return self.taxonomy["unsupported_path_pattern"]
        return self.taxonomy["unsupported_flag_pattern"]

    def classify_line(
        self,
        line: str,
        *,
        target_syscalls: set[str] | None = None,
    ) -> list[str]:
        if line.startswith(PARSE_ERROR_PREFIX):
            return [self.taxonomy["scml_parser_gap"]]
        if not line.startswith(UNSUPPORTED_PREFIX):
            return [self.taxonomy["scml_parser_gap"]]
        strace_line = line[len(UNSUPPORTED_PREFIX) :].strip()
        syscall_name = parse_syscall_name(strace_line)
        if syscall_name is None:
            return [self.taxonomy["scml_parser_gap"]]
        if target_syscalls is not None and syscall_name not in target_syscalls:
            return []
        entry = self.manifest_index.get(syscall_name)
        if entry is None:
            return [self.taxonomy["syscall_not_in_manifest"]]
        if not entry.get("generation_enabled", True):
            return [self.taxonomy["deferred_category"]]
        return [self.classify_reason_from_entry(strace_line, entry)]
