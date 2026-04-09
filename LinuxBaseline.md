# Linux baseline 实施计划：Linux Baseline（Linux vs Linux 顺序差分基线）

## 目标描述

### Linux baseline 的唯一目标

Linux baseline 的目标不是接入 Asterinas，也不是把完整 syzkaller 移植到新 OS，而是先在 `Linux vs Linux` 条件下把整条“离线导入 -> 静态筛选 -> `syz-prog2c` 转换 -> 包装执行 -> trace 采集 -> 归一化 -> 差分分析 -> 最小报告产出”链路做实，验证三件事：

1. syzkaller Linux 程序是否能被稳定地整理成可运行、可追踪、可比较的测试单元。
2. trace schema、normalization 规则和 analyzer v1 是否足够稳定，能把噪声与真实差异分开。
3. 在没有目标 OS 干扰的前提下，系统能否识别并隔离 baseline-invalid workload，并产出至少 1 份完整的最小化报告闭环样例。

### Linux baseline 完成后的成功状态

Linux baseline 结束时，仓库应具备以下能力：

1. 能固定一个 syzkaller revision，并可重复构建 `syz-prog2c` 与必要辅助工具。
2. 能导入一批 Linux `.syz` 程序，完成去重、元数据提取、归一化落盘与拒绝原因记录。
3. 能依据 baseline allowlist 生成稳定的 `eligible_programs/baseline.jsonl`。
4. 能把合格 `.syz` 程序转成 C，再转成带统一 syscall wrapper 的可执行程序。
5. 能在两侧 Linux 基线环境中顺序执行同一 testcase，并完整收集原始执行产物。
6. 能把原始 trace 归一化为 canonical trace，并进行 syscall-level、resource-level、final-state-level 比较。
7. 能识别 `BASELINE_INVALID`、`WEAK_SPEC_OR_ENV_NOISE` 以及测试注入场景下的已知 divergence。
8. 能对至少 1 个 divergence 样例给出可复现、可追溯、包含证据路径的最小化报告。
9. 能对 1000 个 baseline corpus 运行批量评估，并输出量化指标与失败原因统计。

### Linux baseline 的输入

1. 一批 Linux syzkaller 程序，来源可以是已有 `.syz`、离线生成程序或 crash log 中抽出的程序。
2. 一个固定 revision 的 syzkaller 工具链。
3. 两个角色不同但内核/镜像可控的 Linux 执行环境。
4. 一组明确写死的 baseline allowlist、normalization 规则和分类规则。

### Linux baseline 的输出

1. `corpus/raw/`、`corpus/normalized/`、`corpus/meta/`、`corpus/rejected/`
2. `eligible_programs/baseline.jsonl`
3. `artifacts/` 或等价目录下的构建产物、运行日志、raw trace、canonical trace
4. `reports/baseline/` 下的 summary、baseline-invalid 列表、至少 1 份最小化报告
5. 可重复执行的脚本或命令入口，至少覆盖导入、筛选、构建、运行、分析、报告 6 个动作

### Linux baseline 的非目标

以下内容明确不属于 Linux baseline：

1. 不接入 Asterinas 或 DragonOS。
2. 不新增 syzkaller `GOOS` target。
3. 不使用 `syz-manager` 做在线 coverage-guided fuzzing。
4. 不依赖 `threaded=1`、`collide=1`、pseudo-syscalls 或复杂 executor 特殊逻辑。
5. 不追求完整 crash triage 平台，只做差分 replay 与基础报告闭环。
6. 不在 Linux baseline 内实现后续完整 reducer/report 平台化或 executor 适配。

### Linux baseline 固定工程决策

以下决策在本阶段不再讨论：

1. 程序来源固定为 Linux syzkaller programs，不维护目标 OS 自己的 syscall 描述。
2. 执行主路线固定为 `syz-prog2c` + 自定义 trace wrapper。
3. `syz-execprog` 仅用于开发验证，不作为正式执行器。
4. 全部 testcase 必须满足 `-threaded=0` 等价语义。
5. 全部 testcase 禁止依赖 collide。
6. baseline 过滤掉全部 pseudo-syscalls。
7. 只支持 `x86_64/amd64`。
8. Linux baseline 不稳定的程序直接标记为 `BASELINE_INVALID`，不进入“疑似 candidate bug”统计。
9. Linux baseline 外部接口保留 `reference` / `candidate` 双侧抽象，但具体实现允许两侧都指向 Linux，以便 asterinas 无缝替换 candidate。

### Linux baseline 完成定义

只有同时满足以下条件，Linux baseline 才算完成：

1. 主仓库中已有固定 syzkaller revision 与可复现构建入口。
2. Program Source Manager、Corpus Filter、`prog2c` 包装器、Linux agent、Trace Collector、Analyzer v1 均可独立执行。
3. 批量运行 1000 个 baseline 合格程序后，生成 summary、baseline-invalid 列表和可追溯 artifacts。
4. `baseline-invalid` 率低于约定阈值。
5. 至少 1 份完整最小化报告可由脚本重放验证。

