# SysABI 架构改进计划

## 目标

修复当前 SysABI 项目中最严重的架构缺陷——**模块分层混乱、依赖方向颠倒**，在保持所有现有工作流和产物契约兼容的前提下，将项目从"看似分层实则纠缠"的状态演进为真正清晰、可测试、可扩展的分层架构。

## 核心原则

1. **依赖永远向下**：`core/` → `utils/` → `targets/` / `runners/` → `orchestrator/` → `tools/`
2. **接口必须收紧**：Protocol 不是装饰，签名必须精确，实现必须一致
3. **消灭上帝模块**：`orchestrator/common.py` 必须拆分，职责必须分散
4. **先保兼容再演进**：所有现有 smoke workflow 名称、产物路径、报告契约保持原样

---

## 问题诊断

### P0：依赖方向颠倒（最严重）

`targets/` 作为底层抽象模块，大量反向导入上层 `orchestrator/`：

| 违规文件 | 导入的上层模块 |
|---------|--------------|
| `targets/entrypoint.py` | `orchestrator.common` |
| `targets/asterinas/adapter.py` | `orchestrator.common` |
| `targets/asterinas/build.py` | `orchestrator.common` |
| `targets/asterinas/api.py` | `orchestrator.common`, `orchestrator.vm_runner` |
| `targets/asterinas/common.py` | `orchestrator.common` |
| `targets/asterinas/paths.py` | `orchestrator.common` |
| `targets/asterinas/osdk.py` | `orchestrator.common` |
| `targets/asterinas/bundle.py` | `orchestrator.common` |
| `targets/asterinas/scml.py` | `orchestrator.common` |
| `targets/tgoskits_arceos/api.py` | `orchestrator.common`, `orchestrator.vm_runner` |
| `targets/tgoskits_starryos/api.py` | `orchestrator.common`, `orchestrator.vm_runner` |

同时 `orchestrator/common.py` 也反向导入 `targets.base` 和 `runners.factory`，形成循环依赖。

**后果**：`targets/` 无法独立测试和复用；任何对 `orchestrator/common.py` 的修改都可能意外破坏目标适配器。

### P0：`orchestrator/common.py` 上帝模块

288 行的文件同时承担 7+ 种职责：
1. 配置加载 (`config`, `configure_runtime`)
2. JSON I/O (`load_json`, `dump_json`, `dump_jsonl`, `load_jsonl`)
3. 文件系统工具 (`ensure_dir`, `clean_dir`, `read_text`, `write_text`, `sha256_text`)
4. 路径解析 (`repo_root`, `resolve_repo_path`, `path_resolver`, `reports_dir`, `report_path`, `temp_dir`)
5. 环境变量管理 (`env_with_temp`, `env_with_go`)
6. Runner Profile 验证 (`validate_runner_profiles_payload`, `runner_profiles`)
7. Legacy 兼容逻辑 (`_rules.json` 回退)

**后果**：项目几乎所有模块都依赖它，成为单点瓶颈和变更热点。

### P1：TargetAdapter 大量重复代码

以下方法在 `linux`、`asterinas`、`tgoskits_arceos`、`tgoskits_starryos` 的 adapter 中几乎完全一致：
- `prepare_case` — 完全相同的字典构造逻辑
- `collect_result` — 完全相同的字典构造逻辑
- `finalize_result` — 完全相同的 `dict(result) + finalized=True`
- `compose_template_inputs` — 三个返回 `{}`，只有 asterinas 不同
- `packaged_candidate_env` — 三个返回 `{}`，只有 asterinas 不同
- `prewarm_candidate_batch` — 三个是空实现，只有 asterinas 不同

### P1：接口签名松散，Protocol 形同虚设

`targets/base.py` 定义的 Protocol 使用 `*args, **kwargs`，但实现类签名各异：
- `LinuxTargetAdapter.prepare_target(self, **kwargs)` — 无限制
- `AsterinasTargetAdapter.prepare_target(self, *, cfg: dict, mode: str)` — 强制关键字
- `TGOSKitsArceOSTargetAdapter.prepare_target(self, *, cfg: dict)` — 另一个签名

