# Asterinas bring-up 实施计划：Asterinas Candidate 接入（Linux vs Asterinas 顺序差分）

## Linux baseline 已完成状态

在进入 Asterinas bring-up 之前，Linux baseline 已经完成并通过签收。当前仓库内已经具备的能力如下。

1. 已固定 syzkaller revision，并提供可复现的 bootstrap 与目录初始化入口。
2. 已实现 Program Source Manager，支持 `.syz` 导入、去重、元数据提取、归一化落盘与拒绝记录。
3. 已实现 Corpus Filter，且规则已收敛为 exact full-name allowlist，不再放行 `openat$...`、`read$...` 等 specialized variant。
4. 已实现 `syz-prog2c` 转换、post-processing 包装与 `traced_syscall(...)` 统一接管链路。
5. 已实现 Linux agent、raw trace 采集、external state 采样与 canonical trace 生成。
6. 已实现 Analyzer v1，支持 `NO_DIFF`、`BASELINE_INVALID`、`WEAK_SPEC_OR_ENV_NOISE`、`UNSUPPORTED_FEATURE`、`BUG_LIKELY` 分类。
7. 已实现“正常单次跑 + 异常重跑”的稳定性策略，以及 `reference` / `candidate` 双侧抽象。
8. 已实现最小 reducer/report 闭环，最小化报告会同时给出原始 event index 与映射回 `.syz` 的 syscall index。
9. 已完成 1000-case Linux baseline full run，并输出 summary、baseline-invalid 列表、divergence index 与最小化报告。

截至 2026-03-21，Linux baseline 当前签收结果如下。

1. `eligible_program_count=1400`
2. `import_success_rate=1.000`
3. `build_success_rate=1.000`
4. `dual_execution_completion_rate=0.925`
5. `trace_generation_success_rate=1.000`
6. `canonicalization_success_rate=1.000`
7. `baseline_invalid_rate=0.075`
8. `signoff_pass=true`

对应产物已经落盘：

1. `eligible_programs/baseline.jsonl`
2. `reports/baseline/summary.json`
3. `reports/baseline/signoff.md`
4. `reports/baseline/minimized-report.json`
5. `reports/baseline/minimized-report.md`

因此，Asterinas bring-up 不再讨论“Linux baseline 基础链路是否成立”，而是直接基于现有基线做 candidate 替换与目标 OS 接入。

## 目标描述

### Asterinas bring-up 的唯一目标

Asterinas bring-up 的目标不是完整移植 syzkaller 到 Asterinas，也不是上线 coverage-guided fuzzing，而是把当前已经稳定的 Linux baseline 差分框架扩展为 `Linux reference vs Asterinas candidate`，验证以下三件事：

1. Linux baseline 已稳定的 Linux `.syz` 程序，是否能在 Asterinas 上被批量重放、构建、执行并产出可比较 trace。
2. 现有 runner、collector、normalization、analyzer 抽象是否足以承接 Asterinas，而不需要重写整条 orchestrator/analyzer 管线。
3. 在 `Linux vs Asterinas` 条件下，系统是否能把 `UNSUPPORTED_FEATURE` 与真实 ABI divergence 区分开，并输出可复现报告。

### Asterinas bring-up 完成后的成功状态

Asterinas bring-up 结束时，仓库应具备以下能力：

1. 能固定一个 Asterinas revision，并可重复构建、启动、回滚 candidate 镜像。
2. 能把 Linux baseline 的稳定 corpus 派生为 `eligible_programs/asterinas.jsonl`，明确其来源和筛选规则。
3. 能在 Linux `reference` 与 Asterinas `candidate` 间顺序执行同一 testcase，并落盘两侧 artifacts。
4. 能让 Asterinas 侧产出与 Linux baseline schema 兼容的 raw trace、canonical trace 和 external state。
5. 能在 Linux 成功时稳定识别 Asterinas 的 `UNSUPPORTED_FEATURE`、执行失败与语义差异。
6. 能对至少 200 个 Asterinas bring-up testcase 进行批量跑数，并输出 summary、reason taxonomy 与 divergence index。
7. 能对至少 1 个 `Linux vs Asterinas` 差异样例输出可复现、可追溯的最小化报告。

## Asterinas bring-up 的输入

