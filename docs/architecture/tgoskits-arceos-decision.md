# TGOSKits ArceOS Decision

## Decision

ArceOS now has an experimental launch path in SysABI, but it still does not claim StarryOS-level Linux syscall compatibility.

The implemented decision is:

- keep ArceOS non-default and explicitly gated behind `SYZABI_ENABLE_TGOSKITS=1`
- support a real external-workspace smoke healthcheck through TGOSKits
- allow a repo-owned experimental candidate path that materializes a generated ArceOS C app, runs it under QEMU, and recovers framed trace output from the console
- keep batch execution unsupported
- continue to reject any claim that ArceOS already matches StarryOS as a Linux-compatible syscall target

## Rationale

- TGOSKits documents ArceOS primarily as a modular OS with examples and test packages, not as a Linux-compatible syscall target in the same sense as StarryOS.
- SysABI currently expects to run syzkaller-derived user binaries and collect syscall-oriented traces.
- StarryOS is still the natural first-class integration target because its Linux-compatible shell/rootfs path matches SysABI's execution model more directly.
- A repo-owned experimental ArceOS launch path is still useful because it removes the previous "healthcheck only" dead end and gives SysABI an executable way to start bounded candidate runs.

## Current Implementation

- `tgoskits_arceos_smoke` remains non-default and explicitly experimental.
- `targets/tgoskits_arceos/api.py` now supports `healthcheck` plus single-case experimental replay through a generated ArceOS C app.
- the experimental path uses a temporary managed `os/arceos/.cargo/config.toml`, a generated `disk.img`, and `BLK=y` so the C-app build can reuse TGOSKits root patch overrides and boot with a block device.
- `tools/tgoskits_launch.py` and [targets/tgoskits-arceos.md](../targets/tgoskits-arceos.md) provide the repo-owned operator entrypoints and documentation.

## Reconsideration Trigger

Move ArceOS to syscall-level or API-level differential replay only after:

1. the experimental C-app path demonstrates stable enough candidate execution for more than ad hoc single-case evidence
2. the resulting artifact can be mapped cleanly into SysABI's comparison pipeline
3. smoke evidence shows the path is repeatable enough for CI/nightly use
4. batch execution and broader syscall coverage have a defensible implementation strategy