`runners/base.py` 定义了 `prepare`、`healthcheck`、`run_case`、`run_batch`、`collect_outputs`，但 `CommandRunner` 中多个方法是空实现或 `raise NotImplementedError`，且在 `vm_runner.py` 中从未被调用。

### P1：`core/workflow_contract.py` 硬编码目标配置

```python
TARGET_REQUIRED_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "tgoskits_starryos": ("default_mode", "revision", "repo_dir_env", ...),
    "tgoskits_arceos": ("default_mode", "revision", "repo_dir_env", ...),
}
```
每增加一个新目标都需要修改 `core/`，违反开闭原则。

### P2：Python/Go 混合问题

- `go.mod` 引用了不存在的 `./third_party/syzkaller`，Go 代码无法编译
- 模块名 `github.com/plucky/fuzzasterinas` 与仓库名 `SysABI` 不一致
- Python 与 Go 仅通过 stdout JSON 通信，无共享 schema

### P2：技术债务

- **25 处** `sys.path.insert(0, ...)` Hack，应使用 `python -m` 或 `pyproject.toml`
- `RunnerError` 在三个不同位置重复定义
- `dataclass(slots=True)` 要求 Python 3.10+

---

## 改进方案

### 阶段一：建立基础工具层（切断反向依赖）

**目标**：提取所有被 `targets/` 和 `orchestrator/` 共同依赖的基础工具，建立独立的 `utils/` 或下沉到 `core/`，彻底切断 `targets/` → `orchestrator/` 的依赖。

**具体动作**：

1. **新建 `core/fs_utils.py`**
   - 从 `orchestrator/common.py` 迁移：`load_json`, `dump_json`, `dump_jsonl`, `load_jsonl`, `read_text`, `write_text`, `ensure_dir`, `clean_dir`, `sha256_text`
   - 这些是纯工具函数，不依赖任何业务模块

2. **新建 `core/env_utils.py`**
   - 从 `orchestrator/common.py` 迁移：`env_with_temp`, `env_with_go`
   - 纯环境变量封装，无业务逻辑

3. **新建 `core/path_utils.py`**
   - 从 `orchestrator/common.py` 迁移：`repo_root`, `resolve_repo_path`
   - 从 `core/paths.py` 迁移：`PathResolver` 中硬编码的路径模式集中到此处
   - `PathResolver` 只保留面向业务的路径组装逻辑，字符串模板从 `path_utils.py` 导入

4. **重构 `orchestrator/common.py`**
   - 保留配置加载 (`config`, `configure_runtime`) 和 runner profile 验证
   - 移除所有 JSON I/O、文件系统工具、路径解析、环境变量工具的本地实现，改为从 `core/` 导入
   - 移除 `targets.base` 和 `runners.factory` 的导入（这些应在 `orchestrator/scheduler.py` 等消费层导入）

5. **清理 `targets/` 的反向依赖**
   - `targets/asterinas/` 下所有文件：将 `from orchestrator.common import ...` 改为 `from core.fs_utils import ...`, `from core.path_utils import ...`
   - `targets/tgoskits_arceos/api.py`、`targets/tgoskits_starryos/api.py`：同上
   - `targets/entrypoint.py`：同上
   - `targets/tgoskits_arceos/api.py` 和 `targets/tgoskits_starryos/api.py` 中导入的 `orchestrator.vm_runner.extract_framed_events`：将该函数下沉到 `core/trace_utils.py` 或 `analyzer/` 中

**验收标准**：
- `grep -r "from orchestrator" targets/` 和 `grep -r "import orchestrator" targets/` 返回空结果
- `targets/` 目录可以独立运行 `python -c "import targets.asterinas.adapter"` 而不触发 `orchestrator/` 的导入
- 所有现有测试继续通过

---

### 阶段二：拆分上帝模块，建立清晰的配置层

**目标**：将 `orchestrator/common.py` 中剩余的多余职责进一步拆分。

**具体动作**：