1. `eligible_programs/baseline.jsonl` 中的 Linux baseline 稳定 corpus。
2. 现有 `prog2c` 包装链、trace schema、canonicalization 规则与 analyzer v1。
3. 一个固定 revision 的 Asterinas 源码、镜像构建流程和可回滚运行环境。
4. 一组明确写死的 Asterinas bring-up allowlist、unsupported 规则和 runner 配置。

## Asterinas bring-up 的输出

1. `AsterinasBringup.md`
2. `configs/asterinas_rules.json`
3. `eligible_programs/asterinas.jsonl`
4. `reports/asterinas/summary.json`、`summary.md`、`signoff.md`
5. `reports/asterinas/divergence-index.jsonl`
6. `reports/asterinas/unsupported-feature.jsonl`
7. 至少 1 份 `Linux vs Asterinas` 最小化报告
8. Asterinas image build log、boot log、candidate run artifacts 与 trace 证据路径

## Asterinas bring-up 的非目标

以下内容明确不属于 Asterinas bring-up：

1. 不新增 syzkaller `GOOS=asterinas` target。
2. 不移植完整 `syz-executor`。
3. 不引入 `syz-manager` 在线 fuzzing。
4. 不接入 DragonOS。
5. 不在 Asterinas bring-up 内实现完整 reducer/report 平台增强。
6. 不把所有 Linux baseline syscall 一次性扩展到 Asterinas；允许先收敛到一个更小的 proven-supported 子集。

## Asterinas bring-up 固定工程决策

以下决策在本阶段写死：

1. `reference` 侧仍然使用当前 Linux Linux baseline runner，不改语义，不引入额外变量。
2. `candidate` 侧替换为 Asterinas，但外部接口继续复用 `RunResult`、scheduler、collector、analyzer 主链。
3. 程序来源仍然固定为 Linux `.syz` 程序，不维护 Asterinas 自己的 syscall 描述文件。
4. 主执行路线仍然固定为 `syz-prog2c` + 自定义 trace wrapper，不改为 `syz-execprog` 主路线。
5. Asterinas bring-up 的 testcase 入口固定来源于 `eligible_programs/baseline.jsonl`，然后再做 Asterinas-specific derivation，而不是重新放宽 Linux baseline 边界。
6. 若 Linux `reference` 本身不稳定，则仍然直接标记为 `BASELINE_INVALID`，不得拿去评估 Asterinas。
7. 若 Linux 成功而 Asterinas 明确返回 `ENOSYS`、未实现、显式 capability 缺失或已知 unsupported 路径，则归为 `UNSUPPORTED_FEATURE`。
8. 若 Linux 成功而 Asterinas 崩溃、超时、或 syscall/output/final-state 存在稳定语义差异，则进入 `BUG_LIKELY` 或 `WEAK_SPEC_OR_ENV_NOISE` 评估流程。

## Asterinas bring-up 完成定义

只有同时满足以下条件，Asterinas bring-up 才算完成：

1. 主仓库中已有固定 Asterinas revision、可复现的镜像构建入口与 candidate runner。
2. `eligible_programs/asterinas.jsonl` 已可稳定生成，且来源能追溯回 Linux baseline stable corpus。
3. Linux 与 Asterinas 双执行链路已跑通，且 artifact、trace、canonical trace 均可落盘。
4. 已完成至少 200 个 Asterinas bring-up testcase 的批量评估，并输出 summary、unsupported 列表和 divergence index。
5. 至少 1 份 `Linux vs Asterinas` 差异报告可由脚本重放验证。
6. 签收 summary 中的阶段门槛全部达标。

## 验收标准

### AC-1：Asterinas revision、构建环境与运行镜像固定

- 要求：
  - 必须把 Asterinas 固定到具体 revision，而不是浮动分支。
  - 必须存在一键或少量步骤即可重建 candidate 镜像的入口。
  - 必须记录 image、kernel、rootfs、boot 参数与运行 profile。
- Positive Tests：
  - 全新环境执行 bootstrap/build 脚本后，能生成可启动的 Asterinas candidate 镜像。
  - 再次执行构建脚本时，不会漂移到其他 revision。
  - 运行 profile 中能明确看到 snapshot_id、kernel_build、image_path 等信息。
