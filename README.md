# SyzABI

> Compatibility alias: **FuzzAsterinas** remains the historical repository/workflow name during the migration window.

SyzABI 是一个面向 `syzkaller` 程序的离线差分回放框架。它的目标不是直接做在线 coverage-guided fuzzing，而是把 Linux `*.syz` 程序整理为可重放、可构建、可执行、可比较的 testcase，然后在 `reference` 与 `candidate` 两侧顺序执行，收集 trace、归一化、做语义比较，并产出可复盘的报告。

当前仓库仍保留大量 `FuzzAsterinas` / `asterinas*` 命名与入口作为兼容层，但对外定位已经开始收敛为：

- **项目名：SyzABI**
- **当前已接入 target：Asterinas**
- **现有兼容 workflow：`baseline` / `asterinas` / `asterinas_scml`**

当前仓库重点覆盖三条工作流：

| Workflow | 作用 | 典型对比 |
| --- | --- | --- |
| `baseline` | 建立 Linux vs Linux 的稳定基线 | Linux `reference` vs Linux `candidate` |
| `asterinas` | 将稳定基线扩展到 Asterinas bring-up | Linux `reference` vs Asterinas `candidate` |
| `asterinas_scml` | 基于 Asterinas SCML 能力约束做更细粒度筛选与批量差分 | Linux `reference` vs Asterinas `candidate` |

仓库当前固定的关键版本：

- `syzkaller`: `5b92003d577daa0766edda7ed533d75e1ac545ff`
- `Asterinas`: `f05e89b615c5dcb3f7c74accf24bdc23f96fcfc3`
- Asterinas Docker image: `asterinas/asterinas:0.17.1-20260317`

## 项目定位

这个项目解决的问题是：如何把一批 Linux `syzkaller` 程序变成面向 ABI/语义差异分析的证据链，而不是一次性脚本跑完就结束。

它目前已经包含这些核心能力：

- 固定 `syzkaller` revision，并自动准备 Go 工具链、`syz-prog2c` 和本仓库自带的小工具。
- 导入、归一化、去重 `*.syz` 程序，生成稳定的 `program_id` 和元数据。
- 按 allowlist 和稳定性规则筛选 corpus，生成可批量处理的 `eligible_programs/*.jsonl`。
- 把 `syz-prog2c` 生成的 C 程序自动包裹成统一的 `traced_syscall(...)` 执行形式。
- 顺序执行 `reference` / `candidate`，收集 stdout、stderr、raw trace、external state、canonical trace。
- 对结果做分类，当前支持 `NO_DIFF`、`BASELINE_INVALID`、`WEAK_SPEC_OR_ENV_NOISE`、`UNSUPPORTED_FEATURE`、`BUG_LIKELY`。
- 渲染 summary、sign-off、divergence index，并支持最小化报告。
- 在 Asterinas 场景下复用同一条 orchestrator/analyzer 主链，而不是另起一套执行框架。

迁移说明：

- 旧名称 `FuzzAsterinas` 目前仍会在目录、脚本、测试和兼容入口中出现。
- 结构迁移与 PR 拆分见 `docs/architecture/migration-plan.md`。
- 新 target 接入入口见 `docs/architecture/new-target-onboarding.md`。

明确不在当前范围内的事情：

- 不接入 `syz-manager` 做在线 fuzzing。
- 不实现完整的 `GOOS=asterinas` syzkaller target。
- 不移植完整 `syz-executor`。
- 不把这个仓库定位成通用内核模糊测试平台；它首先是一个差分回放与证据产出框架。

## 处理流程

三条 workflow 共享大致相同的流水线：

1. 准备工具链和目录骨架。
2. 生成或导入 `*.syz` 程序。
3. 提取元数据并写入 `corpus/meta/`。
4. 依据配置进行筛选或派生，生成 `eligible_programs/*.jsonl`。
5. 用 `syz-prog2c` 把合格程序转成 C，再做 wrapper 包装和本地编译。
6. 顺序执行 `reference` 和 `candidate`。
7. 收集 raw trace 与外部状态，生成 canonical trace。
8. 比较两侧结果，按 taxonomy 分类。
9. 产出 `summary.json`、`summary.md`、`signoff.md`、`divergence-index.jsonl` 以及最小化报告。