1. **新建 `orchestrator/config_loader.py`**
   - 从 `orchestrator/common.py` 迁移：`config()`, `configure_runtime()`, `resolved_config_path()`
   - 保留 legacy 兼容逻辑，但将其隔离到 `orchestrator/config_loader.py` 的一个私有函数中

2. **新建 `orchestrator/runner_profiles.py`**
   - 从 `orchestrator/common.py` 迁移：`validate_runner_profiles_payload()`, `runner_profiles()`

3. **清理后的 `orchestrator/common.py`**
   - 仅保留真正属于"编排通用逻辑"的代码，或作为兼容性别名重新导出已迁移的符号
   - 逐步废弃，最终目标是让 `orchestrator/common.py` 消失或被替换为显式的 `orchestrator/context.py`

4. **重构 `core/workflow_contract.py` 的硬编码配置**
   - 将 `TARGET_REQUIRED_CONFIG_KEYS` 改为注册表机制
   - 每个 target 的 adapter 暴露一个 `required_config_keys()` 类方法或模块级常量
   - `validate_target_config_payload()` 通过 `targets.registry` 查询目标自声明的必填键

**验收标准**：
- `orchestrator/common.py` 行数从 ~288 行缩减到 <100 行（或完全消失）
- 新增目标时，不需要修改 `core/workflow_contract.py`
- 所有现有 workflow config 验证行为不变

---

### 阶段三：收紧接口，提取公共基类

**目标**：让 `TargetAdapter` Protocol 真正发挥静态约束作用，消除重复代码。

**具体动作**：

1. **收紧 `targets/base.py` 的 Protocol 签名**
   - 将 `def prepare_target(self, **kwargs: Any) -> object: ...` 改为显式签名，例如：
     ```python
     def prepare_target(self, *, cfg: dict[str, Any], mode: str | None = None) -> dict[str, Any]: ...
     ```
   - 同理收紧 `healthcheck`, `run_case`, `run_batch` 的签名
   - 移除 `*args, **kwargs` 泛滥

2. **新建 `targets/base_adapter.py` 中的 `BaseTargetAdapter`**
   - 实现以下公共方法：
     - `prepare_case(self, entry, cfg)` — 默认返回标准字典
     - `collect_result(self, result, cfg)` — 默认返回标准字典
     - `finalize_result(self, result, cfg)` — 默认 `dict(result) | {"finalized": True}`
     - `compose_template_inputs(self, ...)` — 默认返回 `{}`
     - `packaged_candidate_env(self, ...)` — 默认返回 `{}`
     - `prewarm_candidate_batch(self, ...)` — 默认空实现
   - 四个具体 adapter（linux、asterinas、tgoskits_arceos、tgoskits_starryos）继承 `BaseTargetAdapter`，只覆盖差异化方法

3. **清理 `runners/` 的空实现**
   - 删除 `LocalRunner`（空继承自 `CommandRunner`，无意义）
   - 或将其合并为 `CommandRunner` 的一个构造参数/别名
   - 收紧 `RunnerProtocol`，移除在 `vm_runner.py` 中从未被调用的方法（如 `collect_outputs`、`prepare`）

**验收标准**：
- `mypy`（若启用）对 `targets/` 和 `runners/` 的类型检查通过，无 Protocol 实现不匹配的错误
- `BaseTargetAdapter` 的默认实现覆盖至少 4 个 adapter 中的公共逻辑
- 四个 adapter 的行数总和显著减少

---

### 阶段四：统一 TGOSKits 目标，消除重复代码

**目标**：`tgoskits_arceos/api.py`（841行）和 `tgoskits_starryos/api.py`（654行）大量重复，需要收敛。

**具体动作**：

1. **新建 `targets/tgoskits_common/` 包**
   - 提取两个 api.py 中共享的函数：
     - `parse_args`, `env_path`, `runner_result_path`, `write_runner_result`
     - `read_workflow_config`, `target_config`, `repo_dir`, `ensure_pinned_revision`
     - `ensure_toolchain_probes`, `resolve_command`, `preflight_payload`
     - `prepare_target`, `healthcheck`, `run_case`, `run_batch` 的公共骨架
   - 将目标特异的逻辑（如 ArceOS 的 C-app 生成 vs StarryOS 的 shared shell）作为参数或子类覆盖点

