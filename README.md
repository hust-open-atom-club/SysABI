这个项目的正确实现路线不是“先把 syzkaller 完整移植到 Asterinas/DragonOS”，而是“复用 syzkaller 的 Linux syscall 程序生成与重放能力，外加你自己的双执行差分框架”。这是因为 syzkaller 的核心结构本来就是 `syz-manager` 负责 fuzzing 与 VM 管理，`syz-executor` 在目标机内执行 syscall 程序；而官方针对新系统支持的文档也明确表明，完整支持新 OS 需要改 executor、build、report、host、sys、targets 等多个层面。与此同时，官方文档又明确给出了 `syz-execprog` 与 `syz-prog2c` 作为单程序执行、最小化和复现实验的工具，这正好适合你的第一版。([GitHub][1])

此外，Asterinas 明确定位为 Linux ABI-compatible OS，并公开称已支持 210+ Linux system calls；DragonOS 官方仓库与官网也明确宣称 Linux compatibility。也就是说，你的系统应当把 Linux 作为 reference，把 Asterinas/DragonOS 作为 candidate，做 ABI 行为差分。([USENIX][2])

一、项目目标

项目名称建议定为：

SyzABI-Diff
副标题：A differential syscall-sequence testing framework for Linux ABI-compatible operating systems

目标定义如下。

输入是一批 syzkaller Linux 程序。程序来源可以是现有 corpus、离线生成的新程序、或后续在线变异生成。syzkaller 官方文档明确说明：syscall 描述保存在 `sys/$OS/*.txt` 中，程序是“带具体参数值的 syscall 序列”；这些描述会被用于生成、变异、执行、最小化、序列化和反序列化程序。([GitHub][3])

系统对每个程序执行两次：

一次在 reference Linux VM 中执行；
一次在目标 OS VM 中执行。

系统收集两侧的可观察行为，做归一化，再计算 divergence。输出包括：

最小化后的 testcase；
差异类别；
差异证据；
可复现脚本；
统计报告。

项目不以“找到 crash”为唯一目标，而以“发现 Linux ABI 语义偏差”为目标。

二、总体架构

总体架构固定为七个模块：

1. Program Source Manager
2. Corpus Filter
3. Dual Runner
4. Execution Agent
5. Trace Collector
6. Differential Analyzer
7. Reducer & Report Generator

推荐的数据流是：

syzkaller 程序输入
→ 程序筛选/分类
→ 分发到 Linux VM 与目标 OS VM
→ 两边运行
→ 采集 trace
→ 归一化
→ 比较
→ 若不同则最小化
→ 生成 issue-quality 报告

这里必须强调：第一版不要引入 `syz-manager` 的在线 coverage-guided fuzzing。第一版只需要离线程序输入与 `syz-execprog` 风格的重放能力。因为 syzkaller 官方文档表明，完整 manager/executor 模式本质上是 coverage-guided fuzzing 基础设施，而 `syz-execprog` 是本地执行单个或一组程序的工具，并且专门用于 crash 复现和最小化。你的项目第一版的本质是 differential replay，不是 online fuzzing。([GitHub][1])

三、模块设计

1. Program Source Manager

职责：统一管理 syzkaller 程序来源，输出标准化的 `.syzprog` 测试单元。

输入来源分三种。

第一种，已有 `.syz` 文本程序。
第二种，利用 syzkaller 的 Linux target 离线生成的新程序。
第三种，已有 crash log 中抽出的程序。

该模块不负责执行，只负责“拿到程序、解析元数据、赋予唯一 ID、落盘”。

工程要求如下。

目录结构：

`corpus/raw/`：原始 `.syz`
`corpus/normalized/`：归一化后的 `.syz`
`corpus/meta/`：每个程序的 JSON 元数据
`corpus/rejected/`：被筛掉的程序

元数据格式建议固定为：

```json
{
  "program_id": "sha256-of-content",
  "source": "seed|generated|crashlog",
  "target_os": "linux",
  "arch": "amd64",
  "uses_pseudo_syscalls": false,
  "uses_threading_sensitive_features": false,
  "syscall_list": ["openat", "read", "close"],
  "resource_classes": ["fd", "file"],
  "original_path": "..."
}
```

验收标准：

程序入库后必须做到内容去重、ID 稳定、元数据可查询。

2. Corpus Filter

职责：在程序真正进入双执行之前，先做静态筛选。

这是必要模块，不是可选模块。原因有两个。

