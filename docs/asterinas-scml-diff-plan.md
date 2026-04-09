# Asterinas SCML 驱动差分生成实施计划

## 目标描述

本计划解决的不是“如何继续盲目扩大 Linux corpus 再拿去跑 Asterinas”，而是把 Asterinas 已经正式文档化的 **SCML 能力边界** 变成一个可执行的 syscall 生成与筛选约束系统。

目标写死为以下三件事：

1. 从 Asterinas 的 SCML 文档中提取“已支持的 syscall 模式”，形成机器可消费的能力清单，而不是继续手写零散 allowlist。
2. 只生成、保留、运行 **符合 SCML 支持边界** 的 fuzz syscall 序列；不符合 SCML 的序列在进入差分执行前就被拒绝。
3. 在 `Linux reference vs Asterinas candidate` 条件下，对这些 **SCML 允许的序列** 做差分测试，把“功能缺失噪声”尽量前移到生成/预检阶段，把运行期失败尽量收敛为真实行为差异或内核问题。

这份计划是 `AsterinasBringup.md` 的后续工作，不是替代品。它的作用是解决当前 bring-up 中尚未收敛的问题：Asterinas 基础设施已跑通，但 unconstrained workload 仍然导致大量 candidate timeout，使 sign-off 无法通过。

## 当前状态与问题诊断

截至 2026-03-22，主仓库已经完成以下基础能力：

1. 已有固定 Asterinas revision、基于 README Docker 方式的构建/运行入口。
2. 已有 `reference` / `candidate` 双侧抽象，Asterinas 以 command runner 形式接入。
3. 已有 `.syz -> syz-prog2c -> instrumented C -> binary -> trace -> canonical trace -> analyzer -> reducer/report` 主链。
4. 已能对最小 testcase 跑通一次真实的 `Linux vs Asterinas` 双执行。
5. 已能生成最小化报告。

但 Asterinas bring-up 仍未完成签收，原因已经非常明确：

1. smoke run 已经达标，但 full run 对 unconstrained corpus 不达标。
2. 当前 200-case sign-off 的问题不是“构建链路不存在”，而是 candidate 侧大量 timeout 或语义偏差。
3. 当前 `eligible_programs/asterinas.jsonl` 只是“基于人工 allowlist + 历史经验”的 name-level 子集，不是“基于 SCML 支持模式”的 capability-level 子集。
4. 这意味着当前 workload 仍然可能包含：
   - syscall 名称本身在 Asterinas 上存在，但 flag 组合不支持；
   - syscall 名称存在，但 struct 字段模式不支持；
   - syscall 名称存在，但路径、socket domain、mount 参数等运行时模式不支持；
   - 多个 individually-supported syscall 组合成高噪声或高耦合序列。

因此，下一步不应继续扩大“没有 capability oracle 的随机样例”，而应引入 **SCML 驱动的生成与预检层**。

## 现有资料与约束来源

本计划以以下资料为输入：

1. [AsterinasBringup.md](/home/plucky/FuzzAsterinas/docs/AsterinasBringup.md)
2. [sctrace README](/home/plucky/FuzzAsterinas/third_party/asterinas/tools/sctrace/README.md)
3. [SCML 总览](/home/plucky/FuzzAsterinas/third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage/README.md)
4. [SCML 语法文档](/home/plucky/FuzzAsterinas/third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage/system-call-matching-language.md)
5. SCML 文件全集：
   [syscall-flag-coverage](/home/plucky/FuzzAsterinas/third_party/asterinas/book/src/kernel/linux-compatibility/syscall-flag-coverage)

当前仓库中可直接利用的工程基础：

