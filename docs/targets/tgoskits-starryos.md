# TGOSKits StarryOS Bring-up

This guide documents the exact host prerequisites and commands needed to reproduce the real external-workspace StarryOS smoke path used by SysABI.

## Required Host Tools

You need all of the following available on `PATH`:

- `cargo`
- Rust nightly matching the TGOSKits checkout (`nightly-2026-04-01` for the pinned revision used here)
- `qemu-system-riscv64`
- `debugfs`
- `riscv64-linux-musl-gcc`

You also need:

- a TGOSKits checkout at the pinned revision from `configs/targets/tgoskits_starryos/target.json`
- `SYZABI_ENABLE_TGOSKITS=1`
- `SYZABI_TGOSKITS_DIR=/path/to/tgoskits`

## Recommended Toolchain Setup

### 1. Clone TGOSKits at the pinned revision

```bash
git clone https://github.com/rcore-os/tgoskits.git ~/tgoskits
git -C ~/tgoskits checkout eab9e0a80ffc2ff7e3b5e3207a8659965a27c36a
```

### 2. Install or expose the RISC-V musl cross compiler

One user-local option is:

```bash
mkdir -p ~/toolchains
cd ~/toolchains
curl -L --fail -o riscv64-linux-musl-cross.tgz https://musl.cc/riscv64-linux-musl-cross.tgz
tar -xzf riscv64-linux-musl-cross.tgz
export PATH="$HOME/toolchains/riscv64-linux-musl-cross/bin:$PATH"
```

You can verify it with:

```bash
command -v riscv64-linux-musl-gcc
```

### 3. Ensure the TGOSKits Rust nightly is available

```bash
rustup toolchain install nightly-2026-04-01 --profile minimal
cargo +nightly-2026-04-01 --version
```

### 4. Ensure runtime tools exist

```bash
command -v qemu-system-riscv64
command -v debugfs
```

## Environment Variables

Before running SysABI against StarryOS:

```bash
export SYZABI_ENABLE_TGOSKITS=1
export SYZABI_TGOSKITS_DIR="$HOME/tgoskits"
export PATH="$HOME/toolchains/riscv64-linux-musl-cross/bin:$PATH"
```

## TGOSKits Root Workflow Sanity Checks

These commands validate the same integrated path SysABI now targets:

```bash
cd "$SYZABI_TGOSKITS_DIR"
cargo xtask starry rootfs --arch riscv64
cargo xtask starry qemu --arch riscv64
```

Expected success signal:

- QEMU boots to the BusyBox shell prompt: `starry:~#`

## SysABI StarryOS Healthcheck

From the SysABI repo root:

```bash
python3 tools/tgoskits_launch.py --workflow tgoskits_starryos preflight
python3 tools/tgoskits_launch.py --workflow tgoskits_starryos healthcheck
python3 targets/entrypoint.py --workflow tgoskits_starryos --healthcheck
```

Useful optional output capture:

```bash
export SYZABI_CONSOLE_LOG_PATH="$PWD/.humanize/real-smoke/starry-healthcheck.console.log"
export SYZABI_RUNNER_RESULT_PATH="$PWD/.humanize/real-smoke/starry-healthcheck.runner.json"
python3 targets/entrypoint.py --workflow tgoskits_starryos --healthcheck
```

Expected success signal:

- `runner-result.json` contains `"status": "ok"`
- console log reaches `starry:~#`

## SysABI StarryOS Smoke Replay

### Build candidate/reference testcases

If you already have a prepared eligible file:

```bash
python3 tools/prog2c_wrap.py --workflow tgoskits_starryos --eligible-file <eligible.jsonl> --limit 1 --jobs 1
```

### Run the smoke workflow

The repo-owned launch command performs:

1. TGOSKits preflight
2. StarryOS healthcheck
3. testcase build unless `--skip-build` is set
4. bounded scheduler execution

```bash
python3 tools/tgoskits_launch.py \
  --workflow tgoskits_starryos \
  campaign \
  --campaign smoke \
  --eligible-file <eligible.jsonl> \
  --limit 1 \
  --jobs 1
```

If the healthcheck fails, the command stops before testcase build or scheduler execution.

Equivalent direct scheduler path:

```bash
python3 orchestrator/scheduler.py \
  --workflow tgoskits_starryos \
  --campaign smoke \
  --eligible-file <eligible.jsonl> \
  --limit 1 \
  --jobs 1
```

For a small batch smoke:

```bash
python3 orchestrator/scheduler.py \
  --workflow tgoskits_starryos \
  --campaign smoke \
  --eligible-file <eligible.jsonl> \
  --limit 2 \
  --jobs 1
```

That direct scheduler path does not include the repo-owned preflight and healthcheck steps.

Expected output locations:

- `artifacts/runs/targets/tgoskits_starryos/tgoskits_starryos/...`
- `reports/targets/tgoskits_starryos/tgoskits_starryos/summary.json`
- `reports/targets/tgoskits_starryos/tgoskits_starryos/campaign-results.jsonl`

## SysABI StarryOS Scale Replay

StarryOS supports high-concurrency execution through the shared-runtime batch execution mode. The scale workflow uses larger batch sizes and higher job counts than the smoke workflow.

### Build candidate/reference testcases for scale

```bash
python3 tools/prog2c_wrap.py --workflow tgoskits_starryos_scale --eligible-file <eligible.jsonl> --limit 200 --jobs 8
```

### Run the scale workflow

```bash
make run-tgoskits-starryos-scale ELIGIBLE_FILE=<eligible.jsonl>
```

Or directly:

```bash
python3 tools/tgoskits_launch.py \
  --workflow tgoskits_starryos_scale \
  campaign \
  --campaign full \
  --eligible-file <eligible.jsonl> \
  --limit 200 \
  --jobs 8
```

The scale workflow defaults to `limit=200` and `jobs=8`, with a `candidate_batch_size` of 16.

Expected output locations:

- `artifacts/runs/targets/tgoskits_starryos/tgoskits_starryos_scale/...`
- `reports/targets/tgoskits_starryos/tgoskits_starryos_scale/summary.json`
- `reports/targets/tgoskits_starryos/tgoskits_starryos_scale/campaign-results.jsonl`

## Troubleshooting

### `missing required StarryOS tools: riscv64-linux-musl-gcc`

The RISC-V musl cross compiler is not on `PATH`.

Fix:

```bash
export PATH="$HOME/toolchains/riscv64-linux-musl-cross/bin:$PATH"
```

### `TGOSKits revision mismatch`

Your checkout is not at the pinned revision.

Fix:

```bash
git -C "$SYZABI_TGOSKITS_DIR" checkout eab9e0a80ffc2ff7e3b5e3207a8659965a27c36a
```

### Healthcheck boots but never reaches `starry:~#`

Validate the integrated TGOSKits path first:

```bash
cd "$SYZABI_TGOSKITS_DIR"
cargo xtask starry rootfs --arch riscv64
cargo xtask starry qemu --arch riscv64
```

If that fails, the issue is below SysABI in the StarryOS/TGOSKits environment.