对 baseline 而言，两侧都可以是 Linux，从而先验证框架本身稳定。对 Asterinas 而言，`reference` 仍是 Linux，`candidate` 由 [`targets/asterinas/adapter.py`](./targets/asterinas/adapter.py) 暴露的 target adapter 驱动；[`tools/run_asterinas.py`](./tools/run_asterinas.py) 仅保留为兼容 CLI 入口。

## 仓库结构

```text
.
├── agent/                 # guest 侧 syscall trace 采集逻辑
├── analyzer/              # trace schema、canonicalization、compare、classify
├── cmd/                   # Go 小工具：inspect / generate / mutate
├── compat_specs/          # Asterinas SCML 相关清单与生成配置
├── configs/               # 兼容配置 + canonical workflows/targets 配置
├── docs/                  # 设计文档、runbook、bring-up 计划
├── orchestrator/          # 调度、稳定性策略、runner、模型定义
├── tools/                 # 导入、筛选、构建、运行、报告、预检脚本
├── tests/                 # 单元测试和回归测试
├── third_party/           # syzkaller / Asterinas 工作树
├── corpus/                # 原始、归一化、元数据、拒绝记录
├── eligible_programs/     # 各 workflow 的可执行 JSONL 列表
├── build/                 # testcase C 与可执行产物
├── artifacts/             # 运行时产物、sandbox、preflight、toolchain
└── reports/               # summary / signoff / minimized report 等
```

几个关键目录约定：

- `corpus/raw/`: 导入后的原始程序副本。
- `corpus/normalized/`: 归一化后的 `*.syz` 程序。
- `corpus/meta/`: 每个程序对应的 JSON 元数据。
- `corpus/rejected/`: 导入阶段失败或拒绝的记录。
- canonical eligible lists:
  - `eligible_programs/targets/linux/baseline/default.jsonl`
  - `eligible_programs/targets/asterinas/asterinas/default.jsonl`
  - `eligible_programs/targets/asterinas/asterinas_scml/{targets,generated,static,default}.jsonl`
- canonical build roots:
  - `build/targets/linux/baseline/testcases/`
  - `build/targets/asterinas/asterinas/testcases/`
  - `build/targets/asterinas/asterinas_scml/testcases/`
- canonical artifact roots:
  - `artifacts/runs/targets/linux/baseline/`
  - `artifacts/runs/targets/asterinas/asterinas/`
  - `artifacts/runs/targets/asterinas/asterinas_scml/`
- canonical report roots:
  - `reports/targets/linux/baseline/`
  - `reports/targets/asterinas/asterinas/`
  - `reports/targets/asterinas/asterinas_scml/`
- canonical preflight/generated roots:
  - `artifacts/preflight/targets/asterinas/asterinas_scml/`
  - `artifacts/generated/targets/asterinas/asterinas_scml/`

迁移窗口内，旧的 `eligible_programs/*.jsonl`、`build/asterinas*`、`artifacts/runs/asterinas*`、`reports/asterinas*` 路径仍可能作为兼容 shim 或历史产物出现。

## 环境要求

### 通用要求

建议在 Linux `x86_64` 主机上使用，仓库当前配置固定为 `amd64`。

宿主机至少需要这些工具：

- `bash`
- `python3`
- `make`
- `git`
- `curl`
- `tar`
- `gcc` 或等价 C 编译器
- `strace`

说明：

- `make bootstrap` 会自动下载并固定 Go `1.26.0` 到 `artifacts/toolchains/go/current/go`，不依赖系统全局 Go。
- baseline 路径会自动准备 `third_party/syzkaller` 并校验 pinned revision。
- Asterinas 路径要求 `third_party/asterinas` 已存在，且 revision 与配置匹配。

### Asterinas 额外要求

