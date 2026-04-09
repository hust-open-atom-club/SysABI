#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import dump_json, repo_root, resolve_repo_path


DEFAULT_TARGET = "asterinas"
DEFAULT_REPO_DIR = "third_party/asterinas"
DEFAULT_SCML_ROOT = (
    "third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage"
)
DEFAULT_SYZKALLER_ROOT = "third_party/syzkaller/sys/linux"
DEFAULT_OUTPUT = "compat_specs/asterinas/scml-manifest.json"

SECTION_HEADING_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$")
SECTION_SYSCALL_RE = re.compile(r"`([^`]+)`")
SYSCALL_RULE_RE = re.compile(r"^([A-Za-z0-9_]+)\s*\(")
GROUP_LABEL_RE = re.compile(
    r"^(?P<prefix>Silently-ignored|Ignored|Partially-supported|Unsupported)\s+(?P<field>.+):$"
)
SYZKALLER_SYSCALL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_]+(?:\$[A-Za-z0-9_]+)?)\s*\(")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--repo-dir", default=DEFAULT_REPO_DIR)
    parser.add_argument("--source-root", default=DEFAULT_SCML_ROOT)
    parser.add_argument("--syzkaller-root", default=DEFAULT_SYZKALLER_ROOT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    return parser.parse_args()


def relative_path(path: Path) -> str:
    return path.resolve().relative_to(repo_root()).as_posix()


def current_revision(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "failed to resolve target revision")
    return result.stdout.strip()


def normalize_field_name(raw: str) -> str:
    field = raw.strip().lower()
    field = field.replace("&", "and")
    field = re.sub(r"[^a-z0-9]+", "_", field)
    return field.strip("_")


def normalize_group_prefix(raw: str) -> str:
    prefix = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if prefix.startswith("silently_"):
        prefix = prefix[len("silently_") :]
    if prefix == "partially_supported":
        return "partial"
    return prefix


def unique_preserve(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def normalize_bullet_items(raw: str) -> list[str]:
    tokens = [item.strip() for item in INLINE_CODE_RE.findall(raw) if item.strip()]
    if tokens:
        return unique_preserve(tokens)
    item = raw.strip().strip("`")
    if not item:
        return []
    return [item]


def section_template(title: str) -> dict[str, Any]:
    return {
        "heading": title,
        "notes": [],
        "ignored": defaultdict(list),
        "partial": defaultdict(list),
        "unsupported": defaultdict(list),
    }


def finalize_section(section: dict[str, Any] | None) -> dict[str, Any] | None:
    if section is None:
        return None
    finalized: dict[str, Any] = {
        "heading": section["heading"],
        "notes": unique_preserve(section["notes"]),
    }
    for bucket in ("ignored", "partial", "unsupported"):
        finalized[bucket] = {
            key: unique_preserve(values)
            for key, values in sorted(section[bucket].items())
        }
    return finalized


def parse_readme_sections(readme_path: Path) -> dict[str, list[dict[str, Any]]]:
    syscall_sections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    current_names: list[str] = []
    current_section: dict[str, Any] | None = None
    current_bucket: str | None = None
    current_field: str | None = None
    in_code_block = False

    for raw_line in readme_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        heading_match = SECTION_HEADING_RE.match(stripped)
        if heading_match:
            finalized = finalize_section(current_section)
            if finalized is not None:
                for syscall_name in current_names:
                    syscall_sections[syscall_name].append(deepcopy(finalized))
            current_names = SECTION_SYSCALL_RE.findall(heading_match.group("title"))
            current_section = section_template(heading_match.group("title"))
            current_bucket = None
            current_field = None
            continue

        if current_section is None:
            continue

        label_match = GROUP_LABEL_RE.match(stripped)
        if label_match:
            current_bucket = normalize_group_prefix(label_match.group("prefix"))
            current_field = normalize_field_name(label_match.group("field"))
            continue

        if stripped.startswith("* ") and current_bucket and current_field:
            for item in normalize_bullet_items(stripped[2:]):
                current_section[current_bucket][current_field].append(item)
            continue

        if not stripped:
            current_bucket = None
            current_field = None
            continue

        if stripped.startswith("For more information,"):
            current_bucket = None
            current_field = None
            continue

        if stripped.startswith("Supported functionality"):
            continue

        current_section["notes"].append(stripped)

    finalized = finalize_section(current_section)
    if finalized is not None:
        for syscall_name in current_names:
            syscall_sections[syscall_name].append(deepcopy(finalized))

    return dict(syscall_sections)


def extract_syscall_names(scml_path: Path) -> list[str]:
    names: list[str] = []
    for raw_line in scml_path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        match = SYSCALL_RULE_RE.match(stripped)
        if match:
            names.append(match.group(1))
    return unique_preserve(names)


def merge_bucket(sections: list[dict[str, Any]], bucket_name: str) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = defaultdict(list)
    for section in sections:
        for field, values in section.get(bucket_name, {}).items():
            merged[field].extend(values)
    return {
        field: unique_preserve(values)
        for field, values in sorted(merged.items())
    }


def bucket_aliases(
    bucket_name: str,
    fields: dict[str, list[str]],
    *,
    common_fields: tuple[str, ...] = ("flags",),
) -> dict[str, list[str]]:
    aliases = {
        f"{bucket_name}_{field}": list(values)
        for field, values in fields.items()
    }
    for field in common_fields:
        aliases.setdefault(f"{bucket_name}_{field}", list(fields.get(field, [])))
    return aliases


def support_tier(
    source_files: list[str],
    ignored: dict[str, list[str]],
    partial: dict[str, list[str]],
    unsupported: dict[str, list[str]],
) -> str:
    if partial or ignored or unsupported:
        return "partial"
    if any(not path.endswith("fully_covered.scml") for path in source_files):
        return "partial"
    return "full"


def parse_syzkaller_definition(line: str) -> tuple[str, bool] | None:
    stripped = line.strip()
    commented = stripped.startswith("#")
    if commented:
        stripped = stripped[1:].strip()
    if not stripped:
        return None
    match = SYZKALLER_SYSCALL_RE.match(stripped)
    if not match:
        return None
    return match.group("name"), commented


def syscall_base_name(name: str) -> str:
    return name.split("$", 1)[0]


def analyze_syzkaller_descriptions(syzkaller_root: Path) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    if not syzkaller_root.exists():
        return index

    def ensure_entry(base_name: str) -> dict[str, Any]:
        return index.setdefault(
            base_name,
            {
                "active_base_names": set(),
                "active_variant_names": set(),
                "helper_names": set(),
                "commented_names": set(),
                "disabled_names": set(),
            },
        )

    for path in sorted(syzkaller_root.glob("*.txt")):
        if path.name.startswith("auto"):
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            parsed = parse_syzkaller_definition(raw_line)
            if parsed is None:
                continue
            full_name, commented = parsed
            base_name = syscall_base_name(full_name)
            entry = ensure_entry(base_name)
            if commented:
                entry["commented_names"].add(full_name)
                continue

            if full_name.startswith("syz_"):
                helper_base = syscall_base_name(full_name[len("syz_") :])
                helper_entry = ensure_entry(helper_base)
                helper_entry["helper_names"].add(full_name)
                continue

            disabled = "disabled" in raw_line
            if disabled:
                entry["disabled_names"].add(full_name)
                continue
            if "$" in full_name:
                entry["active_variant_names"].add(full_name)
            else:
                entry["active_base_names"].add(full_name)

    finalized: dict[str, dict[str, Any]] = {}
    for base_name, entry in index.items():
        finalized[base_name] = {
            "syzkaller_base_available": bool(entry["active_base_names"]),
            "syzkaller_variant_available": bool(entry["active_variant_names"]),
            "helper_available": bool(entry["helper_names"]),
            "has_disabled_definition": bool(entry["disabled_names"]),
            "has_commented_definition": bool(entry["commented_names"]),
        }
    return finalized


def generator_metadata(
    syscall_name: str,
    syzkaller_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    entry = syzkaller_index.get(syscall_name)
    if entry is None:
        return {
            "syzkaller_base_available": False,
            "syzkaller_variant_available": False,
            "generator_class": "unavailable",
            "generator_gap_reason": "missing_description",
        }

    base_available = bool(entry["syzkaller_base_available"])
    variant_available = bool(entry["syzkaller_variant_available"])
    helper_available = bool(entry["helper_available"])
    disabled = bool(entry["has_disabled_definition"] or entry["has_commented_definition"])

    if base_available:
        return {
            "syzkaller_base_available": True,
            "syzkaller_variant_available": variant_available,
            "generator_class": "base_only",
            "generator_gap_reason": "none",
        }
    if variant_available:
        return {
            "syzkaller_base_available": False,
            "syzkaller_variant_available": True,
            "generator_class": "variant_only",
            "generator_gap_reason": "missing_base_definition",
        }
    if helper_available:
        return {
            "syzkaller_base_available": False,
            "syzkaller_variant_available": False,
            "generator_class": "helper_only",
            "generator_gap_reason": "disabled_in_syzkaller" if disabled else "pseudo_only",
        }
    return {
        "syzkaller_base_available": False,
        "syzkaller_variant_available": False,
        "generator_class": "unavailable",
        "generator_gap_reason": "disabled_in_syzkaller" if disabled else "missing_description",
    }


def build_manifest(
    *,
    target: str,
    repo_dir: Path,
    source_root: Path,
    syzkaller_root: Path | None = None,
) -> dict[str, Any]:
    category_meta: dict[str, dict[str, Any]] = {}
    total_scml_files = 0
    total_readmes = 0
    total_syscalls = 0
    syzkaller_index = analyze_syzkaller_descriptions(
        syzkaller_root or resolve_repo_path(DEFAULT_SYZKALLER_ROOT)
    )

    for category_dir in sorted(path for path in source_root.iterdir() if path.is_dir()):
        category_name = category_dir.name
        readme_path = category_dir / "README.md"
        readme_sections = parse_readme_sections(readme_path) if readme_path.exists() else {}
        scml_files = sorted(category_dir.glob("*.scml"))
        syscall_sources: dict[str, list[str]] = defaultdict(list)
        for scml_path in scml_files:
            total_scml_files += 1
            for syscall_name in extract_syscall_names(scml_path):
                syscall_sources[syscall_name].append(relative_path(scml_path))

        syscalls: dict[str, Any] = {}
        for syscall_name, source_files in sorted(syscall_sources.items()):
            sections = readme_sections.get(syscall_name, [])
            ignored = merge_bucket(sections, "ignored")
            partial = merge_bucket(sections, "partial")
            unsupported = merge_bucket(sections, "unsupported")
            notes = unique_preserve(
                [
                    note
                    for section in sections
                    for note in section.get("notes", [])
                ]
            )
            headings = unique_preserve(
                [
                    section["heading"]
                    for section in sections
                    if section.get("heading")
                ]
            )
            syscall_entry = {
                "name": syscall_name,
                "category": category_name,
                "support_tier": support_tier(source_files, ignored, partial, unsupported),
                "preflight_required": True,
                "generation_enabled": True,
                "defer_reason": None,
                "source_scml_files": sorted(source_files),
                "readme_path": relative_path(readme_path) if readme_path.exists() else None,
                "readme_headings": headings,
                "ignored": ignored,
                "partial": partial,
                "unsupported": unsupported,
                "notes": notes,
            }
            syscall_entry.update(generator_metadata(syscall_name, syzkaller_index))
            syscall_entry.update(bucket_aliases("ignored", ignored))
            syscall_entry.update(bucket_aliases("partial", partial))
            syscall_entry.update(bucket_aliases("unsupported", unsupported))
            syscalls[syscall_name] = syscall_entry

        total_readmes += 1 if readme_path.exists() else 0
        total_syscalls += len(syscalls)
        category_meta[category_name] = {
            "name": category_name,
            "readme_path": relative_path(readme_path) if readme_path.exists() else None,
            "source_files": [relative_path(path) for path in scml_files],
            "syscall_count": len(syscalls),
            "syscalls": syscalls,
        }

    return {
        "target": target,
        "source_type": "scml",
        "source_revision": current_revision(repo_dir),
        "source_root": relative_path(source_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "category_count": len(category_meta),
            "readme_count": total_readmes,
            "scml_file_count": total_scml_files,
            "total_syscalls": total_syscalls,
        },
        "categories": category_meta,
    }


def main() -> None:
    args = parse_args()
    repo_dir = resolve_repo_path(args.repo_dir)
    source_root = resolve_repo_path(args.source_root)
    syzkaller_root = resolve_repo_path(args.syzkaller_root)
    if not repo_dir.exists():
        raise SystemExit(f"missing repo dir: {repo_dir}")
    if not source_root.exists():
        raise SystemExit(f"missing SCML source root: {source_root}")
    manifest = build_manifest(
        target=args.target,
        repo_dir=repo_dir,
        source_root=source_root,
        syzkaller_root=syzkaller_root,
    )
    dump_json(args.output, manifest)


if __name__ == "__main__":
    main()
