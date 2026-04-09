#!/usr/bin/env python3
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, dump_json, dump_jsonl, ensure_dir, load_json
from orchestrator.models import EligibleProgram


def base_syscall_name(full_name: str) -> str:
    return full_name.split("$", 1)[0]


def classify_rejection(meta: dict[str, object], cfg: dict[str, object]) -> list[str]:
    reasons: list[str] = []
    allow = set(cfg["allowlist"]["syscalls"])
    deny_prefixes = tuple(cfg["allowlist"]["deny_prefixes"])
    deny_contains = tuple(cfg["allowlist"]["deny_name_contains"])
    pseudo_prefix = cfg["allowlist"]["pseudo_prefix"]
    threading_prefixes = tuple(cfg["allowlist"]["threading_sensitive_prefixes"])

    if meta["uses_pseudo_syscalls"]:
        reasons.append("pseudo_syscall")

    for full_name in meta["full_syscall_list"]:
        name = base_syscall_name(full_name)
        if name.startswith(pseudo_prefix):
            reasons.append("pseudo_syscall")
        if name not in allow:
            reasons.append("non_allowlisted_syscall")
        if full_name != name and full_name not in allow:
            reasons.append("non_allowlisted_variant")
        if name.startswith(deny_prefixes):
            if name.startswith(("mount", "umount", "pivot_root", "setns", "unshare")):
                reasons.append("privileged_or_mount_path")
            else:
                reasons.append("complex_network_path")
        if name.startswith(threading_prefixes):
            reasons.append("threading_sensitive")
        if any(item in full_name for item in deny_contains):
            reasons.append("complex_network_path")

    if meta["uses_threading_sensitive_features"]:
        reasons.append("threading_sensitive")

    deduped = []
    seen = set()
    for reason in reasons:
        if reason not in seen:
            deduped.append(reason)
            seen.add(reason)
    return deduped


def main() -> None:
    cfg = config()
    meta_root = ensure_dir(cfg["paths"]["corpus_meta"])
    eligible_rows: list[dict[str, object]] = []
    rejection_counts = Counter()
    total = 0

    for meta_path in sorted(Path(meta_root).glob("*.json")):
        total += 1
        meta = load_json(meta_path)
        reasons = classify_rejection(meta, cfg)
        if reasons:
            for reason in reasons:
                rejection_counts[reason] += 1
            continue
        row = EligibleProgram(
            program_id=meta["program_id"],
            workflow="baseline",
            reason=["allowed_syscalls_only", "no_pseudo", "single_thread_safe"],
            normalized_path=meta["normalized_path"],
            meta_path=str(meta_path),
        )
        eligible_rows.append(row.to_dict())

    eligible_rows.sort(key=lambda row: row["program_id"])
    dump_jsonl(cfg["paths"]["eligible_file"], eligible_rows)
    dump_json(
        "reports/baseline/filter-summary.json",
        {
            "total_meta": total,
            "eligible": len(eligible_rows),
            "rejected": total - len(eligible_rows),
            "rejection_counts": dict(rejection_counts),
        },
    )


if __name__ == "__main__":
    main()
