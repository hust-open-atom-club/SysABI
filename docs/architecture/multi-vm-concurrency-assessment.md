# Multi-VM Concurrency Assessment

## Executive Summary

SysABI 当前采用 **ThreadPoolExecutor + subprocess** 模型驱动多实例并发 fuzz。本次评估验证了该模型在 StarryOS、Asterinas、ArceOS 三目标并发场景下的可行性，识别了主要瓶颈，并提出了中长期的架构演进建议。

**结论**：当前模型在 `max_concurrent_vms` 限制下可以稳定运行，但存在 Python GIL 瓶颈和子进程管理复杂度问题。建议短期内保持 ThreadPool，中期评估 ProcessPoolExecutor 替换，长期考虑 Runner 层拆分。

---

## Current Architecture

### Execution Flow

```
scheduler.py (ThreadPoolExecutor: max_workers=jobs)
  ├── prepare_case()          [Thread: build + reference run]
  │      └── run_reference_once()
  │             └── execute_side() -> runner.run_case() -> subprocess.Popen()
  ├── schedule_one()          [Thread: candidate run + finalize]
  │      └── run_candidate_once()
  │             └── execute_side() -> runner.run_case() -> subprocess.Popen()
  └── finalize_prepared_case() [Thread: triage reruns]
         └── run_reference_once() / run_candidate_once()
```

### Concurrency Controls

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| ThreadPool | `ThreadPoolExecutor(max_workers=jobs)` | Worker thread count |
| VM Semaphore | `threading.Semaphore(max_concurrent_vms)` | Concurrent VM instances |
| Process Group | `os.killpg(process.pid, SIGKILL)` | Cleanup on timeout |

### Resource Model

| Target | Per-VM Memory | Isolation Strategy | Copy Overhead |
|--------|--------------|-------------------|---------------|
| Asterinas | ~2GB (Docker) | Docker container per case | Low (image reuse) |
| StarryOS | ~512MB | Disk image copy per case | Medium (~100MB/copy) |
| ArceOS | ~512MB | Workspace copy per case | High (~500MB+/copy) |

---

## Bottleneck Analysis

### 1. Python GIL Limitation

**Problem**: `ThreadPoolExecutor` 的所有 worker 线程共享 Python GIL。当 `jobs` 较大时，scheduler 的线程调度本身成为瓶颈，而非宿主机 CPU。

**Impact**: 
- 低：当 `jobs <= 8` 且 VM 运行时间 > 10s 时，线程调度开销可忽略
- 中：当 `jobs > 16` 或 VM 运行时间 < 1s 时，GIL 争用明显

**Evidence**: 无直接基准测试，但基于 Python subprocess 启动延迟（~50-100ms）和 GIL 切换开销的估算。

### 2. Subprocess Lifecycle Overhead

**Problem**: 每个 case 需要启动 `subprocess.Popen()`，建立管道、环境变量、工作目录。对于短运行 case（< 5s），启动开销占比显著。

**Impact**: 
- StarryOS/ArceOS QEMU 启动时间 ~3-5s，相对 VM 运行时间（30-300s）可接受
- Asterinas Docker 启动时间 ~1-2s，受益于镜像缓存

### 3. Memory Pressure

**Problem**: 每个 QEMU/Docker 实例消耗固定内存。`max_concurrent_vms` 设置为 4 时，峰值内存约 8-10GB（Asterinas 2G x 4）。

**Impact**: 
- 宿主机内存 >= 32G 时无压力
- 宿主机内存 < 16G 时可能发生 OOM，触发 candidate_bug 或 infra_error

### 4. Disk I/O Contention

**Problem**: StarryOS 和 ArceOS 每个 case 都需要拷贝磁盘镜像/工作区到 sandbox。当 `max_concurrent_vms` 较高时，磁盘 I/O 成为瓶颈。

**Mitigation**: 已实现的 `_copytree_with_links` 使用 hardlink 优先策略，大幅减少实际写入量。

---

## Isolation Strategy Comparison

