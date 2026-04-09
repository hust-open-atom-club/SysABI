# SyzABI Migration Plan

本文档记录从历史命名/布局 `FuzzAsterinas` 向目标形态 `SyzABI` 迁移的工程策略。

目标：

1. 对外把项目定义为 **SyzABI**；
2. 对内逐步把 target-specific 逻辑迁入 `targets/`；
3. 在迁移期间保持 `baseline` / `asterinas` / `asterinas_scml` 可运行；
4. 允许阶段性回滚，不把整个重构压成一个不可审阅的大改动。

## 迁移原则

- **兼容优先**：旧命令、旧配置文件、旧路径先保留为 shim，再逐步收敛。
- **结构先于命名**：先把依赖方向和边界切开，再清理对外命名。
- **可验证**：每个阶段都必须有测试或 golden regression 兜底。
- **可回滚**：每个阶段都应能独立回退。

## 推荐 PR / 阶段拆分

### PR-1 / Phase 0

内容：

- 冻结当前 contracts
- 补 architecture docs
- 补 golden regression baseline

验收：

- 当前输出字段与 golden fixture 可比对
- 不改变核心运行行为

### PR-2 / Phase 1

内容：

- 引入 `core/paths.py`
- 引入 `core/capabilities.py`
- 引入 runner / target base protocols
- 把 workflow-name coupling 改成 capability-driven

验收：

- 现有 workflow 继续可跑
- scheduler/vm_runner 的耦合减少

### PR-3 / Phase 2

内容：

- 把 Asterinas helper 逐步迁入 `targets/asterinas/`
- `orchestrator/capability.py` 降级为 compatibility shim
- `tools/run_asterinas.py` 缩减为兼容入口

验收：

- Asterinas target 逻辑主要由 `targets/asterinas/` 持有
- 旧 CLI 仍可用

### PR-4 / Phase 3

内容：

- canonical `configs/workflows/...`
- canonical `configs/targets/...`
- `target_config` / workflow split
- old `configs/*_rules.json` 继续保留为兼容文件

验收：

- builtin workflow 默认经 canonical layout 加载
- 旧 config path 仍兼容

### PR-5 / Phase 4

内容：

- 扩展 target onboarding 文档
- 证明新增 target 不需要直接改 scheduler/vm_runner 核心
- 清理剩余 target-name coupling

验收：

- onboarding 文档能指导新增 target 最小接入
- 报表/分类回归稳定

### PR-6 / Phase 5

内容：

- README / docs / 文案统一到 SyzABI
- 保留历史名称作为兼容 alias
- 明确 deprecation timeline

验收：

- 对外主名称统一为 SyzABI
- 兼容 alias 与删除计划明确

## Compatibility / Deprecation Window

下列内容在迁移期保留，但最终应删除或降级：

- `configs/*_rules.json`
- `configs/runner_profiles.asterinas*.json`
- `tools/run_asterinas.py` 中的非 shim 逻辑
- `run-asterinas-*` 风格命令名与 README 中的旧名称解释

建议节奏：

- **当前版本线**：保留并标注兼容
- **下一个稳定迁移点**：开始输出 deprecation 提示
- **再下一个迁移点**：删除无必要 shim

## Rollback Strategy

如果某一阶段失败：

1. 回滚该阶段 commit / PR；
2. 保持旧 config / 旧入口 / 旧路径继续工作；
3. 以 golden regression 与 smoke tests 定位差异；
4. 在下一轮只重试失败切片，不扩大范围。
