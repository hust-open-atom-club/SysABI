# SyzABI

> Offline differential replay for `syzkaller` programs.
>
> Compatibility alias: `FuzzAsterinas` remains in historical paths and scripts.

SyzABI does one thing well: it takes a corpus of `*.syz` programs, turns them into reproducible testcase binaries, runs them on both sides, normalizes the traces, compares the results, and writes reports you can actually inspect.

Today the main target is:

- Linux `reference`
- Asterinas `candidate`

The repository keeps the shortest operational guide in this `README`, with deeper target/release notes under `docs/`.

## One Command

If `corpus/meta/*.json` and `corpus/normalized/*.syz` already exist, the shortest path is:

```bash
make run
```

For a larger Asterinas run:

```bash
ASTERINAS_JOBS=80 RUN_LIMIT=100 make run
```

`make run` now performs the full Asterinas smoke pipeline:

1. initialize baseline and Asterinas layout
2. rebuild baseline eligible corpus
3. derive Asterinas eligible corpus
4. prepare the Asterinas Docker runner and shared kernel bundle
5. build testcase binaries for the selected limit
6. run the Asterinas workflow

Default values:

- workflow: `asterinas`
- campaign: `smoke`
- limit: `100`
- jobs: `4`

## Quick Start

### 1. Bootstrap once

```bash
make bootstrap
make init-layout
```

### 2. Prepare Asterinas source tree

```bash
git clone https://github.com/asterinas/asterinas.git third_party/asterinas
git -C third_party/asterinas checkout main
```

### 3. If you do not have a corpus yet

```bash
make generate-corpus
make import-corpus
```

### 4. Run Asterinas

```bash
ASTERINAS_JOBS=80 RUN_LIMIT=100 make run
```

## Core Workflows

| Workflow | Purpose | Typical comparison |
| --- | --- | --- |
| `baseline` | validate the replay framework on Linux vs Linux | Linux `reference` vs Linux `candidate` |
| `asterinas` | run Linux vs Asterinas differential replay | Linux `reference` vs Asterinas `candidate` |
| `asterinas_scml` | add SCML-aware filtering and preflight | Linux `reference` vs Asterinas `candidate` |
| `tgoskits_starryos` | external-workspace StarryOS integration path | Linux `reference` vs TGOSKits StarryOS `candidate` |
| `tgoskits_arceos_smoke` | external-workspace ArceOS smoke / PoC path | Linux `reference` vs TGOSKits ArceOS `candidate` |

## Most Useful Commands

### Asterinas

One-shot run:

```bash
make run
ASTERINAS_JOBS=80 RUN_LIMIT=100 make run
```

Step by step:

```bash
make filter-corpus
make derive-asterinas
make prepare-asterinas-candidate
make build-asterinas
ASTERINAS_JOBS=80 make run-asterinas-smoke
make analyze-asterinas
make report-asterinas
```

### Baseline

```bash
make filter-corpus
make build-eligible
make run-smoke
make run-full
make analyze
make report
```

### SCML

```bash
make build-asterinas-scml-manifest
make derive-asterinas-scml
make preflight-asterinas-scml
python3 orchestrator/scheduler.py --workflow asterinas_scml --campaign smoke --limit 100 --jobs 8
python3 tools/render_summary.py --workflow asterinas_scml
```

## Requirements

Host tools:

- `bash`
- `python3`
- `make`
- `git`
- `curl`
- `tar`
- `gcc`
- `strace`

For Asterinas:

- `docker`
- `qemu-system-x86_64`
- preferably `/dev/kvm`

Notes:

- `make bootstrap` installs the pinned Go toolchain into `artifacts/toolchains/go/current/go`
- Asterinas runs use the Docker path by default
- the shared Asterinas kernel bundle is prepared once and then reused across testcase runs

## What The Runner Actually Does

For the Asterinas workflow, the runner is intentionally simple:

