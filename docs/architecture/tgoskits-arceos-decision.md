# TGOSKits ArceOS Decision

## Decision

ArceOS stays on a smoke-only PoC path in SysABI for now.

The implemented decision is:

- keep ArceOS non-default and explicitly gated behind `SYZABI_ENABLE_TGOSKITS=1`
- support a real external-workspace smoke healthcheck through TGOSKits
- reject syscall-level differential replay for ArceOS candidate execution until there is evidence that the user-space / syscall model is compatible with SysABI's testcase format

## Rationale

- TGOSKits documents ArceOS primarily as a modular OS with examples and test packages, not as a Linux-compatible syscall target in the same sense as StarryOS.
- SysABI currently expects to run syzkaller-derived user binaries and collect syscall-oriented traces.
- StarryOS is the natural first integration target because its Linux-compatible shell/rootfs path matches SysABI's execution model more directly.

## Current Implementation

- `tgoskits_arceos_smoke` is a smoke-only workflow and target path.
- `targets/tgoskits_arceos/api.py` allows `healthcheck` and explicitly rejects candidate testcase replay.
- The rejection message points back to this decision so the limitation is deliberate rather than accidental.

## Reconsideration Trigger

Move ArceOS to syscall-level or API-level differential replay only after:

1. a TGOSKits-backed PoC demonstrates a stable way to execute instrumented user payloads or equivalent API probes
2. the resulting artifact can be mapped cleanly into SysABI's comparison pipeline
3. smoke evidence shows the path is repeatable enough for CI/nightly use
