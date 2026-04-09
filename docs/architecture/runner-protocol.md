# Runner Protocol（当前 orchestrator ↔ runner 契约）

本文档描述当前 `orchestrator/vm_runner.py` 与具体 runner（本地二进制、命令型 runner、Asterinas runner）之间已经形成的协议。

目标不是定义理想接口，而是冻结 **当前命令上下文、环境变量、回写文件与兜底行为**，为后续抽象层拆分提供基线。

## 1. 协议边界

当前 orchestrator 负责：

1. 解析 workflow config 与 runner profiles；
2. 为每次 side run 创建：
   - sandbox 目录；
   - artifact 目录；
   - testcase 二进制拷贝；
   - 输出文件路径；
3. 通过环境变量把上下文传递给 runner；
4. 执行 runner；
5. 读取 `runner-result.json`；
6. 若 runner 未写完整 trace/state，则做有限兜底；
7. 统一写出 `run-result.json`。

当前 runner 负责：

1. 在给定 `work_dir` / `binary_path` 下执行 testcase；
2. 写出 `runner-result.json`；
3. 尽量写出 `stdout.txt` / `stderr.txt` / `console.log`；
4. 写出完整 `raw-trace.json` + `external-state.json`，或至少提供 `raw-trace.events.jsonl` 供 orchestrator 合成。

## 2. 命令模板上下文（`command` profile 占位符）

当前 `execution_context()` 会向 command runner 模板注入这些字段：

| 占位符 | 含义 |
| --- | --- |
| `{program_id}` | testcase 的 `program_id` |
| `{side}` | `reference` / `candidate` |
| `{run_id}` | 本次运行唯一标识 |
| `{repo_root}` | 仓库根目录 |
| `{timeout_sec}` | side 的有效超时 |
| `{sandbox_root}` | 本次运行 sandbox 目录 |
| `{artifact_root}` | 本次运行 artifact 目录 |
| `{binary_path}` | 应执行的 testcase 二进制 |
| `{stdout_path}` | stdout 文件路径 |
| `{stderr_path}` | stderr 文件路径 |
| `{console_path}` | console log 路径 |
| `{events_path}` | `raw-trace.events.jsonl` 路径 |
| `{raw_trace_path}` | `raw-trace.json` 路径 |
| `{external_state_path}` | `external-state.json` 路径 |
| `{runner_result_path}` | `runner-result.json` 路径 |

当前基线 contract 中：

- `command` 模板实际被使用；
- `batch_command` 字段虽然存在于 Asterinas profile 中，但当前批量执行路径并不通过 `resolve_batch_command()` 调度 batch manifest，而是由 orchestrator 内部包装 package + per-case 执行。

## 3. orchestrator 下发给 runner 的环境变量

以下变量当前由 `execute_side()` 和 `execute_prepared_candidate_case()` 明确设置。

### 3.1 通用变量

| 变量 | 是否总是存在 | 含义 |
| --- | --- | --- |
| `SYZABI_SIDE` | 是 | 当前 side，`reference` 或 `candidate` |
| `SYZABI_PROGRAM_ID` | 是 | 当前 testcase 标识 |
| `SYZABI_RUN_ID` | 是 | 当前运行标识 |
| `SYZABI_TRACE_EVENTS_PATH` | 是 | 行式 events 输出路径 |
| `SYZABI_TRACE_PREVIEW_BYTES` | 是 | trace preview 截断字节数 |
| `SYZABI_RUNNER_RESULT_PATH` | 是 | runner-result 写回路径 |
| `SYZABI_WORK_DIR` | 是 | sandbox 工作目录 |
| `SYZABI_BINARY_PATH` | 是 | testcase 可执行文件路径 |
| `SYZABI_STDOUT_PATH` | 是 | stdout 文件路径 |
| `SYZABI_STDERR_PATH` | 是 | stderr 文件路径 |
| `SYZABI_CONSOLE_LOG_PATH` | 是 | console log 文件路径 |
| `SYZABI_RAW_TRACE_PATH` | 是 | raw trace JSON 路径 |
| `SYZABI_EXTERNAL_STATE_PATH` | 是 | external state JSON 路径 |

### 3.2 受控 divergence 注入变量（可选）

仅当 orchestrator 启用 `inject_trace` 时下发：

| 变量 | 含义 |
| --- | --- |
| `SYZABI_INJECT_TRACE_ENABLED` | 是否启用注入 |
| `SYZABI_INJECT_TRACE_CALL_INDEX` | 指定 syscall 索引 |
| `SYZABI_INJECT_TRACE_SYSCALL` | 目标 syscall 名 |
| `SYZABI_INJECT_TRACE_FIELD` | 目标字段，如 `return` |
| `SYZABI_INJECT_TRACE_VALUE` | 注入值 |

### 3.3 Asterinas package 变量（仅 packaged candidate path）

| 变量 | 含义 |
| --- | --- |
| `SYZABI_ASTERINAS_PACKAGE_DIR` | packaged initramfs 目录 |
| `SYZABI_ASTERINAS_PACKAGE_SLOT` | 当前 testcase 在 package 中的 slot |

这两个变量当前是 orchestrator 对 Asterinas runner 的显式 target-specific 耦合。

### 3.4 从父进程透传的环境

