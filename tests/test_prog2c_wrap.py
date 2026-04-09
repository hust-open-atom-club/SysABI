from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.prog2c_wrap import build_one, load_cached_build_result


class Prog2CWrapCacheTests(unittest.TestCase):
    def write_file(self, path: Path, content: str, *, mtime_ns: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        os.utime(path, ns=(mtime_ns, mtime_ns))

    def test_load_cached_build_result_rejects_stale_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_root = root / "build" / "program"
            source = root / "input.syz"
            testcase_c = build_root / "testcase.c"
            testcase_instrumented = build_root / "testcase.instrumented.c"
            testcase_bin = build_root / "testcase.bin"
            candidate_bin = build_root / "testcase.candidate.bin"
            base = 1_000_000_000
            source_content = "syz"
            self.write_file(testcase_c, "c", mtime_ns=base + 10)
            self.write_file(testcase_instrumented, "instrumented", mtime_ns=base + 10)
            self.write_file(testcase_bin, "bin", mtime_ns=base + 10)
            self.write_file(candidate_bin, "candidate", mtime_ns=base + 10)
            self.write_file(
                build_root / "build-result.json",
                json.dumps(
                    {
                        "status": "ok",
                        "testcase_c": str(testcase_c),
                        "testcase_instrumented_c": str(testcase_instrumented),
                        "testcase_bin": str(testcase_bin),
                        "candidate_testcase_bin": str(candidate_bin),
                        "input_fingerprints": [
                            {
                                "path": str(source),
                                "sha256": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
                            }
                        ],
                    }
                ),
                mtime_ns=base + 10,
            )
            self.write_file(source, "changed", mtime_ns=base + 20)
            self.assertIsNone(
                load_cached_build_result(
                    build_root,
                    input_paths=[source],
                    should_build_candidate=True,
                )
            )

    def test_load_cached_build_result_rejects_changed_tool_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_root = root / "build" / "program"
            source = root / "input.syz"
            testcase_c = build_root / "testcase.c"
            testcase_instrumented = build_root / "testcase.instrumented.c"
            testcase_bin = build_root / "testcase.bin"
            candidate_bin = build_root / "testcase.candidate.bin"
            base = 1_500_000_000
            source_content = "syz"
            cached_fingerprints = [
                {
                    "path": str(source),
                    "sha256": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
                },
                {
                    "path": "/tmp/syz-prog2c",
                    "sha256": "old-tool-hash",
                },
            ]
            expected_fingerprints = [
                {
                    "path": str(source),
                    "sha256": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
                },
                {
                    "path": "/tmp/syz-prog2c",
                    "sha256": "new-tool-hash",
                },
            ]
            self.write_file(source, source_content, mtime_ns=base)
            self.write_file(testcase_c, "c", mtime_ns=base + 10)
            self.write_file(testcase_instrumented, "instrumented", mtime_ns=base + 10)
            self.write_file(testcase_bin, "bin", mtime_ns=base + 10)
            self.write_file(candidate_bin, "candidate", mtime_ns=base + 10)
            self.write_file(
                build_root / "build-result.json",
                json.dumps(
                    {
                        "status": "ok",
                        "testcase_c": str(testcase_c),
                        "testcase_instrumented_c": str(testcase_instrumented),
                        "testcase_bin": str(testcase_bin),
                        "candidate_testcase_bin": str(candidate_bin),
                        "input_fingerprints": cached_fingerprints,
                    }
                ),
                mtime_ns=base + 10,
            )

            self.assertIsNone(
                load_cached_build_result(
                    build_root,
                    input_paths=[source],
                    should_build_candidate=True,
                    expected_fingerprints=expected_fingerprints,
                )
            )

    def test_build_one_reuses_fresh_cached_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            build_root = root / "build" / "program"
            source = root / "input.syz"
            dep = root / "dep.txt"
            testcase_c = build_root / "testcase.c"
            testcase_instrumented = build_root / "testcase.instrumented.c"
            testcase_bin = build_root / "testcase.bin"
            candidate_bin = build_root / "testcase.candidate.bin"
            base = 2_000_000_000
            source_content = "syz"
            dep_content = "dep"
            cached_fingerprints = [
                {
                    "path": str(source),
                    "sha256": hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
                },
                {
                    "path": str(dep),
                    "sha256": hashlib.sha256(dep_content.encode("utf-8")).hexdigest(),
                },
                {
                    "path": "/tmp/syzkaller::git-revision",
                    "sha256": "tool-hash",
                },
            ]
            self.write_file(source, source_content, mtime_ns=base)
            self.write_file(dep, dep_content, mtime_ns=base)
            self.write_file(testcase_c, "c", mtime_ns=base + 10)
            self.write_file(testcase_instrumented, "instrumented", mtime_ns=base + 10)
            self.write_file(testcase_bin, "bin", mtime_ns=base + 10)
            self.write_file(candidate_bin, "candidate", mtime_ns=base + 10)
            self.write_file(
                build_root / "build-result.json",
                json.dumps(
                    {
                        "program_id": "program",
                        "normalized_path": str(source),
                        "status": "ok",
                        "testcase_c": str(testcase_c),
                        "testcase_instrumented_c": str(testcase_instrumented),
                        "testcase_bin": str(testcase_bin),
                        "candidate_testcase_bin": str(candidate_bin),
                        "input_fingerprints": cached_fingerprints,
                    }
                ),
                mtime_ns=base + 10,
            )

            entry = {"program_id": "program", "normalized_path": str(source)}
            cfg = {"paths": {"build_dir": str(root / "build")}}
            profiles = {"candidate": {"kind": "command", "binary_name": "testcase.candidate.bin"}}
            with patch("tools.prog2c_wrap.config", return_value=cfg), patch(
                "tools.prog2c_wrap.runner_profiles",
                return_value=profiles,
            ), patch(
                "tools.prog2c_wrap.build_input_paths",
                return_value=[source, dep],
            ), patch(
                "tools.prog2c_wrap.build_input_fingerprints",
                return_value=cached_fingerprints,
            ), patch(
                "tools.prog2c_wrap.build_prog2c",
                side_effect=AssertionError("cache miss unexpectedly rebuilt testcase"),
            ):
                result = build_one(entry)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["program_id"], "program")
        self.assertEqual(result["testcase_bin"], str(testcase_bin))
        self.assertEqual(result["candidate_testcase_bin"], str(candidate_bin))


if __name__ == "__main__":
    unittest.main()