2. **重构 `tgoskits_arceos/api.py` 和 `tgoskits_starryos/api.py`**
   - 从 `tgoskits_common` 导入公共函数和类
   - 每个文件只保留目标特异的逻辑

**验收标准**：
- `tgoskits_arceos/api.py` + `tgoskits_starryos/api.py` 总行数从 ~1500 行减少到 <500 行
- 公共逻辑变更时只需修改一处
- 两个目标的现有测试全部通过

---

### 阶段五：Go 依赖与技术债务清理

**目标**：修复构建问题，消除低级技术债务。

**具体动作**：

1. **修复 Go 模块**
   - 检查 `third_party/syzkaller` 是否应存在（可能为 git submodule）
   - 若确实缺失，在 `go.mod` 中移除 `replace` 指向本地路径，改为引用远程模块；或添加 `third_party/syzkaller` 的获取说明到 `README.md`
   - 将 `go.mod` 的模块名从 `github.com/plucky/fuzzasterinas` 改为与仓库名一致（如 `github.com/plucky/SysABI`）

2. **消除 `sys.path.insert` Hack**
   - 统计 25 处 `sys.path.insert` 的分布
   - 添加 `pyproject.toml`，将项目配置为 editable install (`pip install -e .`)
   - 所有脚本通过 `python -m tools.xxx` 运行，或统一入口由 `Makefile` 管理 PYTHONPATH

3. **统一异常层次**
   - 在 `core/exceptions.py` 中定义 `RunnerError`、`WorkflowContractError` 等基础异常
   - 清理 `targets/asterinas/common.py`、`targets/tgoskits_arceos/api.py`、`targets/tgoskits_starryos/api.py` 中各自定义的 `RunnerError`

4. **解决 `dataclass(slots=True)` 兼容性**
   - 检查项目最低支持的 Python 版本
   - 若需支持 3.9，移除 `slots=True` 或使用 `@dataclass` + `__slots__` 手动声明

**验收标准**：
- `go build ./cmd/...` 成功编译
- 项目中 `sys.path.insert` 出现次数从 25 处降到 0
- `RunnerError` 只在一处定义
- 项目在 Python 3.9+ 环境中可正常导入

---

### 阶段六：测试与回归验证

**目标**：确保重构过程中所有现有行为不回归，并为新架构增加契约测试。

**具体动作**：

1. **新增 `tests/test_dependency_direction.py`**
   - 使用 `modulegraph` 或 `importlib` 检查 `targets/` 不导入 `orchestrator/`
   - 检查 `core/` 不导入 `orchestrator/` 或 `targets/`

2. **新增 `tests/test_adapter_contract.py`**
   - 验证所有已注册 target adapter 满足 `TargetAdapter` Protocol
   - 验证 adapter 的 `required_config_keys()` 返回合理的值
   - 负面测试：构造一个缺少必要方法的假 adapter，验证注册/校验失败

3. **运行全部现有测试**
   - `python -m unittest discover -s tests -v`
   - 确保 `test_contract_surface.py`、`test_tgoskits_targets.py`、`test_asterinas_pipeline.py`、`test_release_gates.py` 全部通过

**验收标准**：
- 新增 2 个测试文件，覆盖依赖方向和 adapter 契约
- 所有现有测试零回归
- CI (`ci-fast.yml`) 通过

---

## 任务分解与依赖关系