## 验收标准

以下验收标准采用 TDD 思路组织。每个 AC 都必须有明确的正向验证与反向验证。

- AC-1：syzkaller revision、构建环境与目录骨架固定
  - 要求：
    - 必须把 syzkaller 版本固定到具体 commit，而不是浮动分支名。
    - 必须提供一键或少量步骤即可复现的工具构建入口。
    - 必须建立 baseline 所需的最小目录骨架与 artifact 约定。
  - Positive Tests（应通过）：
    - 全新环境执行 bootstrap 脚本后，能拉取指定 revision 并构建 `syz-prog2c`。
    - 再次执行 bootstrap 脚本时，不会切换到其他 revision，也不会产生不可解释的输出漂移。
    - 目录初始化命令执行后，`corpus/`、`eligible_programs/`、`artifacts/`、`reports/` 等路径全部存在。
  - Negative Tests（应失败或被拒绝）：
    - 若配置中只写 `master`、`main` 或未记录 commit hash，校验脚本必须报错。
    - 若本地 syzkaller revision 与锁定值不一致，构建入口必须拒绝继续。
    - 若目录骨架缺关键路径，后续步骤不得静默创建到临时随机目录。

- AC-2：Program Source Manager 能稳定导入、去重、归一化并产出可查询元数据
  - 要求：
    - `program_id` 必须基于归一化后的内容生成，确保内容等价时 ID 稳定。
    - 原始程序、归一化程序、元数据、拒绝记录必须分目录落盘。
    - 元数据中必须包含 `source`、`arch`、`syscall_list`、`uses_pseudo_syscalls`、`uses_threading_sensitive_features`、`original_path`。
  - Positive Tests（应通过）：
    - 同一内容、不同文件名的两个 `.syz` 导入后，只产生一个稳定 `program_id`。
    - 一个只含 `openat/read/close` 的合法程序可被导入，并在 `corpus/meta/` 生成元数据。
    - crash log 提取出的程序经归一化后仍能产生与原始 `.syz` 相同的 `program_id`。
  - Negative Tests（应失败或被拒绝）：
    - 语法损坏的 `.syz` 必须进入 `corpus/rejected/`，并记录 `parse_error`。
    - 缺少关键元数据字段的导入结果不得被标记为成功。
    - 仅文件路径不同但内容一致时，系统不得生成两个不同 ID。

- AC-3：Corpus Filter 能基于 baseline allowlist 产出稳定、可复现的 eligible 列表
  - 要求：
    - filter 规则必须由单一配置源定义，不能散落在代码多个位置。
    - 必须按三层筛选：语法可执行性、语义白名单、稳定性白名单。
    - `eligible_programs/baseline.jsonl` 的顺序必须稳定，可通过固定排序规则重现。
  - Positive Tests（应通过）：
    - 仅使用白名单 syscall 的单线程程序会进入 `baseline.jsonl`。
    - 包含 `mmap/munmap/mprotect` 但不依赖线程竞争的程序会被正确放行。
    - 对同一份 corpus 连续运行两次 filter，得到完全一致的 JSONL 内容和顺序。
  - Negative Tests（应失败或被拒绝）：
    - 含 pseudo-syscall 的程序必须被拒绝，原因中包含 `no_pseudo` 失败信息。
    - 使用 `epoll`、`io_uring`、复杂 network protocol、mount/namespace/privileged path 的程序必须被拒绝。
    - 任何需要 `threaded` 或 `collide` 才能工作的程序必须被拒绝。

- AC-4：`syz-prog2c` 转换与 post-processing 包装器链路可稳定生成可执行 testcase
  - 要求：
    - 每个 eligible `.syz` 必须先转为 `testcase.c`，再经 post-processor 改写成统一 wrapper 调用风格。
    - 必须存在自动校验，确保所有目标 syscall 调用点均被 wrapper 接管。
    - 构建结果必须保留中间产物，以便定位失败原因。
  - Positive Tests（应通过）：
    - 一个简单的 `openat -> read -> close` 程序可转换为 `testcase.c`、改写为 wrapper 风格并成功编译为 `testcase.bin`。
    - 校验工具能证明生成 C 中所有 baseline 目标 syscall 均经过 `traced_syscall(...)` 或等价接口。
    - 对同一 `.syz` 重复转换两次，得到的中间产物除时间戳字段外保持稳定。
  - Negative Tests（应失败或被拒绝）：
    - 如果 post-processor 遗漏某个 syscall 调用点，校验工具必须失败。
    - 如果 `syz-prog2c` 输出模式因 revision 变化而失配，构建流程必须显式报错，而不是生成部分可运行结果。
    - 编译失败时，任务不得被误记为运行失败，必须明确归类为 `build_failure`。