1. [generate_corpus.py](/home/plucky/FuzzAsterinas/tools/generate_corpus.py)
2. [filter_corpus.py](/home/plucky/FuzzAsterinas/tools/filter_corpus.py)
3. [prog2c_wrap.py](/home/plucky/FuzzAsterinas/tools/prog2c_wrap.py)
4. [scheduler.py](/home/plucky/FuzzAsterinas/orchestrator/scheduler.py)
5. [run_asterinas.py](/home/plucky/FuzzAsterinas/tools/run_asterinas.py)
6. [render_summary.py](/home/plucky/FuzzAsterinas/tools/render_summary.py)
7. [reduce_case.py](/home/plucky/FuzzAsterinas/tools/reduce_case.py)

固定约束：

1. 当前阶段不接入 DragonOS。
2. 仍然不移植完整 syzkaller executor。
3. 仍然复用 Linux syscall descriptions，不新增 `GOOS=asterinas` target。
4. 仍然保留未来接入其他 Linux ABI-compatible OS 的接口可能性，但现在只实现 Asterinas backend。

## SCML 覆盖快照

以当前仓库内的 Asterinas SCML 文档快照为准，SCML 文件中共出现 **237 个唯一 syscall 名称**，分布如下：

1. file-and-directory-operations：71
2. file-descriptor-and-io-control：31
3. file-systems-and-mount-control：13
4. inter-process-communication：5
5. memory-management：7
6. namespaces-cgroups-and-security：7
7. networking-and-sockets：17
8. process-and-thread-management：49
9. signals-and-timers：23
10. system-information-and-misc：14

这个数字是运行时应重新计算的“输入事实”，不是硬编码契约。计划中任何实现都不得把 `237` 写死为固定常量，而必须从 SCML 源文件动态提取。

## 下一阶段的唯一目标

下一阶段的唯一目标是：

**把 SCML 从“文档”提升为“生成器与预检器的正式输入”，使最终进入差分执行的 testcase 都满足 Asterinas 已文档化的支持模式。**

换句话说，下一阶段不再以“从 Linux stable corpus 再次手工缩一批样例”为核心，而是以：

1. SCML 能力提取；
2. SCML 约束下的序列生成；
3. SCML 约束下的 Linux 预检；
4. SCML 允许样例的 Linux vs Asterinas 差分执行

作为主线。

## 输出物

本计划完成时，主仓库应新增或更新以下产物：

1. `docs/asterinas-scml-diff-plan.md`
2. `compat_specs/asterinas/scml/`
   这里可以是从 Asterinas book 同步过来的 SCML 输入副本，或其索引文件
3. `compat_specs/asterinas/scml-manifest.json`
4. `compat_specs/asterinas/generation-profile.json`
5. `tools/build_scml_manifest.py`
6. `tools/derive_scml_allowed_sequences.py`
7. `tools/preflight_scml_gate.py`
8. `configs/asterinas_scml_rules.json`
9. `eligible_programs/asterinas_scml.jsonl`
10. `reports/asterinas_scml/summary.json`
11. `reports/asterinas_scml/signoff.md`
12. `reports/asterinas_scml/divergence-index.jsonl`
13. `reports/asterinas_scml/scml-rejections.jsonl`
14. 至少 1 份 `Linux vs Asterinas` 的最小化报告

如果实现时选择复用现有 `asterinas` workflow，而不是新增 `asterinas_scml` workflow，也允许。但届时必须保证：

1. 历史 bring-up 结果不会被无提示覆盖；
2. 产物目录或文件名能区分“普通 bring-up”与“SCML 驱动差分”；
3. 使用者能清楚知道当前 summary 对应哪一种 workload 生成策略。

## 非目标

以下内容明确不属于本计划：

1. 不把 DragonOS 纳入本阶段实现。
2. 不要求对 Asterinas 的全部 237 个 syscall 立刻做高质量序列生成。
3. 不要求把 SCML 变成一个完整通用编程语言解释器。
4. 不要求为了生成器去修改 Asterinas SCML 文档语法。
5. 不要求完整 coverage-guided fuzzing。
6. 不要求新的 OS target。
7. 不要求在本阶段解决所有 Asterinas 真实 bug。

## 固定工程决策

以下决策在本阶段写死：

