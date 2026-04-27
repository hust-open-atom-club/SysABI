from __future__ import annotations

from enum import StrEnum, auto


class ExecutionStatus(StrEnum):
    """Canonical execution status values for a single run."""

    OK = "ok"
    TIMEOUT = "timeout"
    INFRA_ERROR = "infra_error"
    CRASH = "crash"
    CANDIDATE_BUG = "candidate_bug"
    UNSUPPORTED = "unsupported"


class Classification(StrEnum):
    """Canonical classification values for a differential-replay result."""

    NO_DIFF = "no_diff"
    BASELINE_INVALID = "baseline_invalid"
    BUG_LIKELY = "bug_likely"
    WEAK_SPEC_OR_ENV_NOISE = "weak_spec_or_env_noise"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    BUILD_FAILURE = "build_failure"
    INFRA_ERROR = "infra_error"


class ExecutionMode(StrEnum):
    """Supported candidate execution modes."""

    SINGLE_COMMAND = "single_command"
    PACKAGED_PER_CASE = "packaged_per_case"
    SHARED_RUNTIME_BATCH = "shared_runtime_batch"


class Side(StrEnum):
    """Differential replay sides."""

    REFERENCE = "reference"
    CANDIDATE = "candidate"


class TraceEventsTransport(StrEnum):
    """Trace event delivery mechanisms."""

    FILE = "file"
    STDOUT = "stdout"


class SCMLPreflightStatus(StrEnum):
    """SCML preflight gate statuses."""

    NOT_RUN = "not_run"
    PASSED = "passed"
    REJECTED = "rejected"


class SCMLResultBucket(StrEnum):
    """SCML-aware result buckets."""

    NOT_RUN = ""
    REJECTED_BY_SCML = "rejected_by_scml"
    PASSED_SCML_AND_NO_DIFF = "passed_scml_and_no_diff"
    PASSED_SCML_AND_DIVERGED = "passed_scml_and_diverged"
    PASSED_SCML_BUT_CANDIDATE_FAILED = "passed_scml_but_candidate_failed"
    PASSED_SCML_BUT_REFERENCE_FAILED = "passed_scml_but_reference_failed"