- AC-5：Linux Execution Agent 能记录 syscall 级输入输出与程序级异常信息
  - 要求：
    - trace 至少记录调用序号、syscall 名称、syscall number、参数原值、返回值、errno、开始/结束时间、是否超时、程序级异常信息。
    - 对有输出缓冲区的 syscall，至少记录输出长度、前 N 字节摘要和 SHA-256。
    - agent 必须在 guest 内工作，且输出格式稳定。
  - Positive Tests（应通过）：
    - `openat/read/close` 运行后，trace JSON 中能看到 3 个顺序一致的事件。
    - `read`/`pread` 等输出缓冲区 syscall 会写入长度、hex preview 与摘要哈希。
    - 进程正常退出时，`process_exit` 信息完整记录退出码。
  - Negative Tests（应失败或被拒绝）：
    - 程序超时时，trace 中必须留下超时标记与部分事件，而不是完全空文件。
    - 程序崩溃时，必须有异常状态与 console/stderr 证据路径，不能只返回非零状态码。
    - 如果 trace 缺少事件索引或索引不连续，schema 校验必须失败。

- AC-6：Dual Runner 能在两个 Linux 基线环境中顺序执行同一 testcase，并保证环境对齐
  - 要求：
    - 外部接口必须仍保留 `reference` / `candidate` 两侧抽象。
    - baseline 具体实现中两侧都可以是 Linux，但必须能区分 `role`、`snapshot_id`、`kernel_build`。
    - 每次执行前必须回滚快照，每个程序必须有独立工作目录、独立 artifact 目录与 wall-clock timeout。
    - 执行顺序必须固定为先 `reference`，后 `candidate`。
  - Positive Tests（应通过）：
    - 同一 testcase 在两个 Linux 基线侧顺序运行时，可生成完整的 stdout、stderr、console log、trace 文件。
    - 每轮执行结束后，artifact 目录路径包含 `program_id`、`side`、`run_id` 等可追溯字段。
    - 若 `reference` 侧执行成功，`candidate` 侧才会被调度执行。
  - Negative Tests（应失败或被拒绝）：
    - 若 `reference` 侧执行失败、超时或波动，`candidate` 侧不得被当成有效对照结果继续分析。
    - 若 snapshot 未回滚或工作目录未隔离，运行器自检必须失败。
    - 若 timeout、退出码、trace 路径等关键字段缺失，`RunResult` 不得标记为 `ok`。

- AC-7：Trace Collector 能从原始产物生成稳定的 canonical trace
  - 要求：
    - 必须保留 raw trace，不得在导入时覆盖。
    - 必须有独立 canonicalization 步骤，负责把动态字段映射为可比较表示。
    - 必须补充外部状态采样，至少包含测试目录文件列表与文件内容摘要。
  - Positive Tests（应通过）：
    - 对同一个 raw trace 连续导入两次，生成的 canonical trace 字节级一致。
    - 对含临时路径、PID、地址的 trace，canonical 化后对应字段被映射成稳定占位符。
    - 文件系统最终状态采样可输出目录项集合与文件内容哈希。
  - Negative Tests（应失败或被拒绝）：
    - 如果 canonical trace 仍包含裸地址、真实 PID/TID 或绝对时间戳比较字段，回归测试必须失败。
    - 如果 collector 找不到 raw trace 或 schema 不合法，必须明确报 `collector_error`，而不是继续比较。
    - 如果 external state 采样为空且程序涉及文件操作，该结果不得被认为可比较。

- AC-8：Analyzer v1 能在 Linux vs Linux 场景下稳定区分等价、噪声与 baseline 问题
  - 要求：
    - 必须实现 syscall-level、resource-level、final-state-level 三层比较。
    - 必须先 normalization，再比较，禁止逐字节 raw diff 直接给结论。
    - 必须支持 `BASELINE_INVALID`、`WEAK_SPEC_OR_ENV_NOISE`、`UNSUPPORTED_FEATURE`、`BUG_LIKELY` 四类分类，即使 baseline 以 Linux vs Linux 为主。
  - Positive Tests（应通过）：
    - 两个 canonical trace 完全等价时，分析结果为 `NO_DIFF` 或等价“无 divergence”状态。
    - 两边仅文件描述符编号不同、但资源语义一致时，结果仍判为等价。
    - 两边仅地址、PID、绝对时间不同而资源状态一致时，结果判为 `WEAK_SPEC_OR_ENV_NOISE` 或直接归零。
    - 同一 testcase 多次比较后，分类结论稳定一致。
  - Negative Tests（应失败或被拒绝）：
    - 不得因为裸地址、PID 或临时路径差异直接输出 `BUG_LIKELY`。
    - 如果 `reference` 侧多次执行 hash 不一致，必须归为 `BASELINE_INVALID`。
    - 在 baseline 批量 Linux vs Linux 跑数中，若出现非注入的 `BUG_LIKELY`，必须进入人工复核队列，不得直接统计为真实内核 bug。

