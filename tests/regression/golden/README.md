# Golden Regression Fixtures

这些 fixture 用于冻结 **当前重构前** 的报告输出行为。

约定：

- `__ROOT__` 是测试运行时临时目录根路径的占位符；
- fixture 当前冻结的是 `scheduler.main()` 产出的最终：
  - `summary.json`
  - `failure-report.json`
  - `divergence-index.jsonl`
- 如果未来重构需要有意改变这些输出，必须同时：
  1. 解释变更原因；
  2. 更新本文档与对应 fixture；
  3. 在 PR/提交说明里明确这是“契约更新”，而不是无意漂移。