第一，syzkaller 程序里可能包含 pseudo-syscalls，而官方文档明确说明 pseudo-syscalls 是 executor 中定义的 C 函数，不是普通 syscall；同时官方也明确说 pseudo-syscalls 通常不推荐，因为它们破坏声明式建模的优势并增加维护负担。第一版必须过滤掉它们。([GitHub][4])

第二，第一版必须先限制程序类别，降低非确定性。

筛选规则必须固定为三层。

第一层，语法可执行性。
不能有解析失败、非法依赖、明显缺资源的程序。

第二层，语义白名单。
只允许进入第一版的 syscall 类别：
VFS 基础：open/openat/read/write/pread/pwrite/lseek/close/fstat/newfstatat/mkdir/unlink/rename
Memory 基础：mmap/munmap/mprotect/brk
Process 基础：getpid/getppid/clone 的最小子集、wait4、exit、exit_group
IPC 基础：pipe/pipe2/eventfd/socketpair

第三层，稳定性白名单。
过滤以下内容：
网络协议栈复杂路径；
epoll/poll/select；
signal 高并发路径；
io_uring；
挂载/namespace/特权路径；
任何 pseudo-syscall；
任何需要 threaded/collide 才可运行的程序。

模块输出：

`eligible_programs/baseline.jsonl`

每一行包含：

```json
{
  "program_id": "...",
  "workflow": "baseline",
  "reason": ["allowed_syscalls_only", "no_pseudo", "single_thread_safe"]
}
```

验收标准：

同一份原始 corpus 经过筛选，输出结果稳定可复现。

3. Dual Runner

职责：调度两个 VM 执行同一个程序，并保证环境对齐。

这是整个系统的执行编排层。它不关心程序具体内容，也不关心差异逻辑，只负责“把相同程序在两边跑出来”。

Dual Runner 必须满足以下约束。

Linux VM 与目标 OS VM 必须使用相同架构。第一版只支持 x86_64/amd64。
每次执行前都必须回滚到快照。
每个程序执行时使用独立工作目录。
每次执行必须设置 wall-clock timeout。
每次执行结束必须收集退出码、stdout、stderr、console log、trace file。

推荐接口如下：

```python
class RunRequest:
    program_id: str
    program_path: str
    mode: str              # "sequential"
    timeout_sec: int
    sandbox: str           # "none" in baseline
    repeat: int            # 1 in baseline

class RunResult:
    side: str              # "linux" | "candidate"
    status: str            # "ok" | "timeout" | "crash" | "infra_error"
    exit_code: int | None
    stdout_path: str
    stderr_path: str
    console_log_path: str
    trace_json_path: str | None
    elapsed_ms: int
```

执行协议必须固定：

先执行 Linux reference；
reference 成功后再执行 candidate；
如果 reference 本身不稳定或失败，则该程序标记为 “baseline-invalid”，不进入差分分析。

验收标准：

给定一个固定程序，多次运行 Linux reference，结果必须满足稳定性阈值，否则标记不可用。

4. Execution Agent

职责：运行在 guest 内部，负责真正执行 syzkaller 程序并生成 syscall 级行为日志。

这是你系统最关键的 guest-side 组件。

这里有两条实现路线，但你必须选一条主路线。

主路线建议：

第一版用 `syz-prog2c` 将 `.syz` 程序转换为 C 程序，然后在 Linux 和目标 OS 上分别编译/运行一个统一包装器。官方文档明确说明：`syz-prog2c` 可以把 program 转成可执行 C source；并且当程序在 `-threaded=0 -collide=0` 下可复现时，生成的 C 程序也应能复现。([GitHub][5])

原因是：
这样最容易把“执行逻辑”与“trace 采集逻辑”合并到一个你可控的 runner 中；
不需要一开始改 `executor/executor_GOOS.h`；
不需要一开始移植 `syz-executor` 到 Asterinas/DragonOS。

备选路线：

直接复用 `syz-execprog + syz-executor`。官方文档说明 `syz-execprog` 会本地执行单个或一组程序，而 `syz-executor` 负责真实执行；不过这条路线对目标 OS 的执行环境要求更高。([GitHub][5])

第一版工程决策必须写死：

Execution Agent 使用“prog2c 生成 + 自定义 trace wrapper”路线。
`syz-execprog` 只作为开发阶段验证工具，不作为生产执行器。
完整 `syz-executor` 适配推迟到后续单独的执行器适配工作。

Execution Agent 的实现要求如下。

它由两部分组成。