- AC-9：baseline 稳定性评估与指标收集链路完整
  - 要求：
    - 必须设计“正常单次跑 + 异常重跑”的稳定性策略，而不是所有 case 全量多跑。
    - 必须产出 summary 指标，包括导入成功率、编译成功率、双执行成功率、trace 生成成功率、baseline-invalid 率。
    - 必须生成 baseline-invalid 程序清单与主要原因统计。
  - Positive Tests（应通过）：
    - 对稳定 testcase 进行抽样复跑时，canonical trace hash 保持一致。
    - 对出现异常的 testcase 启动复跑策略后，可稳定判定为 `BASELINE_INVALID` 或“恢复正常”。
    - 批量运行结束后能输出机器可读的 `summary.json` 与人类可读的 `summary.md`。
  - Negative Tests（应失败或被拒绝）：
    - 若 summary 缺少任一核心指标，阶段验收不得通过。
    - 若失败样例没有关联 artifact 路径，复盘脚本必须报错。
    - 若 baseline-invalid 的判定依赖人工临时观察而非规则化流程，验收不得通过。

- AC-10：Linux baseline 必须具备最小化报告闭环，即使只做最小切片
  - 要求：
    - Linux baseline 不要求完整 reducer 平台化能力，但必须有一个最小可用切片，足以对至少 1 个 divergence 样例完成删除式缩减、再验证与报告输出。
    - 为了保证 baseline 一定能验证报告链路，允许引入“仅测试使用”的受控 divergence fixture。
    - 受控 divergence fixture 默认关闭，只能在测试和验收样例中启用。
  - Positive Tests（应通过）：
    - 对一个受控 divergence 样例执行 reducer 后，能得到更短的 testcase，同时 divergence 仍保留。
    - 报告同时输出 JSON 与 Markdown，且包含 `program_id`、最小化前后长度、首次分叉 syscall index、双方证据路径、运行命令、原始/最小化 testcase 路径。
    - 报告可以被复跑脚本重新验证，确认该 divergence 仍存在。
  - Negative Tests（应失败或被拒绝）：
    - 若某次删减导致 divergence 消失，reducer 必须回退该删减，而不是继续提交错误结果。
    - 测试注入开关不得在批量真实跑数中默认开启。
    - 报告若缺少证据路径或最小化后 testcase 无法复现，验收不得通过。

- AC-11：1000 个 baseline corpus 的端到端批量跑数满足阶段门槛
  - 要求：
    - 必须对 1000 个 baseline 合格程序执行完整批量评估。
    - 必须明确写死阶段门槛；推荐先用 smoke 数据校准，再用 full run 验收。
  - 推荐阶段门槛：
    - smoke run（50 到 100 个样例）：
      - trace 生成成功率 >= 90%
      - canonical 化成功率 = 100%（针对已有 raw trace）
      - baseline-invalid 率 < 20%
    - sign-off run（1000 个样例）：
      - 程序导入成功率 >= 95%（针对输入为合法 `.syz` 的前提）
      - 编译成功率 >= 90%（针对 eligible corpus）
      - 双执行完成率 >= 85%
      - trace 生成成功率 >= 95%（针对成功执行的 case）
      - baseline-invalid 率 < 10%
      - 至少 1 份完整最小化报告成功产出并可复跑
  - Positive Tests（应通过）：
    - smoke run 达标后允许进入 1000-case full run。
    - full run 结束后，summary 自动判定阶段是否通过。
    - 失败样例能被聚合到稳定的 reason taxonomy 中。
  - Negative Tests（应失败或被拒绝）：
    - 如果 full run 未达到 1000 个合格样例，不得宣称 Linux baseline 完成。
    - 如果 baseline-invalid 率超阈值，必须回到 filter、wrapper 或 normalization 修正，而不是带着问题进入 asterinas。
    - 如果报告链路、指标链路或 artifact 回溯任一缺失，阶段验收不得通过。

## 路径边界

路径边界用于约束 Linux baseline 可接受的实现范围，避免过度设计，也避免偷工减料。

### 上界（最大可接受范围）

Linux baseline 的理想上界是：

1. 仓库内已有完整的目录骨架、配置文件、schema 校验与本地运行脚本。
2. `Program Source Manager`、`Corpus Filter`、`prog2c` 包装器、Linux agent、Dual Runner、Trace Collector、Analyzer v1、最小 reducer/report 切片全部脚本化。
3. 支持固定 corpus 的 smoke run、fault-injection run、1000-case full run 三类运行模式。
4. raw trace、canonical trace、summary、最小化报告都有 schema 或至少稳定字段检查。
5. 有一套明确的 fixture 集，覆盖允许路径、拒绝路径、噪声路径、baseline-invalid 路径和受控 divergence 路径。
6. 有基础文档说明如何导入、运行、复盘和扩展到 asterinas。

### 下界（最小可接受范围）

Linux baseline 的最低可接受实现是：

