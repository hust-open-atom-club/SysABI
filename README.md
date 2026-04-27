# SyzABI

> Offline differential replay for `syzkaller` programs across OS kernels.

SyzABI takes a corpus of `*.syz` programs, compiles them into reproducible testcase binaries, runs them simultaneously on a **reference** kernel (Linux) and a **candidate** kernel (Asterinas, StarryOS, ArceOS, etc.), normalizes the resulting syscall traces, compares the outputs, and produces actionable divergence reports.

---

## Core Concept

Every workflow follows the same pattern:

```
syz programs  →  testcase binaries  →  run on reference + candidate  →  compare traces  →  report
```

The interface is unified: every workflow is driven by `make <command> WORKFLOW=<name>`.

| Side | Typical kernel | Role |
|------|---------------|------|
| `reference` | Linux | Ground truth |
| `candidate` | Asterinas / StarryOS / ArceOS / Linux | System under test |

---

## Quick Start

### 1. Bootstrap once

```bash
make bootstrap        # Install pinned Go toolchain
make init-layout      # Create directory scaffolding
```

### 2. Prepare target source (example: Asterinas)

```bash
git clone https://github.com/asterinas/asterinas.git third_party/asterinas
git -C third_party/asterinas checkout main
```

### 3. Generate or import corpus

```bash
make generate-corpus
make import-corpus
```

### 4. Run

```bash
# Default: Asterinas smoke, 100 cases, 4 jobs
make run

# Or fully explicit
make run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=100 JOBS=4
```

---

## Supported Workflows

| Workflow | Purpose | Comparison |
|----------|---------|------------|
| `baseline` | Validate replay framework | Linux vs Linux |
| `asterinas` | Differential replay | Linux vs Asterinas |
| `asterinas_scml` | SCML-aware filtering + preflight | Linux vs Asterinas |
| `tgoskits_starryos` | TGOSKits StarryOS integration | Linux vs StarryOS |
| `tgoskits_arceos_smoke` | TGOSKits ArceOS smoke / PoC | Linux vs ArceOS |

---

## Workflow Guides

### Asterinas

One-shot:

```bash
make run
# or
ASTERINAS_JOBS=80 make run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=100
```

Step by step:

```bash
make filter-corpus
make derive WORKFLOW=asterinas
make prepare-target WORKFLOW=asterinas
make build WORKFLOW=asterinas
make run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=4
make analyze WORKFLOW=asterinas
make report WORKFLOW=asterinas
```

### Baseline

```bash
make filter-corpus
make build WORKFLOW=baseline
make run WORKFLOW=baseline CAMPAIGN=smoke LIMIT=100
make run WORKFLOW=baseline CAMPAIGN=full LIMIT=1000
make analyze WORKFLOW=baseline
make report WORKFLOW=baseline
```

### SCML

```bash
make build-asterinas-scml-manifest
make derive WORKFLOW=asterinas_scml
make preflight-workflow WORKFLOW=asterinas_scml
python3 orchestrator/scheduler.py --workflow asterinas_scml --campaign smoke --limit 100 --jobs 8
python3 tools/render_summary.py --workflow asterinas_scml
```

### TGOSKits — StarryOS

Prerequisites:

- `SYZABI_ENABLE_TGOSKITS=1`
- `SYZABI_TGOSKITS_DIR=/path/to/tgoskits`
- Rust toolchain (`rustc`, `cargo`)
- `riscv64-linux-musl-gcc`
- QEMU system-mode `qemu-system-riscv64`

Preflight and healthcheck:

```bash
export SYZABI_ENABLE_TGOSKITS=1
export SYZABI_TGOSKITS_DIR=/path/to/tgoskits

python3 tools/tgoskits_launch.py --workflow tgoskits_starryos preflight
python3 tools/tgoskits_launch.py --workflow tgoskits_starryos healthcheck
```

Smoke campaign:

```bash
make run WORKFLOW=tgoskits_starryos CAMPAIGN=smoke LIMIT=20 JOBS=4
```

Scale campaign:

```bash
make run WORKFLOW=tgoskits_starryos_scale CAMPAIGN=full LIMIT=200 JOBS=8
```

For detailed host prerequisites, PATH setup, and troubleshooting, see [`docs/targets/tgoskits-starryos.md`](docs/targets/tgoskits-starryos.md).

### TGOSKits — ArceOS

Prerequisites:

- `SYZABI_ENABLE_TGOSKITS=1`
- `SYZABI_TGOSKITS_DIR=/path/to/tgoskits`

Preflight and healthcheck:

```bash
export SYZABI_ENABLE_TGOSKITS=1
export SYZABI_TGOSKITS_DIR=/path/to/tgoskits

python3 tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke preflight
python3 tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke healthcheck
```