- testcase binaries are built from `syz-prog2c`
- the Asterinas kernel image and packaged bundle are prepared in Docker
- each testcase runs in its own sandbox directory
- traces and external state are written to disk
- results are compared and classified into:
  - `NO_DIFF`
  - `BASELINE_INVALID`
  - `WEAK_SPEC_OR_ENV_NOISE`
  - `UNSUPPORTED_FEATURE`
  - `BUG_LIKELY`

## Important Outputs

Asterinas run outputs:

- `eligible_programs/targets/asterinas/asterinas/default.jsonl`
- `build/targets/asterinas/asterinas/testcases/`
- `artifacts/runs/targets/asterinas/asterinas/`
- `artifacts/targets/asterinas/build-info.json`
- `reports/targets/asterinas/asterinas/campaign-results.jsonl`
- `reports/targets/asterinas/asterinas/summary.json`
- `reports/targets/asterinas/asterinas/summary.md`
- `reports/targets/asterinas/asterinas/failure-report.json`

Baseline outputs:

- `eligible_programs/targets/linux/baseline/default.jsonl`
- `build/targets/linux/baseline/testcases/`
- `artifacts/runs/targets/linux/baseline/`
- `reports/targets/linux/baseline/summary.json`

## Compatibility Contract

The following surfaces are intentionally preserved while the platform layer evolves:

- workflow names and generic entrypoints such as `make run`, `make build-workflow`, `make run-workflow`
- canonical artifact roots under `build/targets/<target>/<workflow>/`, `artifacts/runs/targets/<target>/<workflow>/`, and `reports/targets/<target>/<workflow>/`
- legacy `_rules.json` compatibility for older configs
- per-run materialization of `stdout.txt`, `stderr.txt`, `console.log`, `raw-trace.json`, `external-state.json`, and `run-result.json`

For the TGOSKits StarryOS workflow:

- the repo does not vendor TGOSKits; point the workflow at an external checkout with `SYZABI_TGOSKITS_DIR`
- external TGOSKits targets are explicitly gated by `SYZABI_ENABLE_TGOSKITS=1`
- `trace.events_transport=stdout` is used so guest-side trace events can be recovered from framed stdout lines when a writable guest file path is not available
- `tgoskits_arceos_smoke` is intentionally smoke-only and does not claim syscall differential replay support
- see `docs/targets/tgoskits-starryos.md` for exact host prerequisites, `PATH` setup, and real StarryOS healthcheck/smoke commands

## Repository Layout

```text
.
├── agent/                 # guest-side trace helpers
├── analyzer/              # normalize / compare / classify
├── cmd/                   # Go helper tools
├── compat_specs/          # SCML and generation metadata
├── configs/               # workflow and target configuration
├── orchestrator/          # scheduler and VM runner
├── targets/               # target-owned runtime/build logic
├── tools/                 # corpus / build / report scripts
├── tests/                 # regression and unit tests
├── corpus/                # raw / normalized / meta
├── eligible_programs/     # executable JSONL lists
├── build/                 # testcase build roots
├── artifacts/             # runtime state, sandboxes, caches
└── reports/               # summaries and failure reports
```

## Version Pins

Current pinned components in this repository:

- `syzkaller`: `5b92003d577daa0766edda7ed533d75e1ac545ff`
- Asterinas Docker image: `asterinas/asterinas:0.17.1-20260317`

The exact Asterinas revision is checked from the configured workflow/runtime path during preparation.

## Current Expectations

`make run` is meant to make the infrastructure path predictable:

- it should not require remembering six separate commands
- it should be safe to use with higher `ASTERINAS_JOBS`
- it should reuse prepared kernel assets instead of rebuilding per testcase

If a run still fails, the first files to inspect are usually:

- `reports/targets/asterinas/asterinas/summary.json`
- `reports/targets/asterinas/asterinas/campaign-results.jsonl`
- the corresponding `candidate/console.log` under `artifacts/runs/targets/asterinas/asterinas/`