1. 能固定 syzkaller revision 并稳定生成 `syz-prog2c`。
2. 能导入、去重、筛选并产出 1000 个 baseline eligible 程序。
3. 能把 eligible 程序转成带 wrapper 的可执行程序。
4. 能在两个 Linux 基线环境中顺序运行 testcase，并收集 trace、stdout、stderr、console log。
5. 能做 canonicalization、差分分析和 baseline-invalid 分类。
6. 能产出 summary、baseline-invalid 列表和至少 1 份完整最小化报告。

如果连上述下界都不满足，就不能进入 asterinas。

### 可接受选择

- 可以使用：
  - Python 作为 orchestrator、collector、analyzer、report glue 的主语言
  - C 作为 Linux guest 执行 wrapper 的实现语言
  - JSON / JSONL / Markdown 作为产物格式
  - `Makefile`、`justfile` 或 shell script 作为本地入口
  - QEMU 或 libvirt 其中一种 VM 管理方式，但一旦选定，Linux baseline 内不应混用
  - 轻量文本级 post-processor，前提是有充分测试证明能稳定识别当前 syzkaller revision 的输出模式
  - JSON schema 或自定义校验器，前提是规则固定
- 不可以使用：
  - `syz-manager` 在线 fuzzing
  - `syz-executor` 新 OS 适配
  - 非固定 revision 的 syzkaller
  - 依赖 `threaded=1`、`collide=1` 的 testcase
  - pseudo-syscalls
  - 复杂网络、io_uring、epoll/poll/select、mount/namespace/特权路径
  - 把 raw trace 直接逐字节 diff 当成最终结论
  - 只有人工可跑、没有脚本入口的关键流程

## 可行性提示与建议

> 本节是建议性的实现路线，用于帮助落地，不是强制逐字照抄的代码设计。

### 推荐的仓库骨架

基于 README 中的建议，Linux baseline 可以直接以如下结构起步：

```text
syzabi-diff/
  docs/
    architecture.md
    testplan.md
    baseline-runbook.md
  third_party/
    syzkaller/
  configs/
    baseline_allowlist.yaml
    normalization_rules.yaml
    runner_profiles.yaml
  corpus/
    raw/
    normalized/
    meta/
    rejected/
  eligible_programs/
    baseline.jsonl
  build/
    testcases/
  artifacts/
    runs/
  orchestrator/
    scheduler.py
    vm_runner.py
    artifacts.py
    stability.py
  agent/
    linux/
      runner.c
      trace.c
      trace.h
  tools/
    bootstrap_syzkaller.sh
    import_syz.py
    filter_corpus.py
    prog2c_wrap.py
    reduce_case.py
    render_report.py
  analyzer/
    normalize.py
    compare.py
    classify.py
    schemas.py
  reports/
    baseline/
  tests/
    fixtures/
      corpus/
      traces/
      reports/
```

### 建议的数据与 artifact 约定

建议尽早把以下约定写死：

1. `program_id` = `sha256(normalized_syz_content)`。
2. `eligible_programs/baseline.jsonl` 必须按 `program_id` 排序。
3. 每次运行的 artifact 路径建议形如：

```text
artifacts/runs/<program_id>/<run_id>/<side>/
  testcase.syz
  testcase.c
  testcase.instrumented.c
  testcase.bin
  stdout.txt
  stderr.txt
  console.log
  raw-trace.json
  canonical-trace.json
  external-state.json
  run-result.json
```

4. summary 建议拆成：
   - `reports/baseline/summary.json`
   - `reports/baseline/summary.md`
   - `reports/baseline/baseline-invalid.jsonl`
   - `reports/baseline/divergence-index.jsonl`

### 建议的概念流水线

一个可行的 Linux baseline 执行链路如下：

1. `import_syz.py`
   - 读取输入 corpus
   - 归一化 `.syz`
   - 计算 `program_id`
   - 产出 raw、normalized、meta、rejected
2. `filter_corpus.py`
   - 加载 baseline allowlist
   - 扫描 syscall 集合与稳定性风险
   - 输出 `eligible_programs/baseline.jsonl`
3. `prog2c_wrap.py`
   - 调用 `syz-prog2c`
   - 对生成 C 做 post-processing
   - 验证所有 syscall 位点都被 wrapper 接管
   - 编译生成 `testcase.bin`
4. `scheduler.py` / `vm_runner.py`
   - 回滚 `reference` Linux snapshot
   - 执行 testcase
   - 收集产物
   - 若 `reference` 成功，再执行 `candidate` Linux snapshot
5. `normalize.py`
   - 导入 raw trace 与 external state
   - 生成 canonical trace
6. `compare.py` + `classify.py`
   - 先做 syscall-level 比较
   - 再做 resource-level 比较
   - 最后做 final-state-level 比较
   - 输出分类与证据
7. `reduce_case.py` + `render_report.py`
   - 仅对 divergence 样例做删除式缩减和再验证
   - 生成 JSON / Markdown 报告

### 关于 post-processor 的现实建议

`syz-prog2c` 生成的 C 代码不天然适合逐 syscall 打点，因此 Linux baseline 要先做一件关键的工程确认：

