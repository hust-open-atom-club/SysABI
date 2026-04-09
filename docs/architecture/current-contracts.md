# 当前契约冻结（Phase 0 Baseline）

本文档记录 **重构开始前** 仓库已经对外暴露、并且当前实现真实依赖的契约。它的目的不是描述目标架构，而是给后续重构提供一个“不得无意改变”的对照面。

> 适用范围：当前 `baseline` / `asterinas` / `asterinas_scml` 三条 workflow  
> 非目标：本文档不为当前耦合背书；它只是把现状写清楚，便于后续按计划拆分。

## 1. 用户入口与命令面

仓库当前的用户入口主要来自 `Makefile`，底层统一落到 Python 脚本：

| 入口 | 当前行为 | 实际脚本 |
| --- | --- | --- |
| `make bootstrap` | 固定 syzkaller 与 Go 工具链 | `tools/bootstrap_syzkaller.sh` |
| `make init-layout` | 初始化目录骨架 | `tools/init_layout.py` |
| `make generate-corpus` | 生成输入 `*.syz` 程序 | `tools/generate_corpus.py` |
| `make import-corpus` | 导入并归一化 corpus | `tools/import_syz.py` |
| `make filter-corpus` | 依据 allowlist/stability 过滤 | `tools/filter_corpus.py` |
| `make build-eligible` | 将 `eligible_programs/*.jsonl` 构建为 testcase | `tools/prog2c_wrap.py` |
| `make run-smoke` / `make run-full` | baseline 调度执行与报告产出 | `orchestrator/scheduler.py` |
| `make analyze` | 基于已有 `campaign-results.jsonl` 重算报告 | `tools/render_summary.py` |
| `make report` | 基于 fixture/campaign 生成最小化报告 | `tools/reduce_case.py` |
| `make derive-asterinas` | 从 baseline 派生 Asterinas corpus | `tools/derive_asterinas_corpus.py` |
| `make prepare-asterinas-candidate` | 做 Asterinas candidate 健康检查 | `tools/run_asterinas.py --healthcheck` |
| `make build-asterinas` | 构建 Asterinas testcase | `tools/prog2c_wrap.py --workflow asterinas` |
| `make run-asterinas-smoke/full` | 运行 Asterinas workflow | `orchestrator/scheduler.py --workflow asterinas ...` |
| `make build-asterinas-scml-manifest` | 构建 SCML manifest | `tools/build_scml_manifest.py` |
| `make derive-asterinas-scml` | 执行 SCML 导出、生成、派生、预检链路 | 多个 `tools/*.py` 串联 |
| `make preflight-asterinas-scml` | 仅执行 SCML 预检链路 | `tools/prog2c_wrap.py` + `tools/preflight_scml_gate.py` |

### 当前 CLI 约定

- `orchestrator/scheduler.py` 当前支持：
  - `--workflow`
  - `--campaign`
  - `--eligible-file`
  - `--limit`
  - `--jobs`
  - `--candidate-batch-size`
  - `--program-id`
  - `--controlled-divergence`
- `tools/render_summary.py` 当前支持：
  - `--workflow`
  - `--config-path`
  - `--campaign`
- `tools/prog2c_wrap.py` 当前支持：
  - `--workflow`
  - `--eligible-file`
  - `--program-id`
  - `--limit`
  - `--jobs`

这些入口脚本目前既是“功能入口”，也是“契约入口”；后续重构可以重定向实现，但在兼容期内不应直接删除。

## 2. workflow / config 发现逻辑

当前配置发现逻辑集中在 `orchestrator/common.py`：

### 2.1 环境变量

| 变量 | 含义 | 当前用途 |
| --- | --- | --- |
| `SYZABI_WORKFLOW` | 当前 workflow 名称 | `runtime_workflow()` 默认读取 |
| `SYZABI_CONFIG_PATH` | 显式 config JSON 路径 | 优先级高于 workflow 推导 |
| `SYZABI_TMPDIR` | 临时目录覆盖 | `env_with_temp()` / `temp_dir()` |

### 2.2 配置解析优先级

当前 `resolved_config_path()` 的行为是：

1. 如果调用方传入 `config_path`，优先使用；
2. 否则读取环境变量 `SYZABI_CONFIG_PATH`；
3. 否则根据 workflow 解析 `configs/<workflow>_rules.json`；
4. 若 workflow 为默认值 `baseline` 且推导文件不存在，则兜底到 `configs/baseline_rules.json`。

### 2.3 当前 config 形状

三类 workflow 当前都以单个 JSON 文件承载配置：

- `configs/baseline_rules.json`
- `configs/asterinas_rules.json`
- `configs/asterinas_scml_rules.json`

共同结构大致包含：

- `workflow`
- `schema_version`
- `target_os`
- `arch`
- `runner_profiles_path`
- `paths`
- `normalization`
- `stability`
- `build`
- `classification`
- `thresholds`

而 Asterinas/SCML 额外携带：

- `asterinas` 目标专用块
- `derivation`
- `preflight`
- `compat_manifest_path`
- `generation_profile_path`
- `parallel`

这意味着当前 config 仍然是 **workflow 驱动且内嵌 target 细节**，尚未拆成 `workflow` / `target` / `target_config`。

## 3. 当前 runner profile 契约

`runner_profiles()` 读取 `cfg["runner_profiles_path"]` 指向的 JSON：