第一部分，Program Build Wrapper。
输入：`testcase.c`
输出：`testcase.bin`

第二部分，Runtime Trace Wrapper。
功能：在每个 syscall 调用前后记录必要信息。

这里要说明一个现实约束：`syz-prog2c` 生成的 C 程序不是天然为“每次 syscall 前后都打点”设计的。所以你必须实现一个 AST 级或文本级 post-processor，把生成的 C 代码转成“每条 syscall 都通过统一 wrapper 调用”。

要求如下。

统一 syscall wrapper 接口：

```c
long traced_syscall(
    const char* name,
    long nr,
    int argc,
    uint64_t a0, uint64_t a1, uint64_t a2,
    uint64_t a3, uint64_t a4, uint64_t a5);
```

记录内容至少包括：

调用序号
syscall 名称
syscall number
参数原值
返回值
errno
开始时间
结束时间
是否超时
程序级异常信息

对于带输出缓冲区的 syscall，不要求第一版记录完整 buffer，但要求记录：
输出长度
前 N 字节十六进制摘要
SHA-256 哈希

验收标准：

同一个 testcase 在 Linux 上执行后，trace JSON 可完整反映每一条 syscall 的输入输出。

5. Trace Collector

职责：把 guest 内 Execution Agent 产出的原始 trace、stdout、stderr、console log 收集成统一格式。

必须输出两类文件。

第一类，raw trace。
原样保存，不做归一化。

第二类，canonical trace。
做过字段规范化。

推荐 canonical schema：

```json
{
  "program_id": "...",
  "environment": {
    "os": "linux",
    "arch": "amd64",
    "kernel_build": "...",
    "runner_version": "..."
  },
  "events": [
    {
      "index": 0,
      "syscall": "openat",
      "nr": 257,
      "args": ["AT_FDCWD", "PTR:path", "0x0", "0x0"],
      "ret": 3,
      "errno": 0,
      "elapsed_us": 42,
      "out_digest": null,
      "resource_effect": {
        "new_fd": 3
      }
    }
  ],
  "process_exit": {
    "signal": null,
    "code": 0
  }
}
```

Collector 还要负责“外部状态采样”。至少包括：

测试目录文件列表
每个文件大小
mtime 可忽略
inode 号可忽略
最终 fd 探针结果可选

验收标准：

同一执行结果在多次导入后生成相同 canonical trace。

6. Differential Analyzer

职责：比较 Linux trace 与 candidate trace，并产出 divergence。

Analyzer 必须采用三级比较模型。

第一级，syscall-level。
比较：
syscall 是否都执行到同一位置；
ret/errno 是否一致；
是否出现 Linux 成功而 candidate 失败、或反之。

第二级，resource-level。
比较：
fd 分配是否语义等价；
close 后 fd 是否不可用；
文件偏移是否一致；
读写长度是否一致；
mmap 建立后后续访问路径是否一致。

第三级，final-state-level。
比较：
程序退出码；
最终文件内容哈希；
目录项集合；
新建/删除文件集合。

Analyzer 的输出类别必须固定为四种：

`BUG_LIKELY`
`UNSUPPORTED_FEATURE`
`WEAK_SPEC_OR_ENV_NOISE`
`BASELINE_INVALID`

分类规则如下。

若 Linux 成功、candidate 返回 `ENOSYS` 或明确 unsupported，则判为 `UNSUPPORTED_FEATURE`。
若 Linux 与 candidate 都成功，但对象状态不一致，则判为 `BUG_LIKELY`。
若差异仅在 PID、地址、时间等不稳定字段，则判为 `WEAK_SPEC_OR_ENV_NOISE`。
若 Linux baseline 自身不稳定，则判为 `BASELINE_INVALID`。

最重要的一条：

不要做“严格逐字节 trace diff”。必须先做 normalization。第一版需要至少屏蔽：
地址值
PID/TID
时间戳绝对值
临时文件随机前缀

验收标准：

同一 divergence 多次分析后分类稳定。

7. Reducer & Report Generator

职责：对出现 divergence 的程序自动最小化，并输出工程可消费的报告。

最小化流程固定为四步。

第一步，syscall 删除。
逐条删除，看 divergence 是否仍存在。

第二步，参数简化。
把数据参数尽量替换为 `nil`、0、小 buffer。官方文档明确建议人工最小化时就这样做。([GitHub][5])

第三步，mmap 合并。
若程序含多个 mmap，可尝试合并区域。官方文档也明确提到这是常见最小化策略。([GitHub][5])

