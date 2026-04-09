# Artifact Schema（当前稳定字段）

本文档记录当前仓库在 **重构前** 已经被 orchestrator、analyzer、render/report 链路实际消费的 artifact 字段。目标是冻结“最低稳定字段集合”，并明确哪些扩展是向后兼容的。

> 约定：
> - **稳定字段**：当前代码已经明确读取、校验或依赖的字段；
> - **允许扩展**：可新增字段，但不应删除、重命名或改变既有字段语义；
> - **未承诺顺序**：JSON 对象字段顺序不属于契约。
> - **快照语义**：本文档冻结的是 Phase 0 基线语义；后续重构可以新增兼容字段或额外抽象层，但不得悄悄改变这里列出的最低稳定字段集合。

## 1. `runner-result.json`（runner 写回）

### 生产者

- `tools/run_asterinas.py`
- `tools/run_asterinas.py --mode local-proxy`
- 任何遵循 `SYZABI_RUNNER_RESULT_PATH` 契约的 command runner

### 当前消费者

`orchestrator/vm_runner.py -> finalize_process_result()`

### 当前稳定字段

| 字段 | 类型 | 是否必需 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 是 | runner 认定的最终状态，如 `ok` / `timeout` / `infra_error` / `crash` |
| `exit_code` | int / null | 否 | runner 进程或 guest 进程退出码 |
| `kernel_build` | string | 否 | runner 提供的内核/镜像标识 |
| `status_detail` | string / null | 否 | 细节说明 |
| `detail` | string / null | 否 | 兼容旧命名；当前也会被读取 |

### 兼容规则

- `status` 是当前唯一真正必要的语义字段；
- `status_detail` 与 `detail` 当前为并行兼容键；
- 新增字段允许，但当前 orchestrator 会忽略。

## 2. `run-result.json`（orchestrator 写回）

### 生产者

`orchestrator/vm_runner.py`

### 当前稳定字段

`RunResult` dataclass 的序列化字段：

| 字段 | 类型 |
| --- | --- |
| `program_id` | string |
| `side` | string |
| `status` | string |
| `exit_code` | int / null |
| `stdout_path` | string |
| `stderr_path` | string |
| `console_log_path` | string |
| `trace_json_path` | string / null |
| `external_state_path` | string / null |
| `elapsed_ms` | int |
| `role` | string |
| `snapshot_id` | string |
| `kernel_build` | string |
| `run_id` | string |
| `status_detail` | string / null |
| `runner_kind` | string / null |

当前 `campaign-results.jsonl` 中的 `reference_runs[]` / `candidate_run` / `candidate_runs[]` 都与该结构兼容。

## 3. `raw-trace.json`

### 生产者

- command runner 直接写完整 trace；
- 或 runner 仅写 `raw-trace.events.jsonl`，由 `orchestrator/vm_runner.py` 合成完整 `raw-trace.json`

### 当前显式校验

`analyzer/schemas.py -> validate_raw_trace()`

### 顶层稳定字段

| 字段 | 类型 | 是否必需 |
| --- | --- | --- |
| `program_id` | string | 是 |
| `side` | string | 是 |
| `run_id` | string | 是 |
| `status` | string | 是 |
| `events` | list | 是 |
| `process_exit` | object | 是 |

### `events[]` 当前稳定字段

这些字段虽未在 `validate_raw_trace()` 中逐项校验，但会被 `analyzer/normalize.py` 实际读取，因此属于当前稳定字段：

| 字段 | 类型 |
| --- | --- |
| `event_index` | int |
| `side` | string |
| `syscall_name` | string |
| `syscall_number` | int |
| `args` | list[int] |
| `return_value` | int |
| `errno` | int |
| `start_ns` | int |
| `end_ns` | int |
| `outputs` | list[object] |

`validate_raw_trace()` 额外要求 `event_index` 严格递增。

### `events[].outputs[]` 当前稳定字段

`normalize_outputs()` 当前读取：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `label` | string | 如 `buf` / `stat` |
| `arg_index` | int | 对应 syscall 参数位置 |
| `length` | int | 输出长度 |
| `preview_hex` | string | 预览内容 |
| `sha256` | string | 输出内容摘要 |
| `resource_kind` | string | 可选；目前用于 `fd` |
| `resource_values` | list[int] | 可选；`fd` 输出时使用 |

### `process_exit` 当前稳定字段

| 字段 | 类型 |
| --- | --- |
| `status` | string |
| `exit_code` | int / null |
| `timed_out` | bool |

## 4. `external-state.json`

### 当前用途

`analyzer/normalize.py` 将其整体放入 canonical trace 的 `final_state`，不再做二次裁剪。

### 当前稳定字段

当前仓库默认格式来自 `sample_external_state()` 与 Asterinas runner 的 external-state 回写：

| 字段 | 类型 | 是否必需 |
| --- | --- | --- |
| `files` | list | 是 |

`files[]` 当前稳定字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `path` | string | 相对 `work_dir` 的路径 |
| `size` | int | 文件大小 |
| `sha256` | string / null | 读取得到的摘要 |
| `read_error` | string | 可选；当前常见为 `permission_denied` |

## 5. `canonical-trace.json`

虽然不在本轮计划要求的“必须文档化”清单里，但它是 compare/classify/report 当前直接依赖的中间产物，因此一并冻结。

### 当前显式校验

`analyzer/schemas.py -> validate_canonical_trace()`

