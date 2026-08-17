# Graph Engineering Agent OS Foundation

[![Version](https://img.shields.io/badge/version-0.0.1-blue.svg)](https://github.com/Feahter/agent-os/releases/tag/v0.0.1)
[![CI](https://github.com/Feahter/agent-os/actions/workflows/ci.yml/badge.svg)](https://github.com/Feahter/agent-os/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个零第三方运行时依赖、兼容 Python 3.9 的任务图内核。它把多 Agent 编排所需的
基础语义从具体 Agent 产品中拆出来：可验证的 DAG、声明式 Artifact 契约、预算准入、
人工门控、独立验证节点、现实锚点、结构化事件和检查点恢复。

## 已实现

- JSON GraphSpec 解析与边界校验
- 循环、缺失依赖、无上游读取、并发写冲突和验证拓扑检查
- 不可变、版本化、带 SHA-256 校验和的 Artifact Store
- 基于依赖就绪状态的并发调度
- 节点重试、失败阻断、预算预留和命名门控
- `run_id / graph_id / node_id` 结构化 JSONL 事件
- 原子检查点与恢复；图指纹防止错用同名 checkpoint，已完成节点不会重复执行
- 默认要求所有终点路径都经过 Reality Anchor
- 统一 `AgentExecutor` seam、能力注册与确定性路由
- Codex、Claude Code、Pi Agent 本地 CLI Adapter
- Agent 输出、token、费用、session 与图事件归一化
- 共享工作区写冲突静态检查与显式隔离 workspace 契约
- GraphSpec 到 Orca Run/Task/Dispatch/deps/gate 的编译与物化后端
- Orca `worker_done / escalation / question` 事件归一化
- 隔离 worktree 的变更集、合并 gate、冲突和保留/释放契约
- 本地异步控制面：submit、事件游标、cancel、heartbeat、lease、resume
- Effect receipt 幂等保护；不确定副作用禁止盲目重放
- 按能力、预计费用、延迟、健康度和数据级别统一选择执行器
- 跨进程持久的供应商限流、连续失败熔断、冷却与单探针恢复
- 图级美元预算准入、结算、事件记录和检查点恢复
- 自包含本地操作控制台：运行图、Artifact 血缘、审批、失败回放和费用
- revision/event cursor 增量刷新的 loopback 实时操作台，界面审批复用同一收件箱
- Prompt-free RSI 遥测、Reality Anchor 质量反馈、灰度激活和回滚
- 按任务类型、工具集和模型族学习的条件化路由；局部样本不足时回退全局策略
- 从重复失败生成提示/拓扑候选，经冻结回归、审批、灰度和自动回滚后生效
- Reality Anchor 显式判定后自动发布的安全复用、TTL/校验和失效与并发任务合并
- 跨 Codex、Claude Code、Pi 进程的共享 lease、heartbeat、失主接管与脱敏结果交付
- 统一 `AgentOS` 根目录、逐版本 bundle 转换、完整性验证与目录级事务导入
- 安装/状态自诊断、兼容矩阵、迁移演练和可校验的自包含发行目录
- 面向日常开发的标准工程循环：探索、计划、摘要审批、实现、检查、独立审查和有限修复
- 项目规则、保护路径、工作区基线、检查证据、成本与 RSI 质量反馈的统一工程报告

## 安装

需要 Python 3.9+，运行时无第三方依赖：

```bash
git clone https://github.com/Feahter/agent-os.git
cd agent-os
python3 -m pip install -e .
agent-os validate examples/minimal_graph.json
```

也可不安装，直接在源码目录运行。

## 快速验证

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 -m grapheng.cli validate examples/minimal_graph.json
PYTHONPATH=. python3 -m grapheng.cli demo examples/minimal_graph.json --work-dir /tmp/grapheng-demo
PYTHONPATH=. python3 -m grapheng.cli executors
PYTHONPATH=. python3 -m grapheng.cli validate examples/heterogeneous_agents.json
PYTHONPATH=. python3 -m grapheng.cli validate examples/verified_reuse_agents.json
PYTHONPATH=. python3 -m grapheng.cli orca-plan examples/orca_agents.json
PYTHONPATH=. python3 -m grapheng.cli agent-os compatibility
```

## 日常工程循环

`engineer` 把文章中值得借鉴的工程协议固化为一个有界流程，不要求用户手写 GraphSpec：

```text
只读探索 → 计划 Artifact → 人工批准计划摘要 → 实现
→ 保护路径校验 → 项目检查 → 独立只读审查
→ 最多 N 轮修复/复测/复审 → Reality Anchor 报告
```

初始化项目策略，然后把单次任务状态放到项目之外的显式目录（模块会拒绝工作区内的
`task-dir`，避免 Agent 接触或污染审批与 Effect receipt）：

```bash
PYTHONPATH=. python3 -m grapheng.cli engineer init \
  --workspace /path/to/project

PYTHONPATH=. python3 -m grapheng.cli engineer plan \
  --workspace /path/to/project \
  --task-dir /tmp/agent-os-tasks/task-001 \
  --agent-os-root /path/to/agent-os-root \
  --objective "修复登录超时并补回归测试"

PYTHONPATH=. python3 -m grapheng.cli engineer run \
  --workspace /path/to/project \
  --task-dir /tmp/agent-os-tasks/task-001 \
  --agent-os-root /path/to/agent-os-root \
  --approved-by operator \
  --plan-digest PLAN_DIGEST

PYTHONPATH=. python3 -m grapheng.cli engineer status \
  --task-dir /tmp/agent-os-tasks/task-001
```

也可在交互终端使用 `engineer ship`，在一个命令内完成计划展示、摘要确认和执行；它始终要求
`y/N` 确认，自动化环境必须改用 `plan` 后再以 `run --plan-digest` 执行。默认策略位于
`.agent-os/engineering.json`，检查命令必须是参数数组，不经过 shell 拼接。默认要求干净 Git
工作区，并保护普通仓库或链接 worktree 的 Git 指针、HEAD、index、config、refs，以及策略文件、
`AGENTS.md` 和 `CLAUDE.md`。初始化策略后应先审阅并提交它，再开始规划。

探索、计划和审查只获得 `read`；实现和修复才获得写工具。写调用明确禁止结果复用，避免缓存命中
却没有把变更写入工作区；只读调用绑定工作区指纹后仍可安全去重。计划摘要同时绑定策略、项目规则
和工作区基线，批准后任一项漂移都要求重新规划。`max_agent_calls` 覆盖探索、计划、实现、审查和
修复的完整调用数，准备阶段用量也进入最终报告；每次 Agent/检查超时都会收紧到剩余总时长。
循环受 `max_review_cycles / max_agent_calls / max_elapsed_seconds` 约束，不会无限“修到干净”。最终
独立审查分数会反馈给 RSI 的最近一次实现或修复任务，供后续按 `engineering.*` 任务类型学习质量、
成本和延迟。

## GraphSpec 摘要

```json
{
  "id": "research-report",
  "max_concurrency": 2,
  "max_tokens": 1000,
  "require_reality_anchor": true,
  "nodes": [
    {
      "id": "verify",
      "kind": "verify",
      "deps": ["merge"],
      "reads": ["report"],
      "writes": ["verified_report"],
      "verifier_for": "merge",
      "reality_anchor": true,
      "gate": "release"
    }
  ]
}
```

节点处理器通过 `NodeRegistry` 注册。处理器只能读取 `reads` 中声明的 Artifact，返回值
必须精确匹配 `writes`，从而让依赖、并发和恢复行为保持可审计。

图可设置 `max_cost_usd`。Agent 节点此时必须同时设置自身的 `max_cost_usd`，运行时才有
可执行的最坏费用边界；费用未知时按预留上限保守结算，恢复运行也会继承已用额度。

## 策略路由

`ExecutorRegistry` 仍是唯一执行入口。注册执行器时可附带 `ExecutorProfile`，声明供应商、
预计单次费用、预计延迟和允许处理的数据级别；`PolicyRouter` 再叠加供应商限流与熔断：

```python
router = PolicyRouter({
    "anthropic": ProviderPolicy(requests_per_minute=20, failure_threshold=3)
})
executors = ExecutorRegistry(router)
executors.register(
    claude,
    ExecutorProfile(
        provider="anthropic",
        estimated_cost_usd=0.20,
        estimated_latency_seconds=8,
        data_classifications=("public", "internal"),
    ),
)
```

候选先满足能力和工具契约，再依次受数据权限、费用、延迟、限流和熔断约束，最后按健康、
费用、延迟和执行器 ID 确定性排序。执行已经开始后不会自动切换供应商；重试由 GraphSpec
统一拥有，避免一次逻辑任务被重复执行。

通过 `AgentOS.router()` 创建路由器时，治理状态会写入统一根目录的 `routing/`：请求窗口、连续
失败、熔断时间和 half-open probe 租约由文件锁原子更新，多个 Codex、Claude Code、Pi 进程不会
重复占用最后一个额度。冷却后只放行一个探针；owner 消失可在 `probe_timeout_seconds` 后接管。
状态带 schema、generation 和 checksum，损坏、未来版本或系统时钟回退均按保守方式处理。

## 异构 Agent 运行

`examples/heterogeneous_agents.json` 演示 Claude Code 生成、Pi Agent 独立复核的任务图：

```bash
PYTHONPATH=. python3 -m grapheng.cli agent-run \
  examples/heterogeneous_agents.json \
  --work-dir /tmp/grapheng-agent-state \
  --workspace /path/to/project \
  --learning-root /tmp/grapheng-learning
```

该命令会触发真实模型调用并可能产生费用。Agent 仅获得节点声明的标准工具
`read / shell / edit / write`；Adapter 会翻译成各产品的工具名。无依赖关系的 Agent
只要其中一个具有写能力，就会因共享工作区竞态而被静态拒绝。将节点声明为
`"workspace": {"mode": "isolated"}` 后，必须改由 Orca 后端承接；直接
`agent-run` 会拒绝执行，避免隔离配置静默失效。

传入 `--learning-root` 后，调用会自动写入脱敏遥测并加载已激活策略。遥测不包含 prompt、
输入或 Artifact 内容，只包含执行器、供应商、成功、延迟、费用和数据级别。

## 操作控制台与审批

控制面运行完成后可生成一个不依赖服务器的本地 HTML：

```bash
PYTHONPATH=. python3 -m grapheng.cli console \
  --control-root /tmp/grapheng-control \
  --run-id RUN_ID \
  --learning-root /tmp/grapheng-learning \
  --output /tmp/agent-os-console.html
```

审批决策写入独立收件箱；`ApprovalInbox.policy_for(run_id)` 可直接传给
`LocalControlPlane.resume()`：

```bash
PYTHONPATH=. python3 -m grapheng.cli approval \
  --control-root /tmp/grapheng-control \
  --run-id RUN_ID --gate release --decision allow --actor operator
```

控制台不嵌入 Artifact 实际内容，只显示血缘、版本和 checksum，减少敏感数据扩散。
传入 `--optimization-root` 后还会显示已激活的提示与图拓扑候选。

需要自动刷新和界面内审批时，启动仅绑定本机回环地址的实时操作台：

```bash
PYTHONPATH=. python3 -m grapheng.cli console-serve \
  --control-root /tmp/grapheng-control \
  --run-id RUN_ID \
  --agent-os-root /tmp/my-agent-os
```

`OperationsAPI` 用 revision 哈希和事件游标返回条件快照与增量事件；快照未变时只返回
游标和新事件。审批 POST 使用每次启动随机生成的 action token，服务拒绝非 loopback
绑定，并设置 no-store、CSP 和防嵌套响应头。审批最终仍只由 `ApprovalInbox.decide()`
持久化，因此 CLI、静态控制台和实时界面不会产生多套 gate 事实。

## RSI：越用越顺、越智能、越省

RSI 不是运行时自改代码，而是受治理的策略闭环：

1. Agent 调用自动沉淀成功率、费用和延迟。
2. Reality Anchor、验证器或人工通过 `rsi feedback` 提供质量分。
3. `rsi propose` 形成不可混淆的版本化候选，`rsi evaluate` 做保守离线评估。
4. 只有 `rsi approve` 后才能 `rsi activate`，新策略默认只覆盖 10% 任务。
5. 任何异常都可 `rsi rollback`；权限、预算、限流、熔断永远不受学习策略修改。

Agent 节点可声明低基数的 `task_type` 和 `model_family`。RSI 会连同标准工具集生成不含
prompt 的条件键，分别学习诸如“编码 + 读写工具”和“研究 + 只读工具”的最佳执行器；
局部样本不足时自动回退到全局估计，避免冷启动抖动。没有足量 Reality Anchor/验证质量
样本的执行器不能通过学习质量门槛。

```bash
PYTHONPATH=. python3 -m grapheng.cli rsi feedback \
  --learning-root /tmp/grapheng-learning \
  --task-id RUN:NODE:ATTEMPT --score 0.95 --source reality-anchor

PYTHONPATH=. python3 -m grapheng.cli rsi propose \
  --learning-root /tmp/grapheng-learning --min-observations 6
PYTHONPATH=. python3 -m grapheng.cli rsi evaluate \
  --learning-root /tmp/grapheng-learning --candidate-id RSI_ID
PYTHONPATH=. python3 -m grapheng.cli rsi approve \
  --learning-root /tmp/grapheng-learning --candidate-id RSI_ID --actor operator
PYTHONPATH=. python3 -m grapheng.cli rsi activate \
  --learning-root /tmp/grapheng-learning --candidate-id RSI_ID
```

路由先满足成功率和验证质量门槛，再优先比较真实费用与延迟，最后才用质量分破局。因此
系统不会为了便宜而选择质量不达标的 Agent，也不会为小幅质量差异接受无边界的成本回退。

### RSI 优化候选

重复的 `missing_evidence / format_mismatch / verification_failure / race_condition` 失败模式可生成
保守候选：提示候选只能追加指导；拓扑候选只能增加依赖或降低并发上限，不能改预算、数据权限、
gate、Reality Anchor 或放宽并发硬约束。低于最小出现次数的噪声不会触发候选。

```bash
PYTHONPATH=. python3 -m grapheng.cli rsi-opt freeze-suite \
  --optimization-root /tmp/grapheng-optimization \
  --cases examples/rsi_regression_cases.json

PYTHONPATH=. python3 -m grapheng.cli rsi-opt suggest \
  --optimization-root /tmp/grapheng-optimization \
  --failures examples/rsi_failure_patterns.json

PYTHONPATH=. python3 -m grapheng.cli rsi-opt evaluate \
  --optimization-root /tmp/grapheng-optimization \
  --candidate-id OPT_ID --suite-id SUITE_ID \
  --measurements examples/rsi_regression_measurements.json
```

回归集只保存 fixture 的 SHA-256 与基线指标，不保存 prompt 或输入内容。评估必须覆盖冻结集中的
每个样例，Reality Anchor、质量、总费用和总延迟全部达标后才能人工批准。候选默认 10% 灰度；
灰度观测任一指标越界会自动回滚。已激活候选可通过 `--optimization-root` 同时应用于
`agent-run` 和 `orca-plan`，用 `--optimization-rollout-key` 提供稳定的任务/运行灰度键。

### 安全复用与并发去重

`--agent-os-root` 启用统一 RSI、优化、审批和复用状态。相同请求先按 prompt/input 摘要、
执行器、模型、工具、输出契约、费用上限、数据级别和 `reuse_scope` 精确匹配。缓存键不保存
prompt 或输入原文；`confidential / restricted` 默认绕过。命中按 0 token、0 费用结算。

同进程与跨进程并发重复请求都只由一个 leader 执行。共享 lease 使用 heartbeat 续租；owner
退出后由一个等待者接管。等待者必须在执行完成前登记，交付收据只短暂保存结构化 outputs、
token/费用和来源任务，不保存 prompt/input、raw response 或 session。失败只传播错误类型与摘要。
这些未验证结果位于 `reuse/flights` 运行态目录，带 schema、checksum 和短 TTL，不会成为持久复用
条目，也不会进入 Agent OS 导出包。只有 Reality Anchor 自动发布后才进入 `reuse/entries`。

持久复用必须由 Reality Anchor 明确通过，执行结束本身不构成验证。验证节点用
`verified_reuse` 声明判定字段、质量字段和最低质量，并且必须读取目标 Agent 的全部输出：

```json
{
  "id": "verify",
  "kind": "verify",
  "deps": ["answer"],
  "reads": ["answer"],
  "writes": ["verification"],
  "verifier_for": "answer",
  "reality_anchor": true,
  "verified_reuse": {
    "decision_artifact": "verification",
    "passed_path": ["passed"],
    "quality_path": ["quality_score"],
    "minimum_quality_score": 0.9
  }
}
```

当 `verification` 为 `{"passed": true, "quality_score": 0.95}` 时，带
`--agent-os-root` 的 `agent-run` 和配置了复用库的 `LocalControlPlane` 会自动发布。发布收据只保存版本、
校验和和来源，不保存 prompt/input 原文；失败会在 resume 时补发。底层手动接口仍可用于
图外验证来源：

```python
agent_os = AgentOS(Path("/tmp/my-agent-os"))
cache = agent_os.reuse_store()
cache.publish_verified(
    request,
    result,
    source_run_id="run-123",
    verification_id="reality-anchor-7",
    quality_score=0.96,
)
```

### Agent OS 收束与迁移

稳定根目录包含 `learning / optimization / reuse / approvals / routing` 和版本清单。`reuse/flights`
与 `routing/state.json` 是可清理、不可迁移的进程协调/供应商保护状态。凭据、worktree、运行
Artifact、prompt/input 原文及未验证结果
均不属于可迁移状态；未知文件、符号链接、
绝对路径、`..`、校验和损坏和未知 schema 会被拒绝。导入默认要求目标没有既有状态，
避免无声覆盖或混合两个 OS 身份。

根状态 schema 与 bundle schema 独立演进：当前根状态为 v1，导出包为 v2。旧 v1 bundle 会在同一
文件系统的 staging 目录中按注册表逐版本转换；verified reuse 条目会真实补入费用边界并重算
key、checksum 和文件名。转换后重新生成文件清单并完整验证，最后以整个目录为提交单位；提交
失败会恢复原空根目录。迁移审计不记录源绝对路径；活动或损坏的 single-flight，以及非空或损坏
的路由治理状态都会阻止导入。

```bash
PYTHONPATH=. python3 -m grapheng.cli agent-os status \
  --root /tmp/my-agent-os
PYTHONPATH=. python3 -m grapheng.cli agent-os export \
  --root /tmp/my-agent-os --bundle /tmp/my-agent-os.bundle
PYTHONPATH=. python3 -m grapheng.cli agent-os import \
  --root /tmp/restored-agent-os --bundle /tmp/my-agent-os.bundle

PYTHONPATH=. python3 -m grapheng.cli agent-run \
  examples/heterogeneous_agents.json \
  --work-dir /tmp/grapheng-agent-state \
  --workspace /path/to/project \
  --agent-os-root /tmp/my-agent-os
```

原有 `--learning-root / --optimization-root` 仍可单独使用；为防止状态分裂，不能与
`--agent-os-root` 同时传入。

### 自诊断、恢复演练与发行

`doctor` 检查 Python、源码布局、POSIX 文件锁/原子替换、Agent OS 状态，以及 Claude Code、
Codex、Pi、Orca 的 CLI 协议。外部工具只执行 `--help / --version`，不会调用模型或创建 Orca
对象。未安装的可选工具记为 warning；已安装但协议漂移则失败关闭。

```bash
# 版本和 schema 兼容矩阵
PYTHONPATH=. python3 -m grapheng.cli agent-os compatibility

# 安装、文件系统、状态和本机工具协议检查
PYTHONPATH=. python3 -m grapheng.cli agent-os doctor \
  --root /tmp/my-agent-os

# 当前状态或指定旧 bundle 的无修改恢复演练
PYTHONPATH=. python3 -m grapheng.cli agent-os rehearse \
  --root /tmp/my-agent-os
PYTHONPATH=. python3 -m grapheng.cli agent-os rehearse \
  --root /tmp/my-agent-os --bundle /path/to/legacy.bundle

# 生成新的自包含发行目录；目标必须不存在
PYTHONPATH=. python3 -m grapheng.cli agent-os release \
  --root /tmp/my-agent-os --release /path/to/new-release

# 在复制、归档或恢复前验证逐文件清单、发行身份和状态 round-trip
PYTHONPATH=. python3 -m grapheng.cli agent-os verify-release \
  --release /path/to/new-release
```

发行目录包含运行源码、测试、fake protocol fixture、示例、`state.bundle`、兼容矩阵和恢复手册。
它明确排除凭据、worktree、运行 Artifact、prompt/input 原文、未验证结果、活动 lease、路由运行态、
publication receipt 和 controlled merge 运行态。清单能够检测内容篡改，但不是密码学签名。

## Orca 编排计划

`orca-plan` 是纯编译命令，不启动 Orca、不创建 Task、也不调用模型：

```bash
PYTHONPATH=. python3 -m grapheng.cli orca-plan \
  examples/orca_agents.json \
  --objective "在隔离工作区完成实现与复核"
```

计划把 Claude Code、Codex、Pi 分别映射到 Orca 的 `claude / codex / pi`，并保留
依赖、gate、模型、重试上限和 workspace 策略。`OrcaBackend.materialize()` 才会创建
Run、Task 和 gate；`start_worker()` 只在上层显式要求时创建 Dispatch。GE 是依赖调度、并发、
token、retry policy、gate decision 和发布的唯一所有者；Orca 是 Run/Task/Dispatch 与 worker
生命周期的唯一所有者。

隔离节点的 `worker_done.payload` 必须带结构化 outputs 和 change-set（workspace id、
base/head ref、修改文件、可选 patch、冲突）。有冲突时永远不可进入 ready；无冲突仍需
GE gate 通过。`retain=always|never|on_failure` 决定完成后调用 Orca retain 还是 release。

`OrcaCoordinator` 提供持久的自动协调循环：按依赖和并发启动就绪节点，等待完整 Delivery，处理
`worker_done / question / escalation`，显式回复问题或执行 continue/retry/fail 决策，并在每个已接受
的 worker 完成 release/retain 后才 ack。materialize、dispatch、gate、reply、stop、cleanup 和 ack
都由稳定身份 Effect receipt 保护；重启后不会重复创建 Dispatch、提交 Artifact 或清理 worker。

Delivery 和消息内容会绑定摘要，重放内容漂移、错误 task/dispatch、未知或陈旧 Dispatch 均不会污染
图状态。Orca 结果继续使用同一 Artifact Store、事件与 Reality Anchor 自动发布器；token 预留不足
只推迟并行波次，实际越界或隔离 change-set 冲突则失败关闭。

### 受控 Git 合并

需要自动合并的 Agent 节点必须显式声明 `controlled_merge.verifier` 和 `target_branch`，并使用隔离
workspace。指定验证器必须通过 `verifier_for` 精确绑定来源、作为 Reality Anchor、配置
`verified_reuse`，并复用现有命名 gate；一个验证器不能同时授权多个来源。

来源 worker 完成后，Coordinator 会冻结 change-set、来源/目标提交、修改文件和来源 Artifact 的精确
版本，临时保留 workspace。只有验证结果通过、质量达标、版本一致且 gate 已批准时，系统才会生成
带 candidate、verification 和 gate 身份的 `--no-ff` merge commit。目标漂移、脏工作区、版本或文件
清单不一致、冲突及状态篡改都会失败关闭；提交后崩溃可通过 Effect receipt 与精确提交身份恢复，
不会重复合并。候选和 receipt 可从 Coordinator 的 `merges` 快照观察，但属于运行态，不进入 Agent OS
迁移包。同步 `GraphRuntime` 会明确拒绝 `controlled_merge`，避免静默跳过合并语义。

## 计划状态与生产化边界

Phase A–E6 已全部完成，当前发行是可验证、可迁移的本地 Agent OS 基线。自动循环使用
FakeBackend 完成协议与恢复验收，本轮未调用真实模型，也未创建真实 Orca Run/Task/Dispatch。

若进入后续生产化阶段，应另立计划处理真实 Orca/真实项目灰度、长期驻留服务、跨主机状态、
多租户隔离和带信任根的发行签名；这些不属于当前 A–E6 的完成条件。任务与验收证据见
`.workflow/agent-os/plan.md` 和 `.workflow/agent-os/results.md`。