1. **SCML 是支持边界的唯一权威输入**。如果 SCML 未声明支持，则默认不生成对应模式的 testcase。
2. syscall 名称级 allowlist 仍可存在，但只能由 SCML manifest 导出，不允许再手工维护第二套独立真相源。
3. 生成阶段必须同时满足两个约束：
   - syscall 名称必须在 SCML manifest 中出现；
   - runtime 实际调用模式必须通过 SCML 预检。
4. 预检必须在 Linux 上完成，作为 candidate 执行前的硬门槛。
5. `sctrace + SCML` 是优先使用的运行时匹配 oracle；若存在局限，可补充自定义 gate，但不能绕过 SCML。
6. 未来其他 OS 的扩展点应当是“能力源 backend”，而不是把 Asterinas 特判散落到 scheduler / analyzer 主链中。
7. 当前阶段默认只对 **环境稳定、非特权、可复盘** 的 supported 模式做批量差分。
8. 即使某 syscall 在 SCML 中出现，也允许在 generation profile 中标记为 `deferred_due_to_env_noise` 或 `deferred_due_to_privilege`，暂不生成。

## 能力模型设计

### 1. 通用能力接口

为了未来接入其他 OS，但又不在当前阶段实现它们，建议定义一个通用能力接口：

```python
class CapabilitySource(Protocol):
    def load_manifest(self) -> dict[str, object]:
        ...

class SequenceGate(Protocol):
    def validate_runtime_trace(self, trace_path: str) -> dict[str, object]:
        ...
```

对当前阶段：

1. `CapabilitySource` 只实现 `AsterinasSCMLSource`
2. `SequenceGate` 只实现 `AsterinasSCMLGate`

未来若接入别的 OS，只需要新增：

1. `ManualSpecSource`
2. `SCMLSource`
3. `HybridSource`

而不是改 orchestrator 主链。

### 2. SCML Manifest Schema

建议将 SCML 提取结果归一化为如下 schema：

```json
{
  "target": "asterinas",
  "source_type": "scml",
  "source_revision": "f05e89b615c5dcb3f7c74accf24bdc23f96fcfc3",
  "generated_at": "2026-03-22T00:00:00Z",
  "categories": {
    "file-and-directory-operations": {
      "syscalls": {
        "openat": {
          "support_tier": "partial",
          "rule_files": ["open_and_openat.scml"],
          "requires_runtime_match": true,
          "ignored_flags": ["O_NOCTTY", "O_DSYNC"],
          "unsupported_flags": ["O_TMPFILE"],
          "notes": ["O_PATH is partially supported"]
        }
      }
    }
  }
}
```

Manifest 中必须至少包含：

1. category
2. syscall_name
3. source_scml_files
4. support_tier：`full | partial | deferred`
5. ignored_flags
6. unsupported_flags
7. notes
8. preflight_required
9. generation_enabled
10. defer_reason（如果不生成）

### 3. Generation Profile Schema

因为“SCML 中出现”不等于“适合当前差分 workload”，所以需要再有一层执行视角的 profile：

```json
{
  "target": "asterinas",
  "profile_name": "stable-diff",
  "included_categories": [
    "file-and-directory-operations",
    "file-descriptor-and-io-control",
    "process-and-thread-management",
    "system-information-and-misc"
  ],
  "deferred_categories": {
    "file-systems-and-mount-control": "privileged_or_mount_heavy",
    "namespaces-cgroups-and-security": "privileged_or_namespace_heavy"
  },
  "sequence_length": {
    "min": 1,
    "max": 12
  }
}
```

这层 profile 的意义是：

1. SCML 决定“语义上支持哪些模式”
2. profile 决定“当前差分 campaign 愿意生成哪些模式”

## 生成策略

### 1. 两级约束，而不是单级约束

不能只做 name-level allowlist，因为 SCML 最大的价值恰恰在 flag、struct、path 和参数模式。

因此生成策略必须分成两级：