1. 先用固定 syzkaller revision 生成 5 到 10 个代表性 testcase。
2. 实际观察输出 C 的 syscall 调用模式，确认是直接 `syscall(...)`、包装宏还是其他 helper 形态。
3. 只有确认模式足够稳定后，才写文本级 post-processor。
4. 如果模式不稳定或存在多种调用路径，优先升级成 AST 级处理，或在生成模板层做更稳妥的 hook。

不要在没有读取真实 `prog2c` 输出样本前就假设其代码结构。

### 关于 Dual Runner 的建议

为了与 asterinas 接口兼容，建议从一开始就保留双侧抽象：

1. `reference`：Linux baseline A
2. `candidate`：Linux baseline B（baseline 中仍是 Linux）

这样做有两个好处：

1. asterinas 只需要替换 `candidate` 的镜像与 agent，不必重写 orchestrator 和 analyzer 的接口。
2. baseline 就能验证“双执行编排”本身，而不是只验证单次运行。

### 关于稳定性判定策略的建议

不要把所有 testcase 都做 3 次、5 次重复执行。更合理的策略是：

1. 批量模式默认每侧只执行 1 次。
2. 如果出现运行失败、trace 缺失、明显 divergence 或分类不确定，再触发 triage rerun。
3. triage rerun 建议额外运行 2 到 4 次。
4. 只有在 `reference` 侧自身不稳定时，才把该 case 标成 `BASELINE_INVALID`。

这样可以兼顾批量吞吐与稳定性判定质量。

### 关于 Linux baseline 报告闭环的建议

由于 Linux baseline 正式场景是 `Linux vs Linux`，理论上不应大量出现真实 divergence，因此必须明确设计一个“只用于验证报告链路”的受控样例。推荐两种方式二选一：

1. 在测试模式下，对 `candidate` 侧 wrapper 注入一个受控偏差，例如对指定 syscall index 的观察值做可识别扰动。
2. 使用一个已知不稳定的 fixture，让 analyzer 稳定地产生 `BASELINE_INVALID` 报告，再配合最小化脚本验证报告路径。

推荐优先选方式 1，因为它更可控，也更适合验证 reducer 和 report generator 是否真的工作。

### 相关参考路径

- [README.md](/home/plucky/FuzzAsterinas/README.md)：当前项目总体目标、模块设计、阶段划分与固定工程决策来源
- [LinuxBaseline.md](/home/plucky/FuzzAsterinas/LinuxBaseline.md)：本阶段详细实施计划

## 依赖关系与实施顺序

### 总体依赖顺序

Linux baseline 的依赖必须按下面顺序推进：

1. 先固定 syzkaller revision 与 repo 骨架。
2. 再完成 corpus 导入与筛选。
3. 再完成 `prog2c` 转换、post-processing 与编译。
4. 再完成 Linux agent 与 Dual Runner。
5. 再完成 collector、normalizer、analyzer。
6. 最后补上最小 reducer/report 切片与 1000-case campaign。

如果顺序打乱，后期很容易出现“是 corpus 问题、wrapper 问题、trace 问题还是 analyzer 问题”无法快速定位的情况。

### Milestone 1：冻结工具链、目录骨架与数据契约

目标：让后续所有开发都建立在固定输入、固定工具链和固定路径约定上。

关键任务：

1. 选定 syzkaller commit，记录来源、commit hash、构建命令与本地校验方法。
2. 建立仓库目录骨架与 artifact 根路径。
3. 写死 baseline allowlist、rejection taxonomy、classification taxonomy。
4. 定义以下 schema 或等价数据契约：
   - corpus meta
   - baseline eligible entry
   - run result
   - raw trace manifest
   - canonical trace
   - divergence report
   - summary
5. 准备最小 fixture 集：
   - 允许通过的 VFS 样例
   - 应被拒绝的 pseudo-syscall 样例
   - 应被拒绝的复杂 syscall 样例
   - 至少 1 个受控 divergence 样例

输出：

1. syzkaller revision lock
2. bootstrap/build 入口
3. schema 文档或校验器
4. fixtures 初版

退出条件：

1. 新环境可以复现 syzkaller 工具构建。
2. schema 约定已稳定，不再在后续里程碑频繁变更。

### Milestone 2：实现 Program Source Manager

目标：把输入 corpus 变成稳定、可查询、可去重的标准化测试单元。

关键任务：

1. 实现 `.syz` 导入入口，支持目录批量导入。
2. 定义归一化规则：
   - 换行统一
   - 空白统一
   - 可安全去除的注释统一
   - 文件名与路径不进入内容哈希
3. 从程序中提取：
   - syscall 列表
   - pseudo-syscall 使用标记
   - 线程敏感特征标记
   - source 类型
4. 生成：
   - `corpus/raw/`
   - `corpus/normalized/`
   - `corpus/meta/`
   - `corpus/rejected/`
5. 补齐导入结果统计与失败原因统计。

输出：