| 阶段 | 任务 | 目标文件/模块 | 依赖 | 预估工作量 |
|------|------|--------------|------|-----------|
| 1-A | 新建 `core/fs_utils.py` | `orchestrator/common.py` → `core/fs_utils.py` | — | 小 |
| 1-B | 新建 `core/env_utils.py` | `orchestrator/common.py` → `core/env_utils.py` | — | 小 |
| 1-C | 新建 `core/path_utils.py` | `orchestrator/common.py` + `core/paths.py` | — | 中 |
| 1-D | 清理 `targets/` 反向依赖 | 11+ 个 targets 文件 | 1-A, 1-B, 1-C | 中 |
| 2-A | 新建 `orchestrator/config_loader.py` | `orchestrator/common.py` | 1-D | 小 |
| 2-B | 新建 `orchestrator/runner_profiles.py` | `orchestrator/common.py` | 1-D | 小 |
| 2-C | 重构 `core/workflow_contract.py` 硬编码 | `core/workflow_contract.py` | — | 中 |
| 3-A | 收紧 `TargetAdapter` Protocol | `targets/base.py` | — | 小 |
| 3-B | 新建 `BaseTargetAdapter` | `targets/base_adapter.py` | 3-A | 中 |
| 3-C | 四个 adapter 继承基类 | `targets/*/adapter.py` | 3-B | 中 |
| 3-D | 清理 `runners/` 空实现 | `runners/local.py`, `runners/base.py` | — | 小 |
| 4-A | 新建 `targets/tgoskits_common/` | 新模块 | 1-D | 大 |
| 4-B | 重构两个 TGOSKits api.py | `targets/tgoskits_*/api.py` | 4-A | 大 |
| 5-A | 修复 Go 模块 | `go.mod`, `README.md` | — | 小 |
| 5-B | 消除 `sys.path.insert` | 25 处分散代码 | — | 中 |
| 5-C | 统一异常层次 | `core/exceptions.py`, 3 个 targets 文件 | — | 小 |
| 5-D | 修复 `dataclass(slots=True)` | `core/paths.py`, `orchestrator/models.py`, `runners/common.py` | — | 小 |
| 6-A | 新增依赖方向测试 | `tests/test_dependency_direction.py` | 1-D | 小 |
| 6-B | 新增 adapter 契约测试 | `tests/test_adapter_contract.py` | 3-C | 小 |
| 6-C | 全量回归测试 | 全部 tests | 所有阶段 | 中 |

**依赖图**：
```
阶段一 (1-A~1-D) ──→ 阶段二 (2-A~2-C)
     │                    │
     ▼                    ▼
阶段三 (3-A~3-D) ←──────┘
     │
     ▼
阶段四 (4-A~4-B)
     │
     ▼
阶段五 (5-A~5-D) ──→ 阶段六 (6-A~6-C)
```

---

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 重构导致现有 workflow 行为变化 | 高 | 每个阶段完成后运行全量测试；保持所有路径和配置键不变；仅移动代码位置 |
| `targets/` 的 `orchestrator` 依赖链复杂，难以一次性切断 | 中 | 分文件逐个清理；每次提交只改一个 target 的一个文件 |
| `BaseTargetAdapter` 的默认实现掩盖了目标特异逻辑 | 中 | 为每个默认方法添加文档说明；保留所有现有测试验证具体行为 |
| `pyproject.toml` 引入 editable install 改变开发流程 | 低 | 保留 `Makefile` 作为统一入口；在 `README.md` 中补充开发环境设置说明 |
| Go 模块的 `third_party/syzkaller` 缺失可能是因为未初始化 submodule | 低 | 先检查 `.gitmodules`；若确实未配置，按阶段五方案修复 |

---

## 附录：关键重构前后对比

### 依赖方向（重构前）
```
            ┌─────────────────┐
            │   orchestrator/ │
            │    common.py    │
            └────────┬────────┘
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   targets/    runners/      tools/
        │            │            │
        └────────────┴────────────┘
                     │
              ┌──────▼──────┐
              │    core/    │
              └─────────────┘
（双向混乱依赖，orchestrator/common.py 是黑洞）
```

### 依赖方向（重构后）
```
   tools/
      │
      ▼
 orchestrator/  ──→  runners/
      │                  │
      ▼                  ▼
 targets/  ←────────  core/
      │
      ▼
   utils/  (core/fs_utils.py, core/env_utils.py, core/path_utils.py)
（严格向下依赖，orchestrator 不再被底层导入）
```
