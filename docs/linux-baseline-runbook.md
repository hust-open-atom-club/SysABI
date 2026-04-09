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
```

`make run-smoke` 和 `make run-full` 结束后会自动产出整理后的报告；`make analyze` 只在你需要基于已有 `campaign-results.jsonl` 手动重渲染 summary/signoff 时再用。

## Controlled Divergence

```bash
python3 tools/reduce_case.py --fixture controlled_divergence
```

## Outputs

- `corpus/`: imported corpus, normalized corpus, metadata and rejected inputs
- `eligible_programs/baseline.jsonl`: stable eligible list
- `build/testcases/`: generated C files and testcase binaries
- `artifacts/runs/`: stdout, stderr, raw trace, canonical trace, external state, run-result
- `reports/baseline/`: summaries, signoff, failure-report, baseline-invalid list, divergence index and minimized reports