1. **静态约束**：
   - 只允许使用 manifest 中 `generation_enabled=true` 的 syscall
   - category/profile 不允许的 syscall 一律不进入 generator
2. **运行时预检约束**：
   - 序列在 Linux 上执行一次
   - 采集 `strace -yy -f`
   - 交给 `sctrace $ASTER_SCML --input`
   - 只保留完全通过 SCML 匹配的序列

这样即便生成器偶尔在参数组合上走出支持边界，也会在预检阶段被淘汰，不会进入最终 corpus。

### 2. 生成来源

本阶段建议支持三种序列来源：

1. **从现有 Linux corpus 反向筛选**
   - 这是最现实的第一步
   - 对现有 `.syz` 先做 name-level 筛选，再做 SCML runtime preflight
2. **从 syzkaller generator 正向生成**
   - 使用 SCML manifest 导出的 syscall 名称集合作为 `-allow`
   - 然后对生成结果做 SCML preflight
3. **从模板种子扩展**
   - 对 `open/openat/renameat2/lseek/socket/socketpair/clone/wait4` 等关键 syscall 维护少量人工模板
   - 作为探索特定能力边界的高价值种子

### 3. 为什么必须有 Linux 预检

SCML 约束的是 **运行时 syscall 调用模式**，而不是 `.syz` 文本表面上出现的 syscall 名称。

例如：

1. `openat` 是否使用 `O_TMPFILE`
2. `renameat2` 是否使用 `RENAME_EXCHANGE`
3. `sendto` 是否使用不支持的 `MSG_*`
4. `mmap` / `mprotect` / `clone3` 是否使用支持外的 flag 组合

这些信息只有在运行时展开后才能可靠判断。

因此，“不支持的 syscall 序列就不生成” 在工程上应解释为：

**任何不通过 SCML 运行时预检的序列，都不得进入最终的差分执行 corpus。**

## 建议的 workflow 切分

建议新增一个独立 workflow，例如：

1. `asterinas_scml`

对应配置与产物：

1. `configs/asterinas_scml_rules.json`
2. `eligible_programs/asterinas_scml.jsonl`
3. `reports/asterinas_scml/`
4. `build/asterinas_scml/testcases/`
5. `artifacts/runs/asterinas_scml/`
6. `artifacts/sandboxes/asterinas_scml/`

这样可以避免：

1. 覆盖原有 `asterinas` bring-up 结果
2. 混淆 “普通 bring-up workload” 和 “SCML 驱动 workload”
3. `asterinas` 与 `asterinas_scml` 在相同 `program_id/run_id` 下共享 sandbox root，进而互相清空对方的运行目录

实现约束补充：

1. `asterinas_scml` 的 `reference` / `candidate` 必须使用独立于 `asterinas` workflow 的 sandbox 根目录。
2. 允许为一批 testcase 预先打包共享的 initramfs package 以减少重复打包开销。
3. 但 candidate 执行隔离不能放宽：**每个 testcase 仍必须单独启动一个 VM**，不得在同一个 guest 实例中连续执行多个 testcase。
4. 因此，`candidate_batch_size` 在实现上只能表示“共享 packaged initramfs 的分组规模”，不能表示“共享 guest 的批执行规模”。

## Acceptance Criteria

以下验收标准遵循 TDD 组织。

- AC-1：SCML 输入源必须固定、完整且可复现
  - 要求：
    - 必须固定 Asterinas revision。
    - 必须能稳定定位到 SCML 根目录。
    - 必须能枚举全部 `.scml` 文件并输出统计。
  - Positive Tests（应通过）：
    - 运行 manifest builder 时，能稳定发现 SCML 文件列表。
    - 对同一 revision 连续运行两次，文件数、syscall 计数和 category 计数一致。
    - 产物中能记录 source revision 与 SCML 根路径。
  - Negative Tests（应失败或被拒绝）：
    - 若 revision 漂移，manifest builder 必须报错或重新记录来源。
    - 若 SCML 根目录缺失或为空，不得回退为手工 allowlist。