如果要运行 `asterinas` 或 `asterinas_scml` workflow，还需要：

- `docker`
- `qemu-system-x86_64`
- 推荐可用 `/dev/kvm`
- 能构建或运行 Asterinas 所需的宿主环境

默认 runner 模式是 `docker-qemu`。也可以通过环境变量 `SYZABI_ASTERINAS_MODE` 选择 `unconfigured`、`local-proxy`、`host-direct`、`docker-qemu`，但仓库当前主路径显然是 `docker-qemu`。

### SCML 额外要求

如果要运行 `asterinas_scml` 预检，还需要：

- Asterinas 仓库内的 SCML 规则目录存在
- `sctrace` 可执行文件

`sctrace` 的查找优先级大致是：

1. `third_party/asterinas/tools/sctrace/target/release/sctrace`
2. `third_party/asterinas/tools/sctrace/target/debug/sctrace`
3. `PATH` 中的 `sctrace`

## 快速开始

### 1. baseline 最小可运行路径

首次准备：

```bash
make bootstrap
make init-layout
```

生成并导入一批程序：

```bash
make generate-corpus
make import-corpus
make filter-corpus
make build-eligible
```

执行 smoke：

```bash
make run-smoke
```

执行 full：

```bash
make run-full
```

如果你只想在已有 `campaign-results.jsonl` 基础上重算 summary：

```bash
make analyze
```

生成受控 divergence 的最小化报告样例：

```bash
make report
```

baseline 的主要输出包括：

- `eligible_programs/targets/linux/baseline/default.jsonl`
- `build/targets/linux/baseline/testcases/`
- `artifacts/runs/targets/linux/baseline/`
- `reports/targets/linux/baseline/summary.json`
- `reports/targets/linux/baseline/summary.md`
- `reports/targets/linux/baseline/signoff.md`
- `reports/targets/linux/baseline/minimized-report.json`
- `reports/targets/linux/baseline/minimized-report.md`

### 2. Asterinas bring-up 路径

先确保 Asterinas 工作树已准备好，并处于固定 revision：

```bash
git clone https://github.com/asterinas/asterinas.git third_party/asterinas
git -C third_party/asterinas checkout f05e89b615c5dcb3f7c74accf24bdc23f96fcfc3
```

从 baseline 派生 Asterinas corpus：

```bash
make derive-asterinas
```

对 candidate 环境做健康检查：

```bash
make prepare-asterinas-candidate
```

构建 testcase：

```bash
make build-asterinas
```

执行 smoke / full：

```bash
make run-asterinas-smoke
make run-asterinas-full
```

渲染 summary 或生成报告：

```bash
make analyze-asterinas
make report-asterinas
```

默认并发数由 `ASTERINAS_JOBS` 控制，默认值为 `4`：

```bash
ASTERINAS_JOBS=8 make run-asterinas-smoke
```

Asterinas 相关重要输出：

- `eligible_programs/targets/asterinas/asterinas/default.jsonl`
- `artifacts/runs/targets/asterinas/asterinas/`
- `artifacts/targets/asterinas/build-info.json`
- `reports/targets/asterinas/asterinas/summary.json`
- `reports/targets/asterinas/asterinas/signoff.md`

### 3. Asterinas SCML 路径

SCML workflow 用于把 Asterinas 的 syscall-compatibility 信息显式纳入 corpus 生成和预检，而不是只依赖运行时失败。

构建 manifest：

```bash
make build-asterinas-scml-manifest
```

执行完整派生链：

```bash
make derive-asterinas-scml
```

只重跑 `prog2c` 包装 + preflight：

```bash
make preflight-asterinas-scml
```

批量执行 smoke/full 时，目前使用 `scheduler.py` 直调更明确：

```bash
python3 orchestrator/scheduler.py --workflow asterinas_scml --campaign smoke --limit 100 --jobs 8
python3 orchestrator/scheduler.py --workflow asterinas_scml --campaign full --limit 500 --jobs 8
python3 tools/render_summary.py --workflow asterinas_scml
python3 tools/reduce_case.py --workflow asterinas_scml --fixture controlled_divergence
```

