# New Target Onboarding

本文档说明：在目标架构下，如何新增一个 target，而 **不需要**直接修改 `orchestrator/scheduler.py` 或 `orchestrator/vm_runner.py` 的核心逻辑。

当前文档基于仓库中已经开始形成的抽象层：

- `core/paths.py`
- `core/capabilities.py`
- `runners/base.py`
- `targets/base.py`
- `targets/registry.py`
- `targets/asterinas/*`

## 最小接入目标

新增 target 时，最小应该做到：

1. 能被 config 选中；
2. 能提供 runner/adapter 所需的 target-specific 行为；
3. 不在 scheduler/vm_runner 主干里再加 target-name `if/else`；
4. 不破坏现有 report / compare / classify 主链。

## 推荐新增步骤

### 1. 增加 target config

新增：

- `configs/targets/<target>/target.json`

内容应包括 target-specific 构建/运行参数，例如：

- repo/workspace 路径
- revision
- 镜像信息
- timeout
- target-specific artifact 目录

### 2. 增加 target runner profiles

新增：

- `configs/targets/<target>/runner_profiles.<workflow-or-mode>.json`

说明：

- 如果多个 workflow 共享 runner profile，可共用一份；
- 如果不同 workflow 需要不同 sandbox/work_root/binary/command，可拆多份。

### 3. 增加 workflow config

新增：

- `configs/workflows/<workflow>.json`

至少应定义：

- `workflow`
- `target`
- `target_config_path`
- `runner_profiles_path`
- `paths`
- `classification`
- `thresholds`
- `capabilities`

兼容要求：

- 若仍需兼容旧入口，可暂时保留 `configs/<workflow>_rules.json` shim。

### 4. 实现 target adapter

在：

- `targets/<target>/`

中实现 target-specific 逻辑。

最小建议模块：

- `adapter.py`
- `build.py`
- `runtime.py`
- `output.py`

如果 target 有额外领域逻辑，也可增加类似 Asterinas 的：

- `initramfs.py`
- `scml.py`

### 5. 在 registry 中注册 target

更新：

- `targets/registry.py`

让 loader 能根据 config 中的 `target` 选择正确 adapter。

要求：

- registry 是 target 选择点；
- 不要把 target-specific import 再塞回 scheduler/vm_runner。

### 6. 添加测试

至少补：

- config discovery tests
- runner profile loading tests
- fake runner protocol tests
- workflow report regression tests
- target-specific smoke / integration tests

## 不应再做的事情

新增 target 时，不应再：

- 在 `scheduler.py` 中写 `workflow.startswith(\"<target>\")`
- 在 `vm_runner.py` 中写死 `<target>` 路径
- 从 orchestrator 直接 import `tools/run_<target>.py`
- 把新的 target-specific env/path 直接塞进 generic branch，而不经过 adapter

## 完成定义

一个新 target 可以认为“最小接入成功”，当且仅当：

1. 它能通过 workflow config 被选中；
2. 它能通过 target adapter 提供 target-specific 行为；
3. 现有 orchestrator/reporting 主链无需为它增加新的 target-name 分支；
4. 相关 tests / regression fixtures 能通过。