1. `import_syz.py`
2. 初版 corpus 目录
3. 导入 summary

退出条件：

1. 同一输入 corpus 重跑两次，导入结果完全稳定。
2. 非法输入能稳定进入 rejected，并带 reason code。

### Milestone 3：实现 Corpus Filter 与 baseline eligible 列表

目标：建立 baseline 的稳定 workload 边界，保证后续运行成本集中在高价值样例上。

关键任务：

1. 把 allowlist、denylist、stability filter 收敛到单一配置源。
2. 建立拒绝原因 taxonomy，例如：
   - `parse_error`
   - `pseudo_syscall`
   - `non_allowlisted_syscall`
   - `threading_sensitive`
   - `privileged_or_mount_path`
   - `complex_network_path`
3. 生成稳定排序的 `eligible_programs/baseline.jsonl`。
4. 产出按原因聚合的筛选统计。
5. 对至少 200 个样例做 dry run 检查，验证 allowlist 是否过宽或过窄。

输出：

1. `filter_corpus.py`
2. `configs/baseline_allowlist.yaml`
3. `eligible_programs/baseline.jsonl`

退出条件：

1. 同一 corpus 两次筛选输出字节级一致。
2. eligible 集合足够大，能支撑后续 1000-case campaign；若不足，需扩充输入 corpus，而不是放宽 baseline 边界。

### Milestone 4：实现 `syz-prog2c` 转换、wrapper 注入与构建流水线

目标：把 eligible `.syz` 程序变成可在 guest 内稳定执行并被 trace 的二进制。

关键任务：

1. 固定 `syz-prog2c` 调用参数，确保全部走顺序执行语义。
2. 生成代表性输出样本，确认真实代码模式。
3. 实现 post-processor，把 syscall 调用统一改写到 wrapper 接口。
4. 实现构建脚本，生成：
   - `testcase.c`
   - `testcase.instrumented.c`
   - `testcase.bin`
5. 实现 wrapper 覆盖校验，防止漏包裹 syscall 位点。
6. 把构建错误与运行错误明确分层。

输出：

1. `prog2c_wrap.py`
2. 构建脚本
3. 覆盖校验脚本

退出条件：

1. 代表性 VFS/Memory/IPC 样例均可成功从 `.syz` 生成可运行二进制。
2. 漏包裹与 revision 漂移都能被自动发现。

### Milestone 5：实现 Linux Execution Agent 与 Dual Runner

目标：完成 guest 内执行与 host 侧双执行编排。

关键任务：

1. 实现 `agent/linux/runner.c`、`trace.c`、`trace.h`。
2. 写死 trace event 的基础字段与 buffer digest 逻辑。
3. 定义 guest 工作目录约定与产物落盘路径。
4. 实现 host 侧 `RunRequest` / `RunResult` 数据结构。
5. 完成 snapshot rollback、timeout 控制、artifact 抓取。
6. 固定执行顺序：先 `reference`，后 `candidate`。
7. 若 `reference` 侧异常，直接进入 baseline triage，而不是继续正常比较。

输出：

1. Linux agent
2. `vm_runner.py`
3. `scheduler.py`
4. 原始执行 artifacts

退出条件：

1. 同一个 testcase 可在两侧 Linux 基线环境中稳定拉起。
2. 运行结束后，stdout/stderr/console/raw trace 均可定位到文件。

### Milestone 6：实现 Trace Collector、Normalization 与 Analyzer v1

目标：让“原始执行现象”变成“稳定、可比较、可分类的差分结果”。

关键任务：

1. 设计 raw trace 到 canonical trace 的转换器。
2. 实现动态字段屏蔽或映射：
   - 虚拟地址
   - PID/TID
   - 绝对时间戳
   - 临时工作目录随机前缀
   - 资源编号的等价映射，例如 `fd#0`、`fd#1`
3. 加入 external state 采样：
   - 目录项集合
   - 文件内容哈希
   - 可选 fd 探针
4. 实现三层比较：
   - syscall-level
   - resource-level
   - final-state-level
5. 实现分类器：
   - `BUG_LIKELY`
   - `UNSUPPORTED_FEATURE`
   - `WEAK_SPEC_OR_ENV_NOISE`
   - `BASELINE_INVALID`
6. 实现“异常复跑 -> 基线稳定性复核”的流程。

输出：

1. `normalize.py`
2. `compare.py`
3. `classify.py`
4. canonical trace 样例与分析结果样例

退出条件：

1. 对相同 raw trace 的重复导入得到完全一致的 canonical trace。
2. 对 Linux vs Linux 样例不会因地址、PID、路径前缀等字段误报 divergence。

### Milestone 7：实现最小 reducer/report 切片

目标：在不提前做完整报告平台化工作的前提下，完成 baseline 所需的最小报告闭环。

关键任务：

1. 只实现最小删除式 reducer：
   - 删除 syscall
   - 重跑验证
   - 保留仍能复现的更短程序
