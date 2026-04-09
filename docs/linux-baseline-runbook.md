# Linux baseline Runbook

## Bootstrap

```bash
make bootstrap
make init-layout
```

## End-to-End

```bash
make generate-corpus
make import-corpus
make filter-corpus
make build-eligible
make run-smoke
make run-full
make analyze
```

## Controlled Divergence

```bash
python3 tools/reduce_case.py --fixture controlled_divergence
```

## Outputs

- `corpus/`: imported corpus, normalized corpus, metadata and rejected inputs
- `eligible_programs/baseline.jsonl`: stable eligible list
- `build/testcases/`: generated C files and testcase binaries
- `artifacts/runs/`: stdout, stderr, raw trace, canonical trace, external state, run-result
- `reports/baseline/`: summaries, baseline-invalid list, divergence index and minimized reports
