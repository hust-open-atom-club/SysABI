# Current Contracts

## Round 0 冻结的耦合点

- 工作流名称、`make run` 与 `make *-workflow` 入口仍然是外部兼容面。
- canonical artifact root 仍然保持 `build/targets/<target>/<workflow>/...`、`artifacts/runs/targets/<target>/<workflow>/...`、`reports/targets/<target>/<workflow>/...`。
- legacy `_rules.json` 与旧 runner profile 路径目前仍保留兼容层，但 canonical `configs/workflows/*.json` 是主路径。
- trace/result artifact 仍然要求写出 `stdout.txt`、`stderr.txt`、`console.log`、`raw-trace.json`、`external-state.json`、`run-result.json`。

## Phase 0 冻结的已知耦合

- Asterinas 仍然拥有最完整的 target-owned runtime，其他 target 先通过同一 adapter/entrypoint 契约逐步接入。
- batching 目前通过 capability + command runner mode 驱动，其中 `shared_guest_shell` 为 TGOSKits StarryOS 提供 target-owned batch 入口。
- canonical path 生成统一收口到 `core/paths.py`，避免各模块再次拼接目标产物路径。
- 新的 target/runner lookup 已改为显式 registry，不再把未知 target 或未知 runner kind 静默回退到默认实现。
- 所有外部 TGOSKits 目标都通过 `targets/entrypoint.py` 统一调度，并且要求 `SYZABI_ENABLE_TGOSKITS=1` 才允许实际执行。