2. 若精力允许，再补一个最小参数简化策略：
   - 整数归零
   - 小 buffer
   - `nil` 化可选参数
3. 设计仅测试使用的受控 divergence fixture。
4. 把报告输出固定为 JSON + Markdown 双格式。
5. 报告中必须带足以下证据：
   - 首次分叉 syscall index
   - reference/candidate 关键事件
   - raw/canonical trace 路径
   - console log 路径
   - 原始 testcase 路径
   - 最小化 testcase 路径
   - 复跑命令

输出：

1. `reduce_case.py`
2. `render_report.py`
3. 至少 1 个完整报告样例

退出条件：

1. 能用脚本对受控 divergence 样例完成最小化和报告生成。
2. 报告复跑命令能重新验证 divergence。

### Milestone 8：完成 smoke run、full run 与 Linux baseline 签收

目标：用真实批量数据证明 Linux baseline 不只是“能跑”，而是“质量达标”。

关键任务：

1. 先做 50 到 100 个样例的 smoke run。
2. 根据 smoke run 调整：
   - allowlist 边界
   - post-processor 稳定性
   - normalization 规则
   - triage rerun 触发条件
3. 准备不少于 1000 个 baseline eligible 程序。
4. 执行 full run，输出完整 summary。
5. 聚合失败原因与 baseline-invalid 原因排行。
6. 明确列出 asterinas 进入条件与遗留问题清单。

输出：

1. smoke run summary
2. full run summary
3. baseline-invalid 列表
4. divergence index
5. Linux baseline sign-off 文档

退出条件：

1. AC-11 的阶段门槛全部满足。
2. 所有关键 deliverable 均可由脚本重新生成。

### 跨里程碑依赖规则

1. Milestone 4 不能早于 Milestone 3，因为没有 baseline eligible 集，构建吞吐和问题分布会失真。
2. Milestone 5 不能早于 Milestone 4，因为没有稳定二进制就无法验证执行器和 trace。
3. Milestone 6 不能早于 Milestone 5，因为 analyzer 需要真实 raw trace 与 external state。
4. Milestone 7 必须建立在 Milestone 6 已能稳定识别 divergence 的基础上，否则 reducer 无从判断“是否仍保留差异”。
5. Milestone 8 只能在前 7 个里程碑都至少达到下界后启动，否则 full run 只会放大无意义噪声。

### 建议的阶段检查点

为避免走到最后才发现方向偏了，建议设置以下检查点：

1. 检查点 A：5 个手工样例从导入到 canonical trace 全通。
2. 检查点 B：50 个样例 smoke run 达标。
3. 检查点 C：受控 divergence 报告闭环达标。
4. 检查点 D：1000-case full run 达标并可复盘。

## 实施备注

### 代码风格要求

1. 实现代码和注释中不要出现 `AC-`、`Milestone`、`Phase` 这类计划术语。
2. 这些术语只属于计划文档，不属于实际代码。
3. 代码中应使用领域语义明确的命名，例如 `baseline_invalid`、`canonical_trace`、`run_result`、`eligible_program`。

### 配置与规则的管理要求

1. allowlist、normalization 规则、分类规则必须集中管理，不要把规则散落在多个脚本里。
2. 所有规则变更都必须配套 fixture 或回归测试。
3. 对 baseline 而言，规则稳定性优先于“短期多支持几个 syscall”。

### 测试数据管理要求

1. fixture 必须覆盖“通过、拒绝、噪声、baseline-invalid、受控 divergence”五类。
2. 用于验收的 1000-case corpus 必须可追溯来源，避免混入未知格式数据。
3. 若输入 corpus 不足以筛出 1000 个 eligible 程序，应先扩充 corpus，再考虑是否需要微调 filter，而不是直接放宽 baseline 边界。

### 运行与复盘要求

1. 每个失败 case 必须至少能定位到一个 `program_id` 和一组 artifacts。
2. summary 中的每个聚合数字都必须能反查到具体样例清单。
3. 任何“看起来是 bug”的 case，在 Linux vs Linux 阶段都应先假设是：
   - filter 不够严
   - wrapper 不够稳
   - normalization 不够完整
   - 基线自身不稳定
   而不是直接当成内核 bug。

### 受控 divergence 的边界要求

1. 受控 divergence 只能用于测试、验收和报告链路验证。
2. 默认配置必须关闭该能力。
3. 批量 full run 不得混入受控 divergence 样例，除非明确单独标记为 test campaign。

### 进入 Asterinas bring-up 的前提

只有当以下条件同时满足时，才建议进入 Asterinas bring-up：

1. Linux vs Linux 的 canonical trace 与 analyzer 逻辑已经稳定。
2. baseline-invalid 率已被压到阈值以下。
3. 报告闭环已被至少一个样例验证。
4. full run 指标已稳定，不再频繁因基础设施问题失败。

如果这些前提不满足，过早接入 Asterinas 只会把“真实 ABI 差异”和“基础设施噪声”混在一起，导致排障成本成倍增加。