- AC-2：必须从 SCML 中提取通用能力清单，而不是继续手工维护第二份真相源
  - 要求：
    - 生成 `scml-manifest.json`。
    - 每个 syscall 条目都要能追溯到一个或多个 `.scml` 文件。
    - manifest 必须保留 `support_tier`、`unsupported_flags`、`ignored_flags`、`notes`。
  - Positive Tests：
    - `open/openat`、`renameat2`、`lseek`、`socket`、`clone3` 等条目均能被提取。
    - README 中写明的 unsupported flags 能进入 manifest。
    - manifest 中每条规则都保留源文件引用。
  - Negative Tests：
    - 不得出现无法回溯源 SCML 的 syscall 条目。
    - 不得把 README 明确写成 unsupported 的 flag 默认为 supported。

- AC-3：必须有一层“稳定差分 profile”，把 SCML 支持与当前可运行 workload 分开
  - 要求：
    - 需要明确哪些 category 当前启用、哪些延后。
    - 延后理由必须结构化。
  - Positive Tests：
    - mount、namespace、reboot 等高特权路径可以被标记为 deferred，但必须有原因。
    - profile 可输出 enabled/deferred syscall 列表。
  - Negative Tests：
    - 不得因为某 syscall 在 SCML 中出现就默认纳入当前 campaign。
    - 不得静默丢弃高噪声 syscall 而不记录 defer reason。

- AC-4：name-level 生成器只能使用 SCML 允许的 syscall 名称集合
  - 要求：
    - 从 manifest 导出生成器 allowlist。
    - generator 只能选择 enable 的 syscall。
  - Positive Tests：
    - 生成器不再依赖手写 Asterinas allowlist。
    - 生成出的 `.syz` 程序中的 base syscall 名称都出现在 manifest 中。
  - Negative Tests：
    - `io_uring`、未出现在 manifest 中的 syscall 不得生成。
    - 被 profile defer 的 syscall 不得进入生成结果。

- AC-5：runtime SCML 预检必须是进入差分执行前的硬门槛
  - 要求：
    - 每个候选序列都要在 Linux 上预跑并生成 `strace`。
    - 必须通过 `sctrace` 或等价 SCML gate。
  - Positive Tests：
    - `openat` 使用支持的 flag 组合时，预检通过。
    - `renameat2` 使用 `RENAME_EXCHANGE` 时，预检拒绝。
    - `lseek` 使用 `SEEK_DATA` 或 `SEEK_HOLE` 时，预检拒绝。
  - Negative Tests：
    - 不通过 SCML gate 的序列不得进入 `eligible_programs/asterinas_scml.jsonl`。
    - 不得因为后续 candidate 可能支持就绕过预检。

- AC-6：必须对“SCML 允许的序列”做 Linux vs Asterinas 差分，而不是只做 SCML 匹配
  - 要求：
    - 预检通过后，序列进入双执行。
    - 复用现有 runner、collector、normalizer、analyzer 主链。
  - Positive Tests：
    - 至少一个 SCML 允许的真实序列能跑通双执行。
    - Linux 和 Asterinas 双侧 artifacts、raw trace、canonical trace 均能落盘。
  - Negative Tests：
    - 不得让 SCML gate 通过的序列跳过 candidate 执行。
    - 不得为 SCML workflow 重写第二套 analyzer 主链。

- AC-7：必须把 SCML gate 结果纳入分类与报告，而不是只看最终运行状态
  - 要求：
    - 每个 testcase 需要记录 `scml_preflight_status`。
    - 需要区分：
      - `rejected_by_scml`
      - `passed_scml_but_candidate_failed`
      - `passed_scml_and_no_diff`
      - `passed_scml_and_diverged`
  - Positive Tests：
    - 报告中可追溯看到某 case 是否经过 SCML gate。
    - `passed_scml_but_candidate_failed` 的 case 不再被混成“生成不受控”。
  - Negative Tests：
    - 不得把所有 candidate 失败都解释成 SCML 缺口。
    - 不得丢失 SCML gate 证据。

