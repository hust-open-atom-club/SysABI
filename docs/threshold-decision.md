# Threshold Decision

Date: 2026-04-12

## Decision

The current repo-default workflow thresholds are the final blocking CI / release thresholds unless they are changed by a later explicit repository update.

This applies to the thresholds checked from the workflow configs under:

- `configs/workflows/baseline.json`
- `configs/workflows/asterinas.json`
- `configs/workflows/asterinas_scml.json`
- `configs/workflows/tgoskits_starryos.json`
- `configs/workflows/tgoskits_arceos_smoke.json`

## Operational Meaning

- `tools/check_workflow_thresholds.py` should treat the checked-in threshold values as authoritative.
- CI and release processes should use the checked-in workflow thresholds directly.
- Future threshold changes must be made as repository changes, not as undocumented operator conventions.

## Notes

- `tgoskits_arceos_smoke` remains a smoke-only / PoC workflow; its thresholds reflect that workflow’s intentionally limited scope.
- StarryOS now has real external-workspace smoke evidence in `.humanize/real-smoke/tgoskits-starry/`, so the checked-in `tgoskits_starryos` thresholds now have concrete smoke evidence behind them.