- Negative Tests：
  - 若只写 `main` 或未记录 revision，构建入口必须报错。
  - 若镜像生成依赖人工临时步骤，阶段验收不得通过。

### AC-2：Candidate Runner 能启动 Asterinas、回滚环境并稳定收集 artifacts

- 要求：
  - 必须提供 Asterinas candidate 的 boot、执行、回滚、超时控制与 artifact 回收能力。
  - 执行协议仍固定为先 `reference` 后 `candidate`。
  - 每次 testcase 必须有独立工作目录和独立 artifact 目录。
- Positive Tests：
  - 一个最小 testcase 能在 Linux `reference` 成功后被派发到 Asterinas `candidate` 执行。
  - `stdout`、`stderr`、console log、raw trace、external state 均能回收到主机侧。
  - artifact 路径中能稳定看到 `program_id`、`side`、`run_id`。
- Negative Tests：
  - 若 Asterinas boot 失败或 snapshot 未回滚，runner 自检必须失败。
  - 若 `reference` 不稳定，`candidate` 不得继续作为有效对照执行。

### AC-3：Asterinas Agent 与 Trace Schema 与 Linux baseline 兼容

- 要求：
  - Asterinas 侧必须产出与 Linux baseline raw trace schema 兼容的 trace。
  - 至少保留调用序号、syscall 名称、syscall number、参数、返回值、errno、开始/结束时间、输出缓冲区摘要和 `process_exit`。
  - 若确实需要扩展字段，必须保证 Linux baseline 解析器对旧字段保持兼容。
- Positive Tests：
  - `openat/read/close` 类 testcase 在 Asterinas 侧能生成结构合法的 raw trace。
  - canonicalization 对 Linux trace 与 Asterinas trace 均能成功。
  - 同一 candidate testcase 重跑后，schema 级字段稳定。
- Negative Tests：
  - 若 candidate trace 缺少关键字段，collector 不得静默接受。
  - 若 Asterinas trace 与 Linux trace 必须走两套不同 analyzer 主链，阶段验收不得通过。

### AC-4：Asterinas bring-up Corpus 必须从 Linux baseline stable corpus 派生，而不是重新放宽边界

- 要求：
  - `eligible_programs/asterinas.jsonl` 必须由 `eligible_programs/baseline.jsonl` 派生。
  - 允许进一步收缩为 “Linux baseline stable exact full-name subset ∩ Asterinas 可运行子集”。
  - 需要明确记录为什么某些 Linux baseline 程序在 Asterinas bring-up 中被过滤掉。
- Positive Tests：
  - `asterinas.jsonl` 中每个 program_id 都能追溯到 Linux baseline eligible 来源。
  - 对同一 Linux baseline corpus 连续运行两次派生逻辑，得到稳定一致的 `asterinas.jsonl`。
  - 被过滤程序会进入稳定 reason taxonomy，如 `unsupported_variant`、`candidate_build_gap`、`candidate_boot_blocker`。
- Negative Tests：
  - Asterinas bring-up 不得重新放行 Linux baseline 已过滤掉的 specialized syscall variant。
  - 若 `asterinas.jsonl` 含无法追溯来源的 testcase，阶段验收不得通过。

### AC-5：Analyzer 必须能把 unsupported、执行失败与真实语义差异分开

- 要求：
  - `UNSUPPORTED_FEATURE` 必须有规则化来源，而不是兜底桶。
  - Linux 成功而 Asterinas 明确未实现时，必须能稳定归类为 `UNSUPPORTED_FEATURE`。
  - Linux 与 Asterinas 都成功但输出/状态不同，必须进入语义差异分析。
- Positive Tests：
  - Linux 成功、Asterinas 明确 unsupported 的样例能稳定归类为 `UNSUPPORTED_FEATURE`。
  - Linux 成功、Asterinas timeout/crash 的样例会被保留为 candidate failure，而不是被吞掉。
  - 两边 canonical trace 等价时，结果仍为 `NO_DIFF`。
- Negative Tests：
  - 不得把所有 candidate 失败统一归为 `UNSUPPORTED_FEATURE`。
  - 不得因为路径、地址、PID 等 normalization 噪声直接判成 `BUG_LIKELY`。