- AC-8：必须有一套明确的 rejection taxonomy
  - 要求：
    - 预检拒绝样例要落盘到 `scml-rejections.jsonl`。
    - reason 必须稳定且可统计。
  - Positive Tests：
    - 至少支持以下 reason：
      - `syscall_not_in_manifest`
      - `unsupported_flag_pattern`
      - `unsupported_struct_pattern`
      - `unsupported_path_pattern`
      - `deferred_category`
      - `scml_parser_gap`
    - 同一输入重复执行，reason 稳定一致。
  - Negative Tests：
    - 不得把所有 SCML 拒绝统一丢进 `unsupported_feature`。
    - 不得因为解析失败而静默跳过。

- AC-9：必须保留未来其他 OS 的接入扩展点
  - 要求：
    - capability source、manifest、gate 接口必须是 target-neutral 命名。
    - 当前只实现 Asterinas backend。
  - Positive Tests：
    - 目录结构或接口命名能容纳未来 `compat_specs/<target>/...`。
    - 不需要改 scheduler / analyzer 主链就能再加一个新的 capability backend。
  - Negative Tests：
    - 不得把 `asterinas` 字符串硬编码到通用 manifest/gate 抽象里。
    - 不得为了“未来可能支持其他 OS”而现在就实现第二个 target。

- AC-10：需要有明确的 smoke 和 sign-off 门槛
  - 建议门槛：
    - smoke（100 个 SCML 通过样例）：
      - preflight pass rate = 100%（对最终 eligible 集）
      - build success rate >= 98%
      - dual execution completion rate >= 90%
      - trace generation success rate >= 98%
      - canonicalization success rate = 100%
    - sign-off（500 个 SCML 通过样例）：
      - build success rate >= 98%
      - dual execution completion rate >= 92%
      - trace generation success rate >= 98%
      - canonicalization success rate = 100%
      - baseline-invalid rate < 5%
      - 至少 1 份 `Linux vs Asterinas` 最小化报告成功产出
  - Positive Tests：
    - smoke 达标后再进入 sign-off。
    - summary 中既有运行指标，也有 SCML gate 指标。
  - Negative Tests：
    - 未达到 500 个 SCML 通过样例，不得宣称本阶段完成。
    - 如果 summary 中缺失 SCML gate 指标，不得签收。

- AC-11：必须有一份“SCML 驱动差分”的可复跑报告
  - 要求：
    - 该报告对应一个通过 SCML gate 的 testcase。
    - 报告中要包含 Linux/Asterinas 双侧证据路径。
  - Positive Tests：
    - 报告中能看到 `scml_preflight_status=passed`。
    - `first_divergence_event_index` 和 `first_divergence_syscall_index` 都合法。
  - Negative Tests：
    - 不得使用“未通过 SCML gate 的样例”充当成功报告。
    - 若最小化后 divergence 消失，reducer 必须回退。

## 路径边界

### 上界

本计划的理想上界是：

1. 具备通用 capability source 抽象；
2. Asterinas SCML backend 完整可用；
3. 支持从现有 corpus 和正向 generator 两种来源得到 SCML 通过样例；
4. 具备 SCML preflight + Linux/Asterinas 差分 + reducer/report 完整闭环；
5. 能稳定完成 500-case sign-off。

### 下界

本计划的最低可接受实现是：

1. 能从 SCML 生成 manifest；
2. 能用 manifest 导出 syscall allowlist；
3. 能对候选样例做 Linux 上的 SCML runtime preflight；
4. 至少能对一个 SCML 通过样例跑通 Linux/Asterinas 双执行并产出报告。

如果连这四点都不满足，就不能宣称“SCML 已进入实际生成和差分链路”。

### Allowed Choices

可以接受的选择：

1. SCML parser 可以是轻量解析器，不必一次覆盖全部高级语法优化。
2. SCML gate 可以优先复用 `sctrace`，必要时增加补充检查器。
3. 初期可以只启用稳定 category。
4. 初期可以只对“预检通过的最终 eligible corpus”做差分。