当前 subprocess 环境基于 `env_with_temp()`/`env_with_temp(cfg=...)` 构造，因此 runner 还会继承：

- `TMPDIR`
- `SYZABI_WORKFLOW`
- `SYZABI_CONFIG_PATH`（若由调用方设置）
- 宿主机已有的其他环境变量

其中 `tools/run_asterinas.py` 当前确实依赖 `SYZABI_WORKFLOW` / `SYZABI_CONFIG_PATH` 去读取 workflow config。

## 4. runner 必须/可以写出的文件

### 4.1 必须保证可恢复出结果的文件

| 文件 | 当前要求 |
| --- | --- |
| `runner-result.json` | **必须**可被写出，或至少在 runner 成功路径下存在 |
| `raw-trace.json` 或 `raw-trace.events.jsonl` | 至少提供其中一种 |
| `external-state.json` | 若 runner 不写，orchestrator 会兜底写空结构或本地采样结构 |

### 4.2 可选但强烈建议写出的文件

| 文件 | orchestrator 的兜底行为 |
| --- | --- |
| `stdout.txt` | 若不存在，使用 subprocess 捕获 stdout 回填 |
| `stderr.txt` | 若不存在，使用 subprocess 捕获 stderr 回填 |
| `console.log` | 若不存在，写入一个包含命令/cwd/status/elapsed_ms 的 JSON 片段 |

## 5. orchestrator 的兜底语义

当前 `vm_runner.py` 的兜底行为如下：

### 5.1 `runner-result.json` 缺失

- 普通命令执行路径：仍可基于 subprocess return code 推导一个默认状态；
- candidate batch finalize 路径：若缺失，则直接记为 `infra_error`，并写入 `missing candidate batch runner result`。

### 5.2 `raw-trace.json` 缺失

orchestrator 会从 `raw-trace.events.jsonl` 合成：

```json
{
  "program_id": "...",
  "side": "...",
  "run_id": "...",
  "status": "...",
  "events": [...],
  "process_exit": {
    "status": "...",
    "exit_code": ...,
    "timed_out": ...
  }
}
```

之后会立即执行 `validate_raw_trace()`。

### 5.3 `external-state.json` 缺失

- 本地/普通执行路径：使用 `sample_external_state(sandbox_root)` 采样；
- packaged candidate 路径：当前兜底写 `{"files": []}`。

## 6. `runner-result.json` 当前消费约束

`finalize_process_result()` 当前只读取这些键：

- `status`
- `exit_code`
- `status_detail`
- `detail`
- `kernel_build`

因此：

- runner 可以追加额外键；
- 但不能删除或改变上述键的语义；
- `status_detail` 与 `detail` 当前都被视为兼容输入。

## 7. `raw-trace.events.jsonl` 的当前要求

如果 runner 不直接写 `raw-trace.json`，而是只写 `raw-trace.events.jsonl`，则每一行都必须是一个 JSON event，最终应能被合成为满足 `analyzer/normalize.py` 输入要求的 `events[]`：

- `event_index`
- `side`
- `syscall_name`
- `syscall_number`
- `args`
- `return_value`
- `errno`
- `start_ns`
- `end_ns`
- `outputs`

`event_index` 必须严格递增，否则 `validate_raw_trace()` 会拒绝。

## 8. 当前状态映射

### 8.1 默认映射

- `command` runner：
  - subprocess return code `0` → 默认 `ok`
  - 非 `0` → 默认 `infra_error`
- `local` runner：
  - return code `< 0` → `crash`
  - 其他 → `ok`

### 8.2 runner-result 覆盖

若 `runner-result.json` 存在，则其 `status` / `exit_code` / `kernel_build` / `status_detail` 会覆盖默认推导结果。

### 8.3 timeout / OSError

orchestrator 本身捕获：

- `subprocess.TimeoutExpired` → `timeout`
- `OSError` → `infra_error`

## 9. 当前 Asterinas 特有扩展

当前 `tools/run_asterinas.py` 在上述通用 contract 之外，还存在一些 target-specific 约定：

- 通过 console markers 解析：
  - `PROCESS_EXIT`
  - `STDOUT`
  - `STDERR`
  - `EVENTS`
  - `EXTERNAL_STATE`
- 读取 `SYZABI_ASTERINAS_MODE`
- 读取 `SYZABI_GUEST_KCMD_ARGS`
- 读取 `SYZABI_ASTERINAS_ENABLE_KVM` / `SYZABI_ASTERINAS_MEM` / `SYZABI_ASTERINAS_SMP` / `SYZABI_ASTERINAS_NETDEV`

这些变量目前尚未被隔离到 target adapter 内，因此属于后续重构必须抽离、但当前必须先兼容的现实协议。

## 10. 对 Phase 1+ 的约束

后续抽象化时，允许：

- 把当前环境变量协议包装到 adapter 接口后面；
- 把 Asterinas 特有变量收敛到 target scope；
- 改善 batch runner 真正的 batch contract。

但在兼容期内，不应：

1. 删除本文列出的通用环境变量；
2. 改变 `runner-result.json` 的最低字段集合；
3. 让 `raw-trace.events.jsonl` 无法被合成现有 `raw-trace.json`；
4. 让 `tools/run_asterinas.py` 失去对现有环境变量的兼容读取能力。
