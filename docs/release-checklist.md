# Release Checklist

## Scope

Use this checklist before signoff or release for any workflow that produces a `summary.json`.

## Required Steps

1. Run the relevant workflow and generate `summary.json`.

   Unified command interface:
   ```bash
   make run WORKFLOW=<workflow> CAMPAIGN=<campaign> LIMIT=<n> JOBS=<n>
   ```

   Example for Asterinas smoke:
   ```bash
   make run WORKFLOW=asterinas CAMPAIGN=smoke LIMIT=50 JOBS=4
   ```

2. Run threshold validation:

```bash
python3 tools/check_workflow_thresholds.py --workflow <workflow> --campaign <campaign> --summary <summary.json>
```

`ci-fast` now exercises the threshold checker path with a synthetic workflow/summary pair so schema drift in threshold evaluation is caught automatically.

The authoritative blocking thresholds are the checked-in workflow thresholds. See [threshold-decision.md](threshold-decision.md).

3. Review:
   - `summary.json` (now includes `concurrency_breakdown` and `infra_error_breakdown`)
   - `summary.md`
   - `failure-report.json`
   - `report.md` (unified report with confirmed/unconfirmed divergences)
   - any uploaded smoke artifacts for external targets

4. Confirm migration / rollback notes:
   - external TGOSKits targets remain gated by `SYZABI_ENABLE_TGOSKITS=1`
   - baseline / asterinas workflows still run without TGOSKits
   - StarryOS operator bring-up steps match [targets/tgoskits-starryos.md](targets/tgoskits-starryos.md)
   - ArceOS experimental launch steps match [targets/tgoskits-arceos.md](targets/tgoskits-arceos.md)

## External Target Smoke

For external TGOSKits targets, run repo-owned preflight or campaign commands after setting:

- `SYZABI_ENABLE_TGOSKITS=1`
- `SYZABI_TGOSKITS_DIR=/path/to/tgoskits`

Example:

```bash
python3 tools/tgoskits_launch.py --workflow tgoskits_starryos preflight
python3 tools/tgoskits_launch.py --workflow tgoskits_arceos_smoke healthcheck
```

Workflow run artifacts are written under:

- `artifacts/runs/targets/<target>/<workflow>/...`
- `reports/targets/<target>/<workflow>/...`

For the real TGOSKits paths, use the documented host prerequisites and commands in [targets/tgoskits-starryos.md](targets/tgoskits-starryos.md) and [targets/tgoskits-arceos.md](targets/tgoskits-arceos.md).