| Strategy | Copy Scope | Concurrency Safety | Overhead | Target |
|----------|-----------|-------------------|----------|--------|
| Disk image copy | `repo_dir` + disk image | 高（完全隔离） | 中 | StarryOS |
| Workspace copy | `repo_dir/os/arceos` | 高（完全隔离） | 高 | ArceOS |
| Docker container | Docker volume | 高（OS 级隔离） | 低 | Asterinas |
| Shared runtime | 无 | 低（需串行化） | 无 | 旧 StarryOS |

**Recommendation**: 当前三种隔离策略均为各自目标的最小可行方案。ArceOS 的 workspace copy 开销最大，但 correctness 优先。

---

## Improvement Roadmap

### Short Term (M1-M3, Completed)

- [x] `max_concurrent_vms` Semaphore 限制并发 VM 数
- [x] `killpg` + `wait` 确保超时后进程完全清理
- [x] Hardlink-first copy 减少磁盘 I/O
- [x] Per-case sandbox 隔离避免并发写冲突

### Medium Term (Post-M6)

**Option A: ProcessPoolExecutor 评估**

将 `schedule_entries()` 中的 `ThreadPoolExecutor` 替换为 `ProcessPoolExecutor`：
- **Pros**: 消除 GIL 瓶颈，充分利用多核 CPU
- **Cons**: 
  - JSON/pickle 序列化开销（campaign results 可能很大）
  - `tempfile` 和 Docker 状态共享问题
  - 子进程间的 semaphore 共享需要额外机制（如 `multiprocessing.Semaphore`）
- **Effort**: 中等（1-2 周）
- **Risk**: 中（可能引入序列化 bug 或状态同步问题）

**Option B: VM Pool / Snapshot Reuse**

对 StarryOS/ArceOS 预启动 QEMU 实例池，通过 snapshot/restore 复用：
- **Pros**: 消除每次 case 的 QEMU 启动时间（3-5s -> 0.5s）
- **Cons**: 
  - 需要修改 QEMU 启动参数支持 snapshot
  - 内存占用增加（需要保持 N 个 QEMU 常驻内存）
- **Effort**: 高（3-4 周）
- **Risk**: 高（snapshot 一致性难以保证）

**Option C: Runner Layer Refactoring**

拆分 `CommandRunner` 为：
- `BaseRunner`: 接口定义
- `LocalRunner`: 本地进程（baseline reference）
- `QEMURunner`: QEMU 进程管理（killpg、monitor、端口复用）
- `DockerRunner`: Docker 容器管理（run、rm、日志收集）
- **Pros**: 每个 runner 可以独立优化，Docker/QEMU 特殊逻辑不再散落
- **Cons**: 重构范围大，需要更新所有 target adapter
- **Effort**: 高（4-6 周）
- **Risk**: 中（接口变更影响所有 target）

### Long Term

- 评估宿主机级 cgroup/memory limit，防止 OOM
- 引入分布式 runner，支持跨多台物理机并发
- QEMU 9p/fs 共享目录替代 disk image copy

---

## Recommendation

1. **保持 ThreadPool + subprocess 模型** 完成 M4-M6
2. **在 M6 E2E 验证阶段收集性能基准**（CPU 利用率、内存峰值、QEMU 启动时间）
3. **M6 结束后评估 ProcessPoolExecutor 替换的可行性**，如果 `jobs > 8` 且 CPU 利用率 < 50%，优先实施 Option A
4. **Runner 层拆分（Option C）** 作为下一个大版本的架构目标

---

## Acceptance Criteria Mapping

| AC | Relevant Finding |
|----|-----------------|
| AC-4 | `max_concurrent_vms` + `killpg` 已解决并发控制和子进程清理 |
| AC-5 | candidate_bug 分类已区分 infra_error 和真实 bug |
| AC-6 | healthcheck 在 campaign 启动前阻止环境未就绪的执行 |
| AC-8 | Makefile 统一命令降低多目标并发操作的心智负担 |
