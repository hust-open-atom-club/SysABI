#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.common import config, dump_json, ensure_dir, read_text, resolve_repo_path, sha256_text, temp_dir, write_text
from orchestrator.models import ProgramMeta
from orchestrator.syzkaller import inspect_program


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--source-type", choices=["seed", "generated", "crashlog"], default="generated")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def candidate_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.rglob("*") if path.is_file())


def extract_crashlog_program(raw: str) -> str:
    lines = raw.splitlines()
    block: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        looks_like_call = "(" in stripped and stripped.endswith(")") and not stripped.startswith("[")
        looks_like_assignment = stripped.startswith("r") and "=" in stripped and looks_like_call
        if looks_like_call or looks_like_assignment:
            in_block = True
            block.append(stripped)
            continue
        if in_block and not stripped:
            break
    return "\n".join(block).strip()


def preprocess_text(raw: str, source_type: str) -> str:
    candidate = extract_crashlog_program(raw) if source_type == "crashlog" else raw
    lines: list[str] = []
    for line in candidate.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip() + "\n" if lines else ""


def store_rejected(path: Path, source_type: str, prepared_text: str, reason: str, detail: str) -> None:
    cfg = config()
    rejected_root = ensure_dir(cfg["paths"]["corpus_rejected"])
    reject_id = sha256_text(f"{path}:{prepared_text}:{reason}")
    rejected_program = rejected_root / f"{reject_id}.syz"
    rejected_meta = rejected_root / f"{reject_id}.json"
    write_text(rejected_program, prepared_text)
    dump_json(
        rejected_meta,
        {
            "reject_id": reject_id,
            "source": source_type,
            "original_path": str(path),
            "reason": reason,
            "detail": detail,
        },
    )


def inspect_candidate(file_path: Path, source_type: str, strict: bool) -> dict[str, object]:
    raw_input = read_text(file_path)
    prepared_text = preprocess_text(raw_input, source_type)
    if not prepared_text.strip():
        return {
            "kind": "rejected",
            "path": file_path,
            "prepared_text": prepared_text,
            "reason": "parse_error",
            "detail": "empty_candidate_program",
        }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".syz", delete=False, dir=temp_dir()) as handle:
        handle.write(prepared_text)
        temp_path = Path(handle.name)
    try:
        try:
            info = inspect_program(temp_path, strict=strict)
        except Exception as exc:
            return {
                "kind": "rejected",
                "path": file_path,
                "prepared_text": prepared_text,
                "reason": "parse_error",
                "detail": str(exc),
            }
        return {
            "kind": "accepted",
            "path": file_path,
            "prepared_text": prepared_text,
            "info": info,
        }
    finally:
        temp_path.unlink(missing_ok=True)


def main() -> None:
    args = parse_args()
    cfg = config()
    input_dir = resolve_repo_path(args.input_dir)
    files = candidate_files(input_dir)
    summaries = Counter()

    raw_root = ensure_dir(cfg["paths"]["corpus_raw"])
    normalized_root = ensure_dir(cfg["paths"]["corpus_normalized"])
    meta_root = ensure_dir(cfg["paths"]["corpus_meta"])

    max_workers = min(32, (os.cpu_count() or 4) * 2)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(inspect_candidate, file_path, args.source_type, args.strict) for file_path in files
        ]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]

    for result in sorted(results, key=lambda item: str(item["path"])):
        file_path = result["path"]
        prepared_text = result["prepared_text"]
        if result["kind"] == "rejected":
            store_rejected(file_path, args.source_type, prepared_text, result["reason"], result["detail"])
            summaries["rejected"] += 1
            summaries[result["reason"]] += 1
            continue

        info = result["info"]
        try:
            program_id = info["program_id"]
            raw_path = raw_root / f"{program_id}.syz"
            normalized_path = normalized_root / f"{program_id}.syz"
            meta_path = meta_root / f"{program_id}.json"

            if meta_path.exists():
                existing = json.loads(meta_path.read_text(encoding="utf-8"))
                duplicates = existing.setdefault("duplicate_inputs", [])
                duplicates.append(str(file_path))
                dump_json(meta_path, existing)
                summaries["duplicate"] += 1
                continue

            write_text(raw_path, prepared_text)
            write_text(normalized_path, info["normalized_syz"])
            meta = ProgramMeta(
                program_id=program_id,
                source=args.source_type,
                target_os=info["target_os"],
                arch=info["arch"],
                syscall_list=info["syscall_list"],
                full_syscall_list=info["full_syscall_list"],
                resource_classes=info["resource_classes"],
                uses_pseudo_syscalls=info["uses_pseudo_syscalls"],
                uses_threading_sensitive_features=info["uses_threading_sensitive_features"],
                original_path=str(file_path),
                raw_path=str(raw_path),
                normalized_path=str(normalized_path),
                call_count=int(info["call_count"]),
            )
            dump_json(meta_path, meta.to_dict())
            summaries["imported"] += 1
        except Exception as exc:
            store_rejected(file_path, args.source_type, prepared_text, "parse_error", str(exc))
            summaries["rejected"] += 1
            summaries["parse_error"] += 1

    dump_json(
        "reports/baseline/import-summary.json",
        {
            "input_dir": str(input_dir),
            "source_type": args.source_type,
            "total_files": len(files),
            "imported": summaries["imported"],
            "duplicate": summaries["duplicate"],
            "rejected": summaries["rejected"],
            "reason_counts": dict(summaries),
        },
    )


if __name__ == "__main__":
    main()