### 顶层稳定字段

| 字段 | 类型 |
| --- | --- |
| `program_id` | string |
| `side` | string |
| `event_count` | int |
| `events` | list |
| `final_state` | object |
| `process_exit` | object |

### `events[]` 当前稳定字段

| 字段 | 类型 |
| --- | --- |
| `index` | int |
| `source_event_index` | int |
| `syscall_name` | string |
| `syscall_number` | int |
| `args` | list[int / string] |
| `return_value` | int / string |
| `errno` | int |
| `duration_ns` | int |
| `outputs` | list |

## 6. `campaign-results.jsonl`

### 生产者

`orchestrator/scheduler.py`

### 当前角色

这是调度阶段的“完整行级证据”，后续 `tools/render_summary.py`、`tools/reduce_case.py`、failure report 与 divergence index 都从这里派生。

### 当前稳定顶层字段（按常见结果行）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `program_id` | string | testcase 标识 |
| `classification` | string | 当前 taxonomy 输出 |
| `normalized_path` | string | 对应 `.syz` 路径 |
| `meta_path` | string | metadata JSON 路径，可能为空 |
| `reference_runs` | list[`RunResult`] | 至少一条 reference run，失败重试时可能多条 |
| `candidate_run` | object | 当前主 candidate run |
| `candidate_runs` | list[`RunResult`] | triage / batch / 兼容路径下可能存在 |
| `comparison` | object / null | compare 输出 |

### SCML / 预检相关可选字段

这些字段在 baseline 中通常为空，但当前已进入稳定兼容面：

- `scml_preflight_status`
- `scml_rejection_reasons`
- `scml_trace_log_path`
- `scml_sctrace_output_path`
- `scml_preflight_run_root`
- `scml_result_bucket`

### `comparison` 当前稳定字段

当前代码显式读取：

- `equivalent`
- `noise_only`
- `reason`
- `first_divergence_index`
- `final_state_equal`
- `process_exit_equal`

允许新增更细粒度 compare 字段，但不应删改上述键的语义。

## 7. `summary.json`

### 生产者

最终版本由 `tools/render_summary.py -> write_rendered_summary()` 覆盖写入；`orchestrator/scheduler.py -> write_summary()` 先写的简版 summary 是中间态，不应作为最终契约。

### 当前稳定字段

| 字段 | 类型 |
| --- | --- |
| `campaign` | string |
| `workflow` | string |
| `total` | int |
| `classification_counts` | object |
| `scml_result_counts` | object |
| `scml_rejected_count` | int |
| `eligible_program_count` | int |
| `build_success_rate` | float |
| `dual_execution_completion_rate` | float |
| `trace_generation_success_rate` | float |
| `canonicalization_success_rate` | float |
| `baseline_invalid_rate` | float |
| `scml_preflight_pass_rate` | float |
| `candidate_runner_kinds` | list[string] |
| `candidate_kernel_builds` | list[string] |
| `signoff_pass` | bool |

### 条件字段

下列字段按 workflow/输入存在时追加：

- `import_success_rate`
- `derivation_success_rate`
- `profile_enabled_total`
- `targets_with_candidates`
- `targets_without_candidates`
- `generation_candidate_count`

## 8. `failure-report.json`

### 生产者

`orchestrator/scheduler.py -> write_failure_reports()`

### 当前稳定字段

| 字段 | 类型 |
| --- | --- |
| `workflow` | string |
| `campaign` | string |
| `total_results` | int |
| `failed_results` | int |
| `classification_counts` | object |
| `failures_by_classification` | object |

`failures_by_classification.<CLASS>[]` 当前稳定字段：

| 字段 | 类型 |
| --- | --- |
| `program_id` | string |
| `classification` | string |
| `scml_preflight_status` | string |
| `normalized_path` | string |
| `meta_path` | string |
| `reference_status` | string / null |
| `candidate_status` | string / null |
| `first_divergence_index` | int / null |
| `first_divergence_syscall_name` | string / null |
| `reference_console_log_path` | string |
| `candidate_console_log_path` | string |
| `reference_trace_json_path` | string |
| `candidate_trace_json_path` | string |
| `reference_canonical_trace_path` | string |
| `candidate_canonical_trace_path` | string |
| `comparison` | object / null |

## 9. `divergence-index.jsonl`

### 生产者

`orchestrator/scheduler.py -> write_summary()`

### 当前选择规则

仅包含分类为：

- `BUG_LIKELY`
- `WEAK_SPEC_OR_ENV_NOISE`

的原始 `campaign-results.jsonl` 行。

### 当前稳定性约束

- 每行仍然沿用 `campaign-results.jsonl` 的原始结构；
- 过滤规则本身属于当前契约，应通过 golden regression 固定；
- 允许新增字段，但不应改变“按分类筛选并原样保留行内容”的语义。

## 10. 向后兼容原则

对本轮重构而言，以下行为视为破坏兼容：

1. 删除或重命名上文列出的稳定字段；
2. 改变 `summary.json` / `failure-report.json` / `divergence-index.jsonl` 的语义选择规则；
3. 把 runner-result / raw-trace / external-state 改到现有 analyzer / orchestrator 无法消费；
4. 将最终 `summary.json` 退回到 scheduler 的中间态格式。

允许的变更应限定为：

- 向现有 JSON 追加字段；
- 在不改变稳定字段语义的前提下增加额外报告；
- 新增目录层级时保留旧路径 shim。
