# 基于 Codebase Memory 的项目分析与评价

## 总体结论

基于 **codebase-memory 0.10.8 全量知识图谱**、关键代码路径审查和测试验证，对当前项目的判断是：

> **这是一个架构思想成熟、核心契约扎实，但崩溃一致性还没有完全闭环的 pre-alpha Agent Runtime。** 适合本地单用户、受控试点和继续研发；暂不适合把高价值仓库的无人值守写操作完全托付给它。

- **技术实现成熟度：3/5**
- **同类预览项目中的工程质量：4/5**
- **高风险生产可用度：2/5**
- **方向正确性：4/5**

没有发现可直接定为 P0 的远程漏洞或必然数据丢失问题，但发现了三个值得优先处理的 P1 级恢复/并发风险。

## 分析依据

分析对象：`HEAD 2fe75a9`

Codebase Memory 索引结果：

| 指标 | 数量 |
|---|---:|
| 图节点 | 2,513 |
| 图关系 | 16,307 |
| Python 文件 | 83 |
| 类 | 240 |
| 方法 | 1,245 |
| 函数 | 206 |
| `CALLS` 关系 | 5,211 |
| `TESTS` 关系 | 1,396 |
| 解析失败/遗漏 | 0 |

验证结果：

- **375 tests passed**
- Ruff：通过
- mypy：46 个生产模块无报错
- 分析过程未写入 `.codebase-memory` 仓库产物

## 做得好的地方

### 1. 核心理念不是文档概念，已经落实到代码

以下能力都有实际实现和离线测试，不是空壳：

- DAG 和 Artifact 读写契约
- 无序共享写冲突检查
- Reality Anchor
- 审批摘要与 workspace/policy/plan 绑定
- 预算预留与结算
- verified reuse
- Effect Receipt
- Resident 持久队列
- Orca 隔离工作区与受控合并
- RSI 候选、评估、审批、灰度和回滚

图校验尤其扎实，`grapheng/validation.py:46-229` 把很多安全约束提前到执行前，以 fail-closed 方式拒绝不完整的图。

### 2. 本地 Agent Adapter 是真实实现

Codex、Claude Code、Pi、OpenCode 都有实际 CLI 协议适配、JSON/JSONL 解析、超时和输出上限处理，集中在 `grapheng/adapters.py`。

项目没有把“检测到命令”伪装成“已经支持”：版本、平台或协议证据不匹配时不会加入执行器注册表。这一点比多数同类编排项目严谨。

但应明确：这里提供的是**进程参数、权限配置和协议层隔离**，不是 OS 级沙箱、网络隔离或凭据保险库。

### 3. 测试和发布工程明显高于普通 pre-alpha

- 46 个生产模块，对应 34 个 `test_*.py`
- 有真实跨进程故障接管、迁移失败、回滚和 checkpoint 恢复测试
- CI 覆盖 Python 3.9/3.12/3.13 × Ubuntu/macOS
- wheel 和 sdist 都在 checkout 外安装并做 CLI 烟测
- `_store.py` 已实现临时文件、文件 `fsync`、原子替换和目录 `fsync`

因此它并非“概念验证代码”，而是已经进入**系统正确性工程阶段**。

## 主要风险

### P1：Lease 缺少真正的跨进程 fencing

相关代码：`grapheng/control.py:284-290,414-425,475-515`

当前 lease claim 更接近“读取后检查”，状态更新主要受实例内 `threading.Lock` 保护。两个 `LocalControlPlane` 进程可能同时认为自己拥有同一个 run。

虽然有 `generation`，但 heartbeat、完成和失败写回没有完整携带并校验 fencing token。结果是：

- 新 owner 接管后，旧 owner 仍可能继续产生副作用；
- 旧 owner 的延迟完成可能覆盖新 owner 状态。

Resident 单实例锁降低了日常触发概率，但不能证明公开控制面的 lease/takeover 语义成立。

### P1：普通 Graph 写节点存在 at-least-once 重放风险

相关代码：

- `grapheng/runtime.py:299-303,530-559,710-718`
- `grapheng/agent_nodes.py:27-51`

恢复时，除 `COMPLETED` 外的节点会重新进入 `PENDING`。如果 Agent 已通过 `shell/edit/write` 修改工作区，却在 Artifact/checkpoint 提交前崩溃，恢复后可能再次执行同一写操作。

受控 Git merge 有较好的 Effect Receipt 保护，但普通 shared-workspace Agent 节点没有统一的 prepare/commit/reconcile 契约。

这与 README 中“崩溃后不会静默重放副作用”的强表述存在一定差距。

### P1：protected-path 校验存在崩溃窗口

相关代码：`grapheng/engineering.py:616-623,688,953-958`

当前顺序中，mutating effect 的 completed receipt 可能先落盘，之后才检查 protected paths。若进程恰好在两者之间崩溃：

1. 恢复时发现 receipt 已完成，跳过原操作；
2. 又可能基于已经被修改的 workspace 重建 baseline；
3. 使原本越权的修改被接受。

这是写盘顺序与恢复语义不一致的问题。

### P2：若干跨状态所有者的交接还不原子

包括：