Experimental single-case campaign:

```bash
make run WORKFLOW=tgoskits_arceos_smoke CAMPAIGN=smoke LIMIT=1 JOBS=1
```

For details, see [`docs/targets/tgoskits-arceos.md`](docs/targets/tgoskits-arceos.md).

---

## System Requirements

### Host tools

- `bash`
- `python3`
- `make`
- `git`
- `curl`
- `tar`
- `gcc`
- `strace`

### For Asterinas workflows

- `docker`
- `qemu-system-x86_64`
- `/dev/kvm` (recommended for speed)

### For TGOSKits StarryOS

- `qemu-system-riscv64`
- `riscv64-linux-musl-gcc`
- Rust nightly toolchain

### Notes

- `make bootstrap` installs the pinned Go toolchain into `artifacts/toolchains/go/current/go`.
- Asterinas runs use Docker by default; the shared kernel bundle is prepared once and reused.
- TGOSKits targets are gated by `SYZABI_ENABLE_TGOSKITS=1` and require an external TGOSKits checkout pointed to by `SYZABI_TGOSKITS_DIR`.

---

## What the Runner Does

1. **Build** — `syz-prog2c` turns each `*.syz` program into a standalone C binary.
2. **Prepare** — Target-specific assets (kernel image, initramfs, Docker bundle) are built or reused.
3. **Run** — Each testcase executes in an isolated sandbox against both reference and candidate sides.
4. **Trace** — Syscall events, return values, errno, and memory outputs are captured.
5. **Compare** — Traces are normalized and compared. Results are classified:
   - `NO_DIFF` — behavior matches
   - `BASELINE_INVALID` — reference side failed (infrastructure issue)
   - `WEAK_SPEC_OR_ENV_NOISE` — expected environmental difference
   - `UNSUPPORTED_FEATURE` — candidate does not implement the syscall
   - `BUG_LIKELY` — semantic divergence detected

---

## Important Outputs

### Asterinas

- `eligible_programs/targets/asterinas/asterinas/default.jsonl`
- `build/targets/asterinas/asterinas/testcases/`
- `artifacts/runs/targets/asterinas/asterinas/`
- `artifacts/targets/asterinas/build-info.json`
- `reports/targets/asterinas/asterinas/campaign-results.jsonl`
- `reports/targets/asterinas/asterinas/summary.json`
- `reports/targets/asterinas/asterinas/summary.md`
- `reports/targets/asterinas/asterinas/failure-report.json`

### Baseline

- `eligible_programs/targets/linux/baseline/default.jsonl`
- `build/targets/linux/baseline/testcases/`
- `artifacts/runs/targets/linux/baseline/`
- `reports/targets/linux/baseline/summary.json`

### TGOSKits StarryOS

- `artifacts/runs/targets/tgoskits_starryos/tgoskits_starryos/`
- `reports/targets/tgoskits_starryos/tgoskits_starryos/summary.json`

When investigating failures, the first files to inspect are usually:

- `reports/targets/<target>/<workflow>/summary.json`
- `reports/targets/<target>/<workflow>/campaign-results.jsonl`
- The corresponding `candidate/console.log` under `artifacts/runs/targets/<target>/<workflow>/`

---

## Repository Layout

```text
.
├── agent/                 # Guest-side trace helpers
├── analyzer/              # Normalize / compare / classify
├── cmd/                   # Go helper tools
├── compat_specs/          # SCML and generation metadata
├── configs/               # Workflow and target configuration
├── orchestrator/          # Scheduler and VM runner
├── targets/               # Target-owned runtime/build logic
├── tools/                 # Corpus / build / report scripts
├── tests/                 # Regression and unit tests
├── corpus/                # Raw / normalized / meta
├── eligible_programs/     # Executable JSONL lists
├── build/                 # Testcase build roots
├── artifacts/             # Runtime state, sandboxes, caches
└── reports/               # Summaries and failure reports
```

---

## Version Pins

| Component | Version / Commit |
|-----------|-----------------|
| `syzkaller` | `5b92003d577daa0766edda7ed533d75e1ac545ff` |
| Asterinas Docker image | `asterinas/asterinas:0.17.1-20260317` |

The exact Asterinas revision is resolved from the configured workflow path during preparation.

---

## Design Goals

- **One command to run** — `make run WORKFLOW=...` should cover the full pipeline.
- **Safe parallelism** — Higher `JOBS` should scale without corruption.
- **Asset reuse** — Prepared kernel bundles are cached and reused across testcases.
- **Clean interface** — No hard-coded target names in generic tooling; everything is workflow-driven.