第四步，再验证。
Linux 与 candidate 都重新跑，确认 divergence 保留。

输出报告必须至少包含：

程序 ID
最小化前长度
最小化后长度
首次分叉 syscall index
Linux 证据
candidate 证据
分类结论
原始 `.syz`
最小化 `.syz`
可选 `.c` reproducer
运行命令
关联 console log 路径

报告格式建议同时输出 JSON 与 Markdown。

四、syzkaller 专项实现说明

这一部分专门回答“syzkaller 到底怎么用”。

你项目里，syzkaller 不是一个整体，而是四类资产。

第一类，syscall 描述资产。
也就是 `sys/linux/*.txt` 里的 syscall descriptions。官方文档明确写明，这些描述文件用于生成程序；程序是具体参数化的 syscall 序列。([GitHub][3])

你的用法是：

直接复用 Linux 的描述文件作为 generator 的输入。
不要为 Asterinas/DragonOS 新建 `sys/asterinas/*.txt` 或 `sys/dragonos/*.txt`。
因为你的目标不是“描述目标 OS 的 syscalls”，而是“用 Linux workload 测目标 OS 的 ABI 兼容性”。

第二类，描述编译资产。
官方文档说明 syscall 描述编译分两步：先用 `syz-extract` 从内核头文件/源码提取符号常量生成 `.const`；再用 `syz-sysgen` 把描述与常量编译成 Go 代码和 executor 元数据。([GitHub][3])

你的用法是：

第一版不改 Linux 描述时，不需要自己跑 `syz-extract` / `syz-sysgen`。
只有在你要新增、删减、修改 Linux syscall descriptions 时，才需要跑这两步。
工程约束上，建议你维护一个自己的 syzkaller fork，只改一份“baseline allowlist”描述集，而不是直接改 upstream 主描述。

第三类，程序执行/复现资产。
官方文档明确写了两个工具：`syz-execprog` 和 `syz-prog2c`。`syz-execprog` 负责本地执行单个或一组程序；`syz-prog2c` 负责把 program 转为 C source。官方还明确说明，`-threaded=0` 时所有 syscalls 都在同一线程执行，而 `-threaded=1` 会让每个 syscall 在单独线程中执行。([GitHub][5])

你的用法是：

第一版主用 `syz-prog2c`。
`syz-execprog` 仅用于两种用途：
一，开发时快速验证某个 `.syz` 是否可顺序执行；
二，人工调试最小化结果。

执行原则写死为：

全部 testcase 必须满足 `-threaded=0` 等价语义；
全部 testcase 禁止依赖 collide；
不能满足者直接从 baseline 排除。

第四类，完整 executor/manager 基础设施。
官方文档明确说明完整新 OS 支持需要在 `executor/executor_GOOS.h` 中实现 `os_init` 和 `execute_syscall`，并且还需要改 `pkg/build` 等其他组件。([GitHub][6])

你的用法是：

baseline 不接。
asterinas 也不接。
只有后续单独的执行器适配工作才考虑做。

原因非常明确：

这部分是“把 syzkaller 作为完整 fuzzing 平台移植”；
而你的核心任务是“把 syzkaller 作为 Linux syscall 程序生产器和复现器使用”。

五、代码仓库建议

建议主仓库目录如下：

```text
syzabi-diff/
  docs/
    architecture.md
    testplan.md
    syzkaller-integration.md
  third_party/
    syzkaller/                 # fork or pinned revision
  corpus/
    raw/
    normalized/
    meta/
    rejected/
  orchestrator/
    scheduler.py
    vm_runner.py
    artifacts.py
  agent/
    linux/
      runner.c
      trace.h
      trace.c
    candidate/
      runner.c
      trace.h
      trace.c
  tools/
    import_syz.py
    filter_corpus.py
    prog2c_wrap.py
    reduce_case.py
    render_report.py
  analyzer/
    normalize.py
    compare.py
    classify.py
  reports/
  ci/
```

仓库内必须固定一个 syzkaller revision，不允许 master 漂移。否则 corpus 行为可能不稳定。

六、实施路线

基础准备：1 周。
目标是拿到固定版本 syzkaller，跑通 `syz-prog2c` 和 `syz-execprog`，并在 Linux VM 中成功执行最简单 testcase。官方文档已经给出 `syz-execprog` 与 `syz-prog2c` 的用途，这一步没有技术不确定性。([GitHub][5])

交付物：
固定 syzkaller revision
基础构建脚本
Hello testcase 跑通证明