SCML 路径的关键中间产物：

- `compat_specs/asterinas/scml-manifest.json`
- `eligible_programs/targets/asterinas/asterinas_scml/targets.jsonl`
- `eligible_programs/targets/asterinas/asterinas_scml/generated.jsonl`
- `eligible_programs/targets/asterinas/asterinas_scml/static.jsonl`
- `eligible_programs/targets/asterinas/asterinas_scml/default.jsonl`
- `artifacts/preflight/targets/asterinas/asterinas_scml/`
- `reports/targets/asterinas/asterinas_scml/`

## 常用命令

### Makefile 入口

最常用的入口已经通过 `Makefile` 暴露出来：

```bash
make bootstrap
make init-layout
make generate-corpus
make import-corpus
make filter-corpus
make build-eligible
make run-smoke
make run-full
make analyze
make report

make derive-asterinas
make prepare-asterinas-candidate
make build-asterinas
make run-asterinas-smoke
make run-asterinas-full
make analyze-asterinas
make report-asterinas

make build-asterinas-scml-manifest
make derive-asterinas-scml
make preflight-asterinas-scml
make test
make clean
```

### 直接脚本入口

当你需要缩小范围排查某一个 case 或某个阶段时，直接调用脚本更方便：

```bash
python3 tools/import_syz.py --input-dir corpus/input/generated --source-type generated
python3 tools/prog2c_wrap.py --workflow asterinas --program-id <program_id>
python3 orchestrator/scheduler.py --workflow baseline --campaign smoke --limit 20
python3 tools/render_summary.py --workflow asterinas
python3 tools/reduce_case.py --workflow asterinas --program-id <program_id>
```

几个有用的 Go 工具：

- `build/bin/syzabi_inspect`: 解析单个 `*.syz` 程序，输出稳定 `program_id`、syscall 列表和资源信息。
- `build/bin/syzabi_generate`: 按 allowlist 生成确定性的测试程序集合。
- `build/bin/syzabi_mutate`: 预留给程序变异和生成相关实验。

## 配置方式

运行时配置主要由 `configs/*.json` 决定。当前仓库同时存在：

- **canonical 布局**（推荐）
- **legacy shim 布局**（兼容）

canonical workflow 配置：

- `configs/workflows/baseline.json`
- `configs/workflows/asterinas.json`
- `configs/workflows/asterinas_scml.json`

canonical target 配置：

- `configs/targets/asterinas/target.json`
- `configs/targets/asterinas/runner_profiles.asterinas.json`
- `configs/targets/asterinas/runner_profiles.asterinas_scml.json`

legacy shim 配置：

- `configs/baseline_rules.json`
- `configs/asterinas_rules.json`
- `configs/asterinas_scml_rules.json`

这些配置控制：

- 工作流名称与输出目录。
- `syzkaller`、Asterinas 的固定版本。
- allowlist / denylist / rejection taxonomy。
- 构建参数与 timeout。
- 并发度和 sign-off 门槛。
- runner profile 路径。

legacy runner profile shim 另存于：

- `configs/runner_profiles.json`
- `configs/runner_profiles.asterinas.json`
- `configs/runner_profiles.asterinas_scml.json`

它们定义了 `reference` / `candidate` 的执行方式，例如本地执行还是命令型 runner、sandbox 根目录、超时和 batch 命令格式。

当前迁移窗口内，以下兼容入口会输出 deprecation 提示：

- `configs/*_rules.json`
- `configs/runner_profiles.asterinas*.json`
- `build-eligible`
- `derive-asterinas*` / `preflight-asterinas-scml`
- `prepare-asterinas-candidate`
- `run-asterinas-*` / `analyze-asterinas` / `report-asterinas`

几个常用环境变量：

