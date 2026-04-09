from __future__ import annotations

import re
import unittest

from analyzer.classify import classify_result
from analyzer.compare import compare_canonical
from analyzer.normalize import canonicalize
from orchestrator.common import config
from orchestrator.stability import canonical_trace_hash
from orchestrator.vm_runner import classify_process_returncode
from tools.filter_corpus import classify_rejection
from tools.prog2c_wrap import instrument_source
from tools.reduce_case import map_event_index_to_program_call


class BaselinePipelineTests(unittest.TestCase):
    def test_instrument_source_wraps_all_syscalls(self) -> None:
        source = """
#include <sys/syscall.h>
int main(void) {
\tsyscall(__NR_openat, 1, 2, 3, 4);
\tres = syscall(__NR_close, 5);
\treturn 0;
}
"""
        instrumented, wrapped = instrument_source(source)
        self.assertEqual(wrapped, 2)
        self.assertIn('traced_syscall("openat"', instrumented)
        self.assertIn('traced_syscall("close"', instrumented)
        self.assertIsNone(re.search(r"(?<!traced_)syscall\s*\(", instrumented))

    def test_filter_rejects_complex_network_variants(self) -> None:
        meta = {
            "uses_pseudo_syscalls": False,
            "uses_threading_sensitive_features": False,
            "syscall_list": ["socketpair"],
            "full_syscall_list": ["socketpair$inet"],
        }
        reasons = classify_rejection(meta, config())
        self.assertIn("complex_network_path", reasons)

    def test_filter_rejects_non_allowlisted_specializations(self) -> None:
        meta = {
            "uses_pseudo_syscalls": False,
            "uses_threading_sensitive_features": False,
            "syscall_list": ["openat", "read"],
            "full_syscall_list": ["openat$fuse", "read$FUSE"],
        }
        reasons = classify_rejection(meta, config())
        self.assertIn("non_allowlisted_variant", reasons)
        self.assertNotIn("non_allowlisted_syscall", reasons)

    def test_canonical_hash_ignores_duration_noise(self) -> None:
        trace_a = {
            "program_id": "p",
            "side": "reference",
            "event_count": 1,
            "events": [
                {
                    "index": 0,
                    "syscall_name": "close",
                    "syscall_number": 3,
                    "args": ["fd#0"],
                    "return_value": 0,
                    "errno": 0,
                    "duration_ns": 100,
                    "outputs": [],
                }
            ],
            "final_state": {"files": []},
            "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
        }
        trace_b = {
            **trace_a,
            "events": [{**trace_a["events"][0], "duration_ns": 99999}],
        }
        self.assertEqual(canonical_trace_hash(trace_a), canonical_trace_hash(trace_b))

    def test_canonicalize_and_compare_fd_differences_as_equivalent(self) -> None:
        base_raw = {
            "program_id": "p",
            "side": "reference",
            "run_id": "r",
            "status": "ok",
            "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
        }
        raw_a = {
            **base_raw,
            "events": [
                {
                    "event_index": 0,
                    "side": "reference",
                    "syscall_name": "openat",
                    "syscall_number": 257,
                    "args": [-100, 0x200000000000, 0, 0, 0, 0],
                    "return_value": 3,
                    "errno": 0,
                    "start_ns": 1,
                    "end_ns": 2,
                    "timed_out": False,
                    "outputs": [],
                },
                {
                    "event_index": 1,
                    "side": "reference",
                    "syscall_name": "close",
                    "syscall_number": 3,
                    "args": [3, 0, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "start_ns": 3,
                    "end_ns": 4,
                    "timed_out": False,
                    "outputs": [],
                },
            ],
        }
        raw_b = {
            **base_raw,
            "side": "candidate",
            "events": [
                {
                    "event_index": 0,
                    "side": "candidate",
                    "syscall_name": "openat",
                    "syscall_number": 257,
                    "args": [-100, 0x200000000100, 0, 0, 0, 0],
                    "return_value": 7,
                    "errno": 0,
                    "start_ns": 5,
                    "end_ns": 6,
                    "timed_out": False,
                    "outputs": [],
                },
                {
                    "event_index": 1,
                    "side": "candidate",
                    "syscall_name": "close",
                    "syscall_number": 3,
                    "args": [7, 0, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "start_ns": 7,
                    "end_ns": 9,
                    "timed_out": False,
                    "outputs": [],
                },
            ],
        }
        external_state = {"files": []}
        canonical_a = canonicalize(raw_a, external_state)
        canonical_b = canonicalize(raw_b, external_state)
        comparison = compare_canonical(canonical_a, canonical_b)
        self.assertTrue(comparison["equivalent"])

    def test_canonicalize_preserves_large_literal_offsets(self) -> None:
        base_raw = {
            "program_id": "p",
            "side": "reference",
            "run_id": "r",
            "status": "ok",
            "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
        }
        raw_a = {
            **base_raw,
            "events": [
                {
                    "event_index": 0,
                    "side": "reference",
                    "syscall_name": "pread64",
                    "syscall_number": 17,
                    "args": [3, 0x200000000100, 8, 0x100000000, 0, 0],
                    "return_value": 4,
                    "errno": 0,
                    "start_ns": 1,
                    "end_ns": 2,
                    "timed_out": False,
                    "outputs": [],
                }
            ],
        }
        raw_b = {
            **base_raw,
            "side": "candidate",
            "events": [
                {
                    "event_index": 0,
                    "side": "candidate",
                    "syscall_name": "pread64",
                    "syscall_number": 17,
                    "args": [7, 0x200000000200, 8, 0x200000000, 0, 0],
                    "return_value": 4,
                    "errno": 0,
                    "start_ns": 3,
                    "end_ns": 4,
                    "timed_out": False,
                    "outputs": [],
                }
            ],
        }
        external_state = {"files": []}
        canonical_a = canonicalize(raw_a, external_state)
        canonical_b = canonicalize(raw_b, external_state)
        self.assertEqual(canonical_a["events"][0]["args"][0], "fd#0")
        self.assertEqual(canonical_b["events"][0]["args"][0], "fd#0")
        self.assertEqual(canonical_a["events"][0]["args"][1], "addr#0")
        self.assertEqual(canonical_b["events"][0]["args"][1], "addr#0")
        self.assertEqual(canonical_a["events"][0]["args"][3], 0x100000000)
        self.assertEqual(canonical_b["events"][0]["args"][3], 0x200000000)
        comparison = compare_canonical(canonical_a, canonical_b)
        self.assertFalse(comparison["equivalent"])
        self.assertFalse(comparison["noise_only"])
        self.assertEqual(comparison["first_divergence_index"], 0)

    def test_compare_treats_output_mismatch_as_semantic(self) -> None:
        base = {
            "program_id": "p",
            "side": "reference",
            "event_count": 1,
            "events": [
                {
                    "index": 0,
                    "syscall_name": "read",
                    "syscall_number": 0,
                    "args": ["fd#0", "addr#0", 8, 0, 0, 0],
                    "return_value": 4,
                    "errno": 0,
                    "duration_ns": 100,
                    "outputs": [
                        {
                            "label": "buf",
                            "arg_index": 1,
                            "length": 4,
                            "preview_hex": "41414141",
                            "sha256": "a",
                        }
                    ],
                }
            ],
            "final_state": {"files": []},
            "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
        }
        candidate = {
            **base,
            "side": "candidate",
            "events": [
                {
                    **base["events"][0],
                    "outputs": [
                        {
                            "label": "buf",
                            "arg_index": 1,
                            "length": 4,
                            "preview_hex": "42424242",
                            "sha256": "b",
                        }
                    ],
                }
            ],
        }
        comparison = compare_canonical(base, candidate)
        self.assertFalse(comparison["equivalent"])
        self.assertFalse(comparison["noise_only"])
        self.assertEqual(comparison["first_divergence_index"], 0)

    def test_canonicalize_masks_volatile_stat_output_fields(self) -> None:
        raw_a = {
            "program_id": "p",
            "side": "reference",
            "run_id": "r0",
            "status": "ok",
            "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            "events": [
                {
                    "event_index": 0,
                    "side": "reference",
                    "syscall_name": "fstat",
                    "syscall_number": 5,
                    "args": [3, 0x200000000000, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "start_ns": 1,
                    "end_ns": 2,
                    "timed_out": False,
                    "outputs": [
                        {
                            "label": "stat",
                            "arg_index": 1,
                            "length": 144,
                            "preview_hex": "10000000000000002b0000000000000001000000000000008001000000000000",
                            "sha256": "stable-stat",
                        }
                    ],
                }
            ],
        }
        raw_b = {
            **raw_a,
            "run_id": "r1",
            "events": [
                {
                    **raw_a["events"][0],
                    "start_ns": 4,
                    "end_ns": 9,
                    "outputs": [
                        {
                            "label": "stat",
                            "arg_index": 1,
                            "length": 144,
                            "preview_hex": "1000000000000000deadbeefdeadbeef01000000000000008001000000000000",
                            "sha256": "stable-stat",
                        }
                    ],
                }
            ],
        }
        external_state = {"files": []}
        canonical_a = canonicalize(raw_a, external_state)
        canonical_b = canonicalize(raw_b, external_state)
        self.assertEqual(canonical_a["events"][0]["outputs"], canonical_b["events"][0]["outputs"])
        self.assertEqual(canonical_trace_hash(canonical_a), canonical_trace_hash(canonical_b))
        self.assertTrue(compare_canonical(canonical_a, canonical_b)["equivalent"])

    def test_canonicalize_preserves_semantic_stat_differences_beyond_preview(self) -> None:
        raw_a = {
            "program_id": "p",
            "side": "reference",
            "run_id": "r0",
            "status": "ok",
            "process_exit": {"status": "ok", "exit_code": 0, "timed_out": False},
            "events": [
                {
                    "event_index": 0,
                    "side": "reference",
                    "syscall_name": "fstat",
                    "syscall_number": 5,
                    "args": [3, 0x200000000000, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "start_ns": 1,
                    "end_ns": 2,
                    "timed_out": False,
                    "outputs": [
                        {
                            "label": "stat",
                            "arg_index": 1,
                            "length": 144,
                            "preview_hex": "0000000000000000000000000000000001000000000000008001000000000000",
                            "sha256": "stable-stat-a",
                        }
                    ],
                }
            ],
        }
        raw_b = {
            **raw_a,
            "run_id": "r1",
            "events": [
                {
                    **raw_a["events"][0],
                    "start_ns": 4,
                    "end_ns": 9,
                    "outputs": [
                        {
                            "label": "stat",
                            "arg_index": 1,
                            "length": 144,
                            "preview_hex": "0000000000000000000000000000000001000000000000008001000000000000",
                            "sha256": "stable-stat-b",
                        }
                    ],
                }
            ],
        }
        external_state = {"files": []}
        canonical_a = canonicalize(raw_a, external_state)
        canonical_b = canonicalize(raw_b, external_state)
        comparison = compare_canonical(canonical_a, canonical_b)
        self.assertFalse(comparison["equivalent"])
        self.assertFalse(comparison["noise_only"])
        self.assertEqual(comparison["first_divergence_index"], 0)

    def test_compare_treats_process_exit_mismatch_as_semantic(self) -> None:
        reference = {
            "program_id": "p",
            "side": "reference",
            "event_count": 1,
            "events": [
                {
                    "index": 0,
                    "syscall_name": "exit_group",
                    "syscall_number": 231,
                    "args": [0, 0, 0, 0, 0, 0],
                    "return_value": 0,
                    "errno": 0,
                    "duration_ns": 100,
                    "outputs": [],
                }
            ],
            "final_state": {"files": []},
            "process_exit": {"status": "ok", "exit_code": 1, "timed_out": False},
        }
        candidate = {
            **reference,
            "side": "candidate",
            "process_exit": {"status": "ok", "exit_code": 2, "timed_out": False},
        }
        comparison = compare_canonical(reference, candidate)
        self.assertFalse(comparison["equivalent"])
        self.assertFalse(comparison["noise_only"])
        self.assertEqual(comparison["first_divergence_index"], 1)

    def test_classify_process_returncode_allows_nonzero_exit(self) -> None:
        self.assertEqual(classify_process_returncode(7), "ok")
        self.assertEqual(classify_process_returncode(-9), "crash")

    def test_classify_candidate_execution_failures(self) -> None:
        classes = config()["classification"]
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="crash",
                comparison=None,
            ),
            classes["bug_likely"],
        )
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="timeout",
                comparison=None,
            ),
            classes["bug_likely"],
        )
        self.assertEqual(
            classify_result(
                reference_stable=True,
                reference_status="ok",
                candidate_status="infra_error",
                comparison=None,
            ),
            classes["weak_spec_or_env_noise"],
        )

    def test_reduce_case_maps_event_index_back_to_program_call_index(self) -> None:
        canonical = {
            "events": [
                {"index": 0, "syscall_name": "mmap"},
                {"index": 1, "syscall_name": "mmap"},
                {"index": 2, "syscall_name": "openat"},
                {"index": 3, "syscall_name": "close"},
            ]
        }
        self.assertEqual(map_event_index_to_program_call(canonical, 2), 0)
        self.assertEqual(map_event_index_to_program_call(canonical, 3), 1)
        self.assertIsNone(map_event_index_to_program_call(canonical, 1))


if __name__ == "__main__":
    unittest.main()