不允许的选择：

1. 不允许继续手工维护独立的第二份 Asterinas allowlist 真相源。
2. 不允许跳过 runtime SCML preflight。
3. 不允许把未在 SCML 中声明支持的模式直接纳入生成结果。
4. 不允许把 DragonOS 一起带进这一轮实现。

## 依赖与实施顺序

### 里程碑 1：把 SCML 文档变成能力清单

1. 枚举全部 `.scml` 文件。
2. 解析 syscall 名称、bitflag rule、struct rule、特殊 built-in rule。
3. 结合各 category README 补充 unsupported / ignored / partial notes。
4. 输出 `scml-manifest.json`。

### 里程碑 2：建立稳定差分 profile

1. 从 manifest 中挑出当前可运行 category。
2. 对高噪声 syscall 和高特权 syscall 给出 defer reason。
3. 输出 `generation-profile.json`。

### 里程碑 3：建立 SCML 约束下的生成与导入

1. 从 manifest/profile 导出 syscall allowlist。
2. 对现有 corpus 做第一轮 SCML 派生。
3. 对正向生成器加上 manifest-derived allowlist。

### 里程碑 4：建立 SCML runtime preflight gate

1. Linux 上运行候选 binary。
2. 生成 `strace -yy -f` 日志。
3. 使用 `sctrace` 对照 SCML。
4. 只保留通过样例。

### 里程碑 5：差分执行与报告整合

1. 把 preflight 通过样例写入 `eligible_programs/asterinas_scml.jsonl`。
2. 复用现有 scheduler/analyzer/reducer。
3. 在报告中补入 SCML gate 信息。

### 里程碑 6：smoke 与 sign-off

1. 先跑 100 个 SCML 通过样例。
2. 达标后跑 500 个 sign-off 样例。
3. 输出 summary、rejections、divergence index、最小化报告。

## 实施建议

### 1. 先做“从现有 corpus 反向筛选”，再做“正向生成”

原因：

1. 当前仓库已经有 `.syz` corpus、build、runner 和差分链。
2. 反向筛选更容易快速验证 SCML gate 是否有价值。
3. 一旦反向筛选能显著降低 Asterinas timeout，再把 SCML manifest 喂给 generator 才更稳。

### 2. 优先吃透 fully_covered + 低噪声 category

建议默认先启用：

1. file-and-directory-operations
2. file-descriptor-and-io-control
3. process-and-thread-management 中低特权子集
4. system-information-and-misc

谨慎启用：

1. networking-and-sockets
2. memory-management
3. signals-and-timers

默认延后：

1. file-systems-and-mount-control
2. namespaces-cgroups-and-security
3. reboot / pivot_root / mount / unshare / setns 等环境破坏性路径

### 3. SCML 通过样例的 candidate 失败，优先看作高价值信号

一旦一个样例：

1. syscall 名称在 SCML 中存在；
2. 运行时模式通过 SCML gate；
3. Linux reference 稳定；
4. Asterinas candidate 仍然失败或语义偏差；

那么它比当前 unconstrained corpus 中的随机 timeout 更接近真正值得排查的 Asterinas 问题。

## Implementation Notes

### 命名要求

1. 代码和配置中不得重新引入 `phase1/phase2` 之类命名。
2. 优先使用 `workflow`、`manifest`、`profile`、`capability`、`preflight`、`gate`、`eligible` 等域内术语。

### 文档要求

1. README 中若新增本阶段说明，必须明确写出：
   - SCML 是输入真相源
   - runtime preflight 是硬门槛
   - 当前只实现 Asterinas backend

### 结果要求

1. 本计划完成时，不能只拿“能跑”作为验收。
2. 必须用 SCML 驱动后的指标证明：
   - timeout/unsupported 噪声下降；
   - candidate failure 更集中于真实差异；
   - 差分报告更具 issue 质量。