- `SYZABI_WORKFLOW`: 显式指定当前 workflow。
- `SYZABI_CONFIG_PATH`: 覆盖默认配置文件路径。
- `SYZABI_TMPDIR`: 覆盖临时目录。
- `SYZABI_ASTERINAS_MODE`: 选择 Asterinas runner 模式。
- `ASTERINAS_JOBS`: `make run-workflow WORKFLOW=asterinas ...`（以及兼容 alias `run-asterinas-*`）的并发数。

示例：

```bash
SYZABI_WORKFLOW=asterinas python3 tools/render_summary.py
SYZABI_CONFIG_PATH=configs/workflows/asterinas_scml.json python3 orchestrator/scheduler.py --campaign smoke --limit 50
SYZABI_ASTERINAS_MODE=docker-qemu make prepare-target WORKFLOW=asterinas
# legacy compatibility alias:
SYZABI_ASTERINAS_MODE=docker-qemu python3 tools/run_asterinas.py --healthcheck
```

## 测试与验证

运行全部测试：

```bash
make test
```

当前测试主要覆盖这些方面：

- wrapper 是否接管所有 syscall 调用点。
- corpus filter / derivation / SCML preflight 的分类规则。
- canonicalization 和 compare 的等价性判断。
- Asterinas runner 的命令拼装、环境注入和状态映射。
- summary/report 相关辅助逻辑。

如果你修改了配置、过滤规则或 runner 行为，至少应重跑：

```bash
make test
make run-smoke
```

## 项目历史计划文档

如果你第一次看这个仓库，建议按这个顺序读文档：

1. 本 README
2. [`docs/linux-baseline-runbook.md`](./docs/linux-baseline-runbook.md)
3. [`docs/LinuxBaseline.md`](./docs/LinuxBaseline.md)
4. [`docs/AsterinasBringup.md`](./docs/AsterinasBringup.md)
5. [`docs/asterinas-scml-diff-plan.md`](./docs/asterinas-scml-diff-plan.md)

## 常见问题

### `make bootstrap` 失败

优先检查这些项：

- 宿主机能否访问 `dl.google.com` 和 `github.com`
- `curl` / `tar` / `git` 是否存在
- `third_party/syzkaller` 是否被本地改坏
- `artifacts/toolchains/go/current/go` 是否被部分写入

### Asterinas runner 报 revision mismatch

这通常表示 `third_party/asterinas` 的当前 HEAD 与 Asterinas target config 中的 pinned revision 不一致。直接对齐到 pinned revision 即可：

```bash
git -C third_party/asterinas checkout f05e89b615c5dcb3f7c74accf24bdc23f96fcfc3
```

### Asterinas 健康检查或运行失败

优先检查：

- `docker` 是否可用
- QEMU 是否安装
- `/dev/kvm` 是否可访问
- `third_party/asterinas/test/initramfs/build/initramfs.cpio.gz` 是否存在
- `artifacts/targets/asterinas/build-info.json` 是否与当前 revision / mode 对齐

### SCML preflight 失败

优先检查：

- `strace` 是否安装
- `sctrace` 是否可执行
- `third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage/` 是否存在
- `reports/targets/asterinas/asterinas_scml/debug-preflight/` 和 `artifacts/preflight/targets/asterinas/asterinas_scml/` 中的证据文件

## 清理

仓库提供了受控清理入口：

```bash
make clean
```

它会清理 Asterinas / Asterinas SCML 运行产物、sandbox、构建目录和部分派生文件，但不会暴力重置 Git 工作树。

## 当前边界

从代码和配置来看，这个仓库当前最适合以下使用场景：

- 建立可复现的 Linux baseline。
- 做 Asterinas bring-up 期间的差分回放验证。
- 基于 SCML 能力约束构造更可靠的 Asterinas corpus。
- 产出可审计、可重放的 divergence 证据。

如果你要把它扩展到新的 candidate OS，建议优先复用现有抽象：

- 保持 `reference` / `candidate` 双侧接口不变。
- 复用 `orchestrator`、`analyzer` 和 `tools/prog2c_wrap.py` 主链。
- 只在 runner、agent、config、capability gating 层扩展。