- baseline：`configs/runner_profiles.json`
- asterinas：`configs/runner_profiles.asterinas.json`
- asterinas_scml：`configs/runner_profiles.asterinas_scml.json`

### 3.1 当前 profile 字段

| 字段 | 含义 |
| --- | --- |
| `kind` | `local` 或 `command` |
| `role` | `reference` / `candidate` |
| `snapshot_id` | 当前快照/运行环境标识 |
| `work_root` | sandbox 根目录 |
| `kernel_build_command` | 运行后用于采样内核版本/标识 |
| `binary_name` | command runner 使用的二进制名字 |
| `timeout_sec` | runner 自身超时覆盖 |
| `command` | command runner 的执行模板 |
| `batch_command` | 当前仅作为“是否允许 candidate batching”的存在性标志 |
| `controlled_divergence` | 受控 divergence 注入配置 |

### 3.2 当前耦合点

- Asterinas candidate 当前通过 `command` 直接调用 `tools/run_asterinas.py`；
- `candidate_batching_enabled()` 仍以 `workflow.startswith("asterinas")` 判断是否允许 batching；
- 虽然 profile 中存在 `batch_command`，但当前批量路径实际上走 `execute_candidate_batch_with_context()`，并没有把 batch manifest 真正交给 runner 执行。

这部分耦合正是后续抽象层要去掉的内容，但在重构前属于既有契约。

## 4. 当前流水线契约

三条 workflow 当前共享一条主链，只是在 corpus 派生、candidate runner 和 SCML 预检上存在分叉。

### 4.1 统一阶段

1. 准备目录与工具链；
2. 生成/导入 `*.syz` 程序；
3. 归一化并写入 `corpus/normalized/` 与 `corpus/meta/`；
4. 产生 `eligible_programs/*.jsonl`；
5. `tools/prog2c_wrap.py` 生成 testcase C、instrumented C、可执行文件与 `build-result.json`；
6. `orchestrator/scheduler.py` 调度 `reference` / `candidate`；
7. `orchestrator/vm_runner.py` 组织 sandbox、环境变量、artifact 路径并执行 runner；
8. `analyzer/normalize.py` 从 `raw-trace.json` + `external-state.json` 生成 `canonical-trace.json`；
9. `analyzer/compare.py` / `analyzer/classify.py` 生成分类与 comparison；
10. `scheduler.py` 先写 `campaign-results.jsonl` 与中间 summary，再调用 `tools/render_summary.py` 覆盖最终 `summary.json` / `summary.md` / `signoff.md`，最后写 `failure-report.*` 与 `divergence-index.jsonl`。

### 4.2 当前目录约定

| 类别 | baseline | asterinas | asterinas_scml |
| --- | --- | --- | --- |
| testcase build | `build/testcases` | `build/asterinas/testcases` | `build/asterinas_scml/testcases` |
| run artifacts | `artifacts/runs` | `artifacts/runs/asterinas` | `artifacts/runs/asterinas_scml` |
| reports | `reports/baseline` | `reports/asterinas` | `reports/asterinas_scml` |
| eligible list | `eligible_programs/baseline.jsonl` | `eligible_programs/asterinas.jsonl` | `eligible_programs/asterinas_scml.jsonl` |

当前路径语义是“workflow 名字决定目录布局”，还不是计划中的 `targets/<target>/<workflow>`。

## 5. 当前 artifact 生产顺序

### 5.1 build 阶段

每个 testcase build 根目录当前会出现：

- `testcase.c`
- `testcase.instrumented.c`
- `testcase.bin`
- `testcase.candidate.bin`（command runner 需要时）
- `build-result.json`

### 5.2 run 阶段

每个 side 的 artifact 目录当前会出现：

- `stdout.txt`
- `stderr.txt`
- `console.log`
- `raw-trace.events.jsonl`（runner 只写 events 时使用）
- `raw-trace.json`
- `external-state.json`
- `runner-result.json`（runner 写回）
- `run-result.json`（orchestrator 归一化写回）
- `canonical-trace.json`（仅在 trace+state 均可用时生成）

### 5.3 report 阶段

workflow report 目录当前会出现：

- `campaign-results.jsonl`
- `summary.json`
- `summary.md`
- `signoff.md`
- `failure-report.json`
- `failure-report.md`
- `divergence-index.jsonl`
- `baseline-invalid.jsonl`
- `unsupported-feature.jsonl`
- `bug_likely/` 相关二级报告

## 6. 当前已知耦合（重构时必须保持行为但允许移动实现）

这些行为是“当前事实”，不是目标状态：

1. `orchestrator/scheduler.py` 当前仍通过 workflow 名字判断 Asterinas candidate batching；
2. `orchestrator/vm_runner.py` 当前直接 import `tools.run_asterinas`，并硬编码 `artifacts/asterinas/initramfs-packages`；
3. `orchestrator/vm_runner.py` 当前直接下发 `SYZABI_ASTERINAS_PACKAGE_DIR` / `SYZABI_ASTERINAS_PACKAGE_SLOT`；
4. `tools/run_asterinas.py` 当前既负责 build probe，也负责 Docker/QEMU/host-direct 运行、initramfs 组装、输出解析与 runner-result 回写；
5. Asterinas / SCML config 当前重复携带 target-specific 字段与 workflow-specific 路径。

Phase 0 的目标不是“修掉这些问题”，而是先把这些现状固化成可审计文档与回归基线。
