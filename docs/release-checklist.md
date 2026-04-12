# Release Checklist

## Scope

Use this checklist before signoff or release for any workflow that produces a `summary.json`.

## Required Steps

1. Run the relevant workflow and generate `summary.json`.
2. Run threshold validation:

```bash
python3 tools/check_workflow_thresholds.py --workflow <workflow> --campaign <campaign> --summary <summary.json>
```

`ci-fast` now runs the threshold checker against the committed baseline smoke summary fixture so schema drift in threshold evaluation is caught automatically.

The authoritative blocking thresholds are the checked-in workflow thresholds. See [threshold-decision.md](threshold-decision.md).

3. Review:
   - `summary.json`
   - `summary.md`
   - `failure-report.json`
   - any uploaded smoke artifacts for external targets

4. Confirm migration / rollback notes:
   - external TGOSKits targets remain gated by `SYZABI_ENABLE_TGOSKITS=1`
   - baseline / asterinas workflows still run without TGOSKits
   - StarryOS operator bring-up steps match [targets/tgoskits-starryos.md](targets/tgoskits-starryos.md)

## External Target Smoke

For external TGOSKits targets, run the smoke helper after setting:

- `SYZABI_ENABLE_TGOSKITS=1`
- `SYZABI_TGOSKITS_DIR=/path/to/tgoskits`

Example:

```bash
python3 tools/run_external_target_smoke.py --workflow tgoskits_arceos_smoke
```

Artifacts are written under `artifacts/smoke/<workflow>/`.

For the real StarryOS path, use the documented host prerequisites and commands in [targets/tgoskits-starryos.md](targets/tgoskits-starryos.md).
