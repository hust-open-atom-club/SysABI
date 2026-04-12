# TGOSKits ArceOS Experimental Launch

This guide documents the current experimental ArceOS path in SysABI.

The current scope is intentionally narrower than StarryOS:

- external TGOSKits checkout only
- explicit feature flag gating
- repo-owned preflight and launch commands
- single-case experimental replay path through a generated ArceOS C app
- no batch execution
- no claim of Linux-compatible syscall parity with StarryOS
- the repo-owned campaign command only supports `--limit 1 --jobs 1`

## Required Host Tools

You need all of the following on `PATH`:

- `cargo`
- `make`
- `mkfs.fat`
- `qemu-system-riscv64`
- `riscv64-linux-musl-gcc`
- `riscv64-linux-musl-ar`
- `riscv64-linux-musl-ranlib`

You also need:

- a TGOSKits checkout at the pinned revision from `configs/targets/tgoskits_arceos/target.json`
- `SYZABI_ENABLE_TGOSKITS=1`
- `SYZABI_TGOSKITS_DIR=/path/to/tgoskits`

## Environment Variables

Before running SysABI against ArceOS:

```bash
export SYZABI_ENABLE_TGOSKITS=1
export SYZABI_TGOSKITS_DIR="$HOME/tgoskits"
export PATH="$HOME/toolchains/riscv64-linux-musl-cross/bin:$PATH"
```

## Repo-Owned Preflight

From the SysABI repo root:

```bash
python3 tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke preflight
```

Equivalent `make` entrypoint:

```bash
make preflight-tgoskits-arceos
```

Expected success signal:

- printed JSON includes the pinned revision, workspace path, target triple, and `experimental-c-app` mode

## Healthcheck

```bash
python3 tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke healthcheck
```

Equivalent `make` entrypoint:

```bash
python3 targets/entrypoint.py --workflow tgoskits_arceos_smoke --healthcheck
```

Expected success signal:

- runner result contains `"status": "ok"`

## Experimental Campaign Start

This path uses the existing SysABI build and scheduler flow, but the candidate side is materialized as a generated ArceOS C app under a temporary work directory.

If you already have an eligible file:

```bash
python3 tools/tgoskits_launch.py \
  --workflow tgoskits_arceos_smoke \
  campaign \
  --campaign smoke \
  --eligible-file <eligible.jsonl> \
  --limit 1 \
  --jobs 1
```

The launch tool rejects any other `--limit` or `--jobs` combination with a fail-fast scope error.

Equivalent `make` entrypoint:

```bash
make run-tgoskits-arceos-smoke ELIGIBLE_FILE=<eligible.jsonl> LIMIT=1 JOBS=1
```

Expected output locations:

- `build/targets/tgoskits_arceos/tgoskits_arceos_smoke/testcases/...`
- `artifacts/runs/targets/tgoskits_arceos/tgoskits_arceos_smoke/...`
- `reports/targets/tgoskits_arceos/tgoskits_arceos_smoke/summary.json`

## Implementation Notes

The experimental ArceOS path currently does the following:

- validates TGOSKits revision and toolchain prerequisites
- auto-creates `os/arceos/disk.img` when missing
- writes a temporary managed `os/arceos/.cargo/config.toml` so ArceOS C-app builds can reuse the TGOSKits root `[patch.crates-io]` overrides
- builds a generated C app with `axlibc`
- runs QEMU with `BLK=y`
- extracts framed trace events from the console log
- removes the temporary managed cargo config after the run

## Limitations

- batch execution is not supported
- the repo-owned `campaign` command requires `--limit 1 --jobs 1`
- the path is still experimental and should be treated as launch-readiness evidence, not as StarryOS-level maturity
- current reporting still compares against Linux reference behavior, so ArceOS divergences should be interpreted in the context of this narrower execution model

## Troubleshooting

### `missing required ArceOS tools: ...`

One or more required host tools are missing from `PATH`.

### `missing syz-prog2c`

The syzkaller helper is missing from `third_party/syzkaller/bin/syz-prog2c`.

Fix:

```bash
make bootstrap
```

### `refusing to overwrite user-managed cargo config`

SysABI only manages `os/arceos/.cargo/config.toml` when the file contains the TGOSKits C-test marker.

Fix:

- remove the conflicting file if it is disposable, or
- move the user-managed config elsewhere before running the experimental path

### `missing trace markers in ArceOS command output`

The generated app ran but SysABI could not recover framed trace events from the console log.

Inspect:

- the candidate `console.log`
- the generated testcase under `build/targets/tgoskits_arceos/tgoskits_arceos_smoke/testcases/...`
- the corresponding ArceOS candidate run directory under `artifacts/runs/targets/tgoskits_arceos/tgoskits_arceos_smoke/...`