Linux baseline：2 周。
目标是 Linux vs Linux。只做顺序执行。先确认 trace 模型与比较逻辑可用。

交付物：
Program Source Manager
Corpus Filter
prog2c 包装器
Linux agent
Trace Collector
Analyzer v1

验收标准：
1000 个 baseline corpus 中，baseline-invalid 率低于预设阈值；
至少输出 1 份完整最小化报告。

Asterinas bring-up：2 到 3 周。
目标是把 candidate runner 跑起来，并完成 baseline 白名单 syscall 子集的双执行。

交付物：
Asterinas build/run image
Asterinas agent
Linux vs Asterinas 差分结果

验收标准：
至少 200 个 baseline testcase 跑通；
每个 testcase 均能落盘两侧 trace；
能稳定产出 divergence 报告。

DragonOS 接入：2 周。
目标同上。

Reducer 与报告系统增强：1 到 2 周。
目标是把 divergence 变成真正能发 issue 的最小 repro。

可选的完整 syzkaller 执行器适配。
只有当前面都稳定了才开始。如果真的走到这里，才需要碰 `executor/executor_GOOS.h`、`pkg/build`、`pkg/host` 等层。官方文档已经说明这是新 OS 支持的必要改动面。([GitHub][6])

七、明确的工程决策

为了防止工程师分歧，下面这些决策必须写死。

第一，不移植完整 syzkaller。
Linux baseline 与 Asterinas bring-up 都不新增 GOOS target。

第二，程序来源固定为 Linux syzkaller programs。
不维护目标 OS 自己的 syscall 描述。

第三，第一版只支持单线程顺序执行。
依据是官方文档中 `-threaded=0` 会让所有 syscalls 在同一线程执行，这最适合做稳定差分。([GitHub][5])

第四，第一版过滤掉 pseudo-syscalls。
因为它们不是普通 syscall，并且官方文档明确不鼓励普遍使用。([GitHub][4])

第五，Linux baseline 不稳定的程序直接剔除。
不要拿不稳定 workload 去测 candidate。

第六，不以 crash 为核心指标。
核心指标是 divergence 的数量、质量、可最小化率、问题分类。

八、风险点

最主要的风险不是实现，而是误报。

第一个误报源是环境随机性。
解决方法是先 Linux vs Linux，建立 baseline-invalid 列表。

第二个误报源是目标 OS feature gap。
解决方法是分类为 `UNSUPPORTED_FEATURE`，不混进 bug 统计。

第三个误报源是程序本身依赖 syzkaller executor 特殊逻辑。
解决方法是 baseline 只接受 prog2c 后可稳定运行的程序，并过滤 pseudo-syscalls。

第四个误报源是输出状态采样不足。
解决方法是 baseline 从 VFS/Memory/Process 基础类开始，每类先做最小但准确的 object-level checker。

九、验收指标

这个项目不能只用“跑起来了”验收，必须有量化标准。

建议指标如下。

基础指标：
程序导入成功率
程序编译成功率
程序双执行成功率
trace 生成成功率

质量指标：
Linux baseline-invalid 率
divergence 可复现率
divergence 最小化成功率
最终 issue-quality case 数量

项目里程碑验收：

M1：Linux baseline 跑通 1000 个程序，能产出 canonical trace。
M2：Asterinas 跑通 200 个 baseline 稳定程序。
M3：能自动把一个 divergence 缩到 10 条 syscall 以内并给出报告。
M4：至少形成一批真实可提交的问题单。
、

[1]: https://github.com/google/syzkaller/blob/master/docs/internals.md "syzkaller/docs/internals.md at master · google/syzkaller · GitHub"
[2]: https://github.com/asterinas/asterinas
[3]: https://github.com/google/syzkaller/blob/master/docs/syscall_descriptions.md "syzkaller/docs/syscall_descriptions.md at master · google/syzkaller · GitHub"
[4]: https://github.com/google/syzkaller/blob/master/docs/pseudo_syscalls.md "syzkaller/docs/pseudo_syscalls.md at master · google/syzkaller · GitHub"
[5]: https://github.com/google/syzkaller/blob/master/docs/reproducing_crashes.md "syzkaller/docs/reproducing_crashes.md at master · google/syzkaller · GitHub"
[6]: https://github.com/google/syzkaller/blob/master/docs/adding_new_os_support.md "syzkaller/docs/adding_new_os_support.md at master · google/syzkaller · GitHub"