- `approve → queued → resident.submit` 分多次提交，崩溃可能产生孤儿任务（`grapheng/tasks.py:254-273`）
- Graph pause 可能等到底层已经完成后，仍向队列报告 paused（`grapheng/resident_jobs.py:134-152`）
- Orca 的 dispatch/gate/reply/cleanup 出现 indeterminate receipt 后会安全拒绝重放，但缺少公开 reconcile/reset 流程
- checkpoint 和 event sink 尚未完全复用统一 durability contract；部分路径缺目录 `fsync` 或事件 `fsync`

这些设计都偏向“宁可停住，也不盲目执行”，安全方向正确，但会降低故障后的可恢复性。

## Codebase Memory 揭示的复杂度热点

项目整体方法复杂度并不高：

- 生产 callable：906
- 圈复杂度中位数：1，P90：7
- 认知复杂度中位数：1，P90：10

问题主要集中在少数编排函数：

| 函数 | LOC | 圈复杂度 | 认知复杂度 | fan-out |
|---|---:|---:|---:|---:|
| `cli._dispatch` | 570 | 53 | 125 | 87 |
| `runtime.run` | 327 | 32 | 122 | 57 |
| `engineering.execute` | 250 | 23 | 55 | 36 |
| `validation.validation_issues` | 197 | 51 | 143 | 31 |
| `economics._node_metrics` | 165 | 20 | 52 | 27 |

另外：

- 44 个生产 callable 圈复杂度 ≥ 10
- 51 个认知复杂度 ≥ 15
- 35 个函数/方法 ≥ 80 行
- 23 个 callable fan-out ≥ 20

因此当前不是“整个代码库混乱”，而是**控制流和状态迁移过度集中在少数巨型函数/类中**。

Codebase Memory 还识别出两个调用 SCC，其中一个横跨 `OrcaCoordinator` 与 `ControlledGitMerger` 的 27 节点环。它不等于 Python import cycle，但说明协调、合并和恢复流程之间已经形成较强的双向控制耦合。

## 产品声明与实际成熟度

| 领域 | 评价 |
|---|---|
| Graph/Artifact/审批 | 实现扎实，接近 4/5 |
| Adapter | 四个真实实现，但认证平台和版本较窄 |
| Budget | 账本与准入真实；硬执行上限并非所有 Adapter 都支持 |
| Resident | 持久队列和恢复较强，跨状态交接仍需补齐 |
| Security | fail-closed 很好，但不是强沙箱 |
| RSI | 治理原语完整，尚无真实项目收益证据 |
| 生产采用 | 仍缺真实 Agent/Orca soak、签名发布和威胁模型 |

尤其需要注意：

- 目前只有 Claude Code 的硬美元预算具备明确执行器支持；
- Codex、Pi、OpenCode 没有硬 token budget 能力时会正确拒绝执行；
- RSI 当前是“受治理的路由策略改进”，不是自主修改代码；
- 文档整体算诚实，但“crash-safe continuation”“副作用不重放”等措辞应附带适用边界。

## 建议优先级

### 第一阶段：先补正确性地基

1. 给每个 run 引入真正的 owner fencing token：
   - 原子 claim；
   - heartbeat、effect、完成写回全部校验 generation；
   - 增加旧 owner 延迟完成、双进程 takeover 测试。

2. 统一副作用协议：
   - `prepare → execute → commit/reconcile`；
   - 写代码的 Agent 默认进入隔离 workspace；
   - shared workspace 仅允许声明为纯操作或可验证幂等操作。

3. 用 durable outbox 处理：
   - approve → enqueue；
   - pause/cancel → execution acknowledgment；
   - queue 状态与任务状态的跨 owner 交接。

### 第二阶段：降低复杂度

优先抽出：

- `effects.py`
- `leases.py`
- `state_machine.py`
- `durable_outbox.py`

随后拆分：

- `cli._dispatch`
- `runtime.run`
- `engineering.execute`
- `economics.py`
- `adapters.py`

重点不是单纯缩短文件，而是让“状态迁移规则”从巨型条件分支变成可枚举、可测试的状态机。

### 第三阶段：建立生产证据

- CI 增加 branch coverage 和高风险模块阈值
- 对外部 JSON/状态文件使用 `TypedDict` 或数据类
- 冻结最小公共 API，增加签名/schema contract 测试
- CI 使用 `uv.lock` 锁定依赖
- 做真实 Codex/Claude/Pi/Orca 故障注入与长时 soak
- 补 SBOM、签名发布和正式威胁模型

## 最终评价

这个项目最有价值的地方不是“又一个多 Agent 调度器”，而是它认真处理了其他项目经常忽略的内容：**Artifact 契约、审批绑定、预算准入、故障恢复、受控副作用和学习治理**。

当前最大的矛盾也很明确：

> 上层已经宣称接近“可恢复执行系统”，但底层跨进程 ownership、fencing、事务 handoff 和副作用 reconcile 还没有完全达到这个承诺。

如果先完成这层正确性闭环，而不是继续增加更多 Adapter 或功能入口，项目会从“优秀的 pre-alpha”明显跨进到“值得真实团队试点的基础设施”。