### AC-6：Asterinas 接入后仍能输出可复盘的 evidence-quality 报告

- 要求：
  - divergence 报告必须保留 Linux/Asterinas 双侧证据路径。
  - 最小化报告继续输出原始 event index 与映射回 `.syz` 的 syscall index。
  - 报告必须给出可复现命令和 testcase 路径。
- Positive Tests：
  - 至少 1 个 `Linux vs Asterinas` 差异样例可生成 JSON + Markdown 报告。
  - 报告中的索引字段不会指到 `.syz` 程序长度之外。
  - 复跑脚本能再次观测到该 divergence。
- Negative Tests：
  - 若报告缺少双侧证据路径、运行命令或 testcase 路径，阶段验收不得通过。
  - 若最小化后 divergence 消失，reducer 必须回退，而不是提交错误结果。

### AC-7：需要有明确的 smoke run 与 sign-off run 门槛

- 推荐门槛：
  - smoke run（50 个样例）：
    - candidate boot success rate >= 95%
    - dual execution completion rate >= 80%
    - trace generation success rate >= 95%
    - canonicalization success rate = 100%（针对已有 raw trace）
  - sign-off run（200 个样例）：
    - Asterinas bring-up testcase build success rate >= 95%
    - dual execution completion rate >= 85%
    - trace generation success rate >= 95%
    - canonicalization success rate = 100%
    - `reference` baseline-invalid rate < 10%
    - 至少 1 份 `Linux vs Asterinas` 报告成功产出并可复跑
- Positive Tests：
  - smoke run 达标后允许进入 sign-off run。
  - sign-off run 结束后，summary 自动判定阶段是否通过。
  - `NO_DIFF`、`UNSUPPORTED_FEATURE`、`BUG_LIKELY`、`WEAK_SPEC_OR_ENV_NOISE` 分布可统计、可回溯。
- Negative Tests：
  - 如果未达到 200 个合格 Asterinas bring-up 样例，不得宣称 Asterinas bring-up 完成。
  - 如果没有可复现报告，阶段验收不得通过。

## 建议实施顺序

建议按以下顺序推进 Asterinas bring-up：

1. 固定 Asterinas revision、镜像构建脚本和运行 profile。
2. 实现 candidate runner 的 boot、回滚、artifact 回收。
3. 让 Asterinas 侧先跑通一个最小 `openat -> close` testcase，并产出兼容 trace。
4. 在 `eligible_programs/baseline.jsonl` 基础上派生 `eligible_programs/asterinas.jsonl`。
5. 跑 20 到 50 个 smoke testcase，收敛 boot、agent、schema、unsupported taxonomy。
6. 跑 200 个 sign-off testcase，输出 summary、unsupported 列表、divergence index 和最小化报告。

## 路径边界

### 上界

Asterinas bring-up 的理想上界是：

1. 不改 analyzer 主链，只在 runner/agent/config 层扩展出 Asterinas candidate。
2. 支持 Asterinas bring-up corpus derivation、smoke run、sign-off run 和 divergence report。
3. 对 unsupported、candidate failure、语义差异都有稳定 taxonomy 和证据链。

### 下界

Asterinas bring-up 的最小可接受下界是：

1. Asterinas image 能稳定启动。
2. 一个最小 syscall 子集 testcase 能在 Linux/Asterinas 两侧双执行。
3. candidate trace 能被 canonicalize 并参与比较。
4. 至少 1 个 `Linux vs Asterinas` 样例能输出可复盘报告。

如果连上述下界都不满足，就不能宣称 Asterinas bring-up 已开始进入“批量差分”阶段，只能算 candidate bring-up。

## 进入 DragonOS 接入的前提

只有当以下条件同时满足时，才建议进入 DragonOS 接入：

1. Asterinas bring-up 已完成签收。
2. Asterinas candidate runner、agent、report 链路已经稳定。
3. `UNSUPPORTED_FEATURE` 与真实 divergence 的边界已经清晰，不再大量依赖人工口头判断。
4. 已经证明当前 orchestrator/analyzer 抽象足以承接第二个 candidate，而不需要推翻重写。

如果这些前提不满足，过早进入 DragonOS 会把 candidate-specific 噪声和框架问题混在一起，显著抬高排障成本。
