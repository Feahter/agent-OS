# Codebase Memory 评估问题：ROI 任务清单

本清单把 [codebase-memory-assessment.md](codebase-memory-assessment.md) 的结论转换为可排期、可验收的任务。复核基线为 `HEAD 2fe75a9`；评估文档与当前源码处于同一提交。

执行状态（2026-09-15）：CM-00～CM-09、CM-11～CM-13 的本地实施与验收已完成；ROI 计划 1～3 的可离线部分也已推进，Resident 具备跨进程事件唤醒与按最近期限动态休眠。真实 Adapter/Orca canary、真实六节点 token/经济性对照按用户要求暂不执行；计划 4/5 的真实压力和样本启动门槛未满足，保持未启动。发布门禁仍为 `blocked_pending_real_adapter_canary`。

本轮离线验证：467 项测试与 74 项 subtest 通过，11 项安全 mutation 全部被目标测试杀死；高风险模块 branch coverage 均达到已冻结门槛，其中 `resident.py` 为 71.64%（门槛 69.36%）。`uv lock --check`、Ruff、mypy、compileall 和 `git diff --check` 同时通过。尚未勾选的内容仅包括真实 Agent/Orca/Luna 验收、依赖历史对照的 Resident 空闲持久写降低 80% 指标，以及未满足启动门槛的计划 4/5。

## 复核结论

| 问题 | 结论 | 当前源码证据 |
| --- | --- | --- |
| Control Plane lease 缺少跨进程 fencing | 已确认 | `control.py` 的状态更新只受实例内 `threading.Lock` 保护；`generation` 在恢复时递增，但 heartbeat、成功和失败写回没有校验 generation。 |
| 普通 Graph 写节点可能 at-least-once 重放 | 已确认 | `runtime.py` 恢复时把所有非 `COMPLETED` 节点重置为 `PENDING`；`agent_nodes.py` 在 shared workspace 中直接给 Agent 传递 `shell/edit/write` 能力，没有节点级 prepare/commit/reconcile。 |
| protected-path 校验存在崩溃窗口 | 已确认 | `engineering.py` 的 mutating effect 先由 Effect Journal 写 completed receipt，调用返回后才执行 `_assert_protected_unchanged`。 |
| `approve → queued → resident.submit` 交接不原子 | 已确认 | `tasks.py` 依次写 approval、status、queue，异常回滚不能覆盖进程在任一步骤间崩溃的情况。 |
| Graph pause 可能覆盖已完成终态 | 已确认 | `resident_jobs.py` 记录过 pause 请求后，无论 `plane.wait` 是否已经返回终态，都优先返回 `paused`。 |
| Orca indeterminate effect 缺少完整恢复入口 | 部分确认 | merge 等路径已有内部 reconcile，但 dispatch/gate/reply/cleanup 并非全部可协调恢复，公开 API 也没有统一的 inspect/reconcile/reset 操作。 |
| checkpoint/event durability 未统一 | 已确认 | checkpoint 自行实现临时文件、文件 `fsync` 和 replace，但缺目录 `fsync`；event sink 追加后没有 flush/`fsync`，两者都未复用 `_store.py` 的统一契约。 |
| CI 未锁定依赖 | 已确认但需修正文案 | 仓库已有 `uv.lock`，问题是 CI 仍用 `pip install` 重新解析依赖，而不是冻结使用 lock。 |
| 复杂度集中在少数编排函数 | 已确认 | 评估列出的 `_dispatch`、`runtime.run`、`engineering.execute`、`validation_issues` 和 `_node_metrics` 在当前提交中仍是大分支编排入口。 |

复核限制：当前会话没有可用的 Codebase Memory graph 工具，因此复杂度数值和 SCC 数量沿用评估快照；所有会影响任务排序的正确性问题均已用当前源码定向复核。

## ROI 排序方法

这里的 ROI 指“每个工程日可减少的正确性、恢复性或生产采用风险”，不是功能收入估算。排序依次考虑：

1. P1 正确性风险是否被直接关闭；
2. 影响路径和故障后果；
3. 证据置信度；
4. 实施成本、回归面和前置依赖。

`极高` 通常是 0.5–3 人日的明确修复，`高` 是 3–8 人日的核心闭环，`中` 是 1–3 人周的系统改造，`低` 表示当前阶段不应独立投入。工期是范围估算，不是承诺。

安全约束高于纯数值 ROI：在 CM-01、CM-05、CM-07 完成前，不应把高价值仓库的无人值守 shared-workspace 写操作标记为生产可用。

## 排序总表

| 排名 | ID | 任务 | 风险 | 预计投入 | ROI | 依赖/并行建议 |
| ---: | --- | --- | --- | ---: | --- | --- |
| 1 | CM-01 | 关闭 protected-path receipt 崩溃窗口 | P1 | 2–4 人日 | 极高 | 无前置；立即做 |
| 2 | CM-02 | 修复 pause 与终态竞争 | P2 | 1–2 人日 | 极高 | 可与 CM-01 并行 |
| 3 | CM-03 | 统一 checkpoint 与 event durability | P2 | 2–3 人日 | 极高 | 为 outbox 提供存储地基 |
| 4 | CM-04 | 让 CI 冻结消费现有 `uv.lock` | 生产证据 | 1–2 人日 | 极高 | 独立并行 |
| 5 | CM-05 | 为 LocalControlPlane 建立跨进程 fencing | P1 | 5–8 人日 | 高 | CM-07 的 ownership 前置 |
| 6 | CM-06 | 用 durable outbox 原子化任务与队列交接 | P2 | 5–8 人日 | 高 | 建议复用 CM-03 存储原语 |
| 7 | CM-07 | 统一普通 Graph 写副作用协议 | P1 | 10–15 人日 | 中高 | 依赖 CM-05 的 token 语义 |
| 8 | CM-08 | 补齐 Orca indeterminate effect 恢复入口 | P2 | 4–7 人日 | 中高 | 复用 CM-07 effect contract |
| 9 | CM-09 | 建立高风险模块故障注入与 coverage 门槛 | 生产证据 | 5–10 人日 | 中 | CM-01～CM-08 的验收层 |
| 10 | CM-10 | 真实 Agent/Orca crash 与长时 soak | 生产证据 | 5–10 人日 + 运行时间 | 中 | 在正确性闭环后执行 |
| 11 | CM-11 | 冻结最小公共 API 和持久化 schema | 兼容性 | 5–8 人日 | 中 | 先稳定状态机语义 |
| 12 | CM-12 | 补威胁模型、SBOM 和签名发布 | 供应链 | 5–10 人日 | 中 | 生产试点前门禁 |
| 13 | CM-13 | 按状态机 seam 拆分复杂度热点 | 可维护性 | 10–20 人日 | 低（当前） | 只随 CM-05～CM-08 渐进实施 |

另有一个不计入根因修复排名的当日止损项：CM-00。它应立即完成，但不能用来替代 CM-01、CM-05、CM-07。

## 任务明细

### CM-00：收窄 crash-safe 与副作用不重放声明

目标：在根因修复完成前，让用户不会把局部 Effect Receipt 保障理解为所有 shared-workspace 写操作都 exactly-once。

- [x] 在 README 的 Recovery、Effect Receipt 和限制章节列出已覆盖与未覆盖路径。
- [x] 明确普通 Graph shared-workspace Agent 写操作当前为 at-least-once 风险，不承诺崩溃后无重放。
- [x] 明确 indeterminate receipt 的默认行为是 fail-closed，需要 reconcile，而不是自动成功或自动重试。
- [x] 将“高价值仓库无人值守写操作”标记为 CM-01、CM-05、CM-07 完成前的非支持场景。

验收：README 中不再存在无适用边界的“crash-safe continuation”或“mutating calls 不重放”表述；文档仍准确描述受控 Git merge 等已有保障。

### CM-01：关闭 protected-path receipt 崩溃窗口

目标：只有 protected-path 和只读后置条件通过后，mutating effect 才能进入 completed。

- [x] 把 approved protected snapshot、workspace fingerprint 和 effect identity 持久化到 execute 前的 intent/prepare 记录。
- [x] 将 `_assert_protected_unchanged` 与只读 workspace 校验纳入 effect 的 commit/postcondition，而不是 completed receipt 之后的普通调用。
- [x] 恢复时只使用持久化的 pre-effect snapshot 做 reconcile，禁止从已被修改的 workspace 重建 baseline。
- [x] protected path 已改变时保留可审计证据并 fail-closed，不把 receipt 标成 completed。
- [x] 注入三个崩溃点：Agent 返回前、返回后/校验前、校验后/receipt commit 前。

验收：任一崩溃点恢复后都不能接受越权修改；移除 postcondition 后，目标测试必须精确变红。

### CM-02：修复 pause 与终态竞争

目标：真实执行终态优先于迟到的 pause 投影。

- [x] `plane.wait` 返回 terminal snapshot 后，忽略未被执行器确认的 pause 请求。
- [x] 区分 `pause_requested`、`pause_acknowledged` 和 terminal，不以本地布尔值代替执行确认。
- [x] 覆盖 pause 请求与 succeeded/failed/cancelled 同时发生的竞争测试。
- [x] 状态中心和队列投影以同一状态转换规则生成摘要。

验收：底层已完成时 Resident 不再报告 paused；只有在安全检查点被确认暂停时才进入 paused。

### CM-03：统一 checkpoint 与 event durability

目标：让核心恢复状态共享 `_store.py` 的落盘契约。

- [x] `CheckpointStore` 复用 `atomic_json_write`，包含文件和目录 `fsync`。
- [x] `JsonlEventSink` 复用 durable append，并定义首次创建文件时的目录持久化行为。
- [x] 明确 checkpoint 与 event 的提交顺序，以及恢复时如何处理“checkpoint 已提交但 event 缺失”和尾部半行。
- [x] 增加 replace、文件 `fsync`、目录 `fsync`、event append 失败的故障注入。
- [x] 保持旧 checkpoint/event 文件的向后读取兼容。

验收：所有核心 checkpoint/event 写入都只通过统一存储原语；故障后要么恢复到旧完整状态，要么读取新完整状态，不接受截断 JSON。

### CM-04：让 CI 冻结消费现有 `uv.lock`

目标：CI、发布构建和本地复现使用同一依赖解析结果。

- [x] 选择与 Python 3.9/3.12/3.13 matrix 兼容的 frozen 安装方式。
- [x] CI 在 lock 与 `pyproject.toml` 不一致时失败，不静默重新解析。
- [x] wheel/sdist 烟测继续验证发行包本身，不把源码 checkout 泄漏进环境。
- [x] 记录 lock 更新流程，避免开发者手改 lock。

验收：同一提交的 CI 依赖版本可复现；故意修改依赖声明但不更新 lock 时，目标 job 精确失败。

### CM-05：为 LocalControlPlane 建立跨进程 fencing

目标：同一个 run 在任意时刻最多只有一个有效 owner，陈旧 owner 不能续租、派发新 effect 或写回终态。

- [x] 将 read-check-write claim 改为跨进程原子事务或受文件锁保护的 compare-and-swap。
- [x] 定义不可伪造的 `LeaseToken(run_id, owner_id, generation)`，claim/takeover 原子返回 token。
- [x] heartbeat、cancel acknowledgment、node/effect 派发、checkpoint publication、success/failure settle 全部携带并校验 token。
- [x] takeover 只在 lease 过期且 generation 单调递增时成功。
- [x] 陈旧 owner 检测到 token 失效后停止派发；已经在途的外部副作用交给 CM-07 reconcile。
- [x] 增加双进程同时 claim、旧 owner 延迟 heartbeat、旧 owner 延迟 success/failure、takeover 后旧 owner 派发的测试。

验收：多进程压力测试中无双 owner、无陈旧 settle、无 generation 回退；删除 token 校验后对应测试必须变红。

### CM-06：用 durable outbox 原子化任务与队列交接

目标：进程可在任一写入点崩溃，但 approval、task state、queue job 最终仍能收敛且不重复执行。

- [x] 在 task owner 内持久化带幂等键的 enqueue intent，再由 Resident 消费/确认。
- [x] 用状态机替换“写三份文件，异常时反向 unlink/覆盖”的补偿式流程。
- [x] submit 重试以 task ID + approval digest 去重。
- [x] 为 pause/cancel 建立 control intent → execution acknowledgment，重启后继续投递未确认 intent。
- [x] 增加每个提交边界的 kill/restart 测试，以及重复消费、乱序 acknowledgment 测试。
- [x] 与 [04-safe-resident-concurrency.md](roi-iteration-plans/04-safe-resident-concurrency.md) 共享 outbox/lease 语义，不重复建设第二套队列事务。

验收：不存在 approved 但永远不可发现的孤儿任务；重复恢复不产生重复 queue item 或第二次副作用。

### CM-07：统一普通 Graph 写副作用协议

目标：普通 Graph 写节点具备显式 `prepare → execute → commit/reconcile` 契约，不再把非完成节点一律盲目重放。

- [x] 在 Node/Agent 契约中声明 `read_only`、`verified_idempotent` 或 `reconcilable` effect 类型；未知类型 fail-closed。
- [x] 写操作默认进入隔离 workspace；shared workspace 只允许只读或显式验证幂等、可协调的操作。
- [x] execute 前落盘 effect intent、输入 digest、lease token 和 workspace identity。
- [x] execute 后提交输出、Artifact、checkpoint 和 receipt 时定义唯一顺序。
- [x] 恢复先 reconcile，再决定 completed、retryable 或 indeterminate；禁止仅凭节点非 `COMPLETED` 就重跑。
- [x] 对 shell/edit/write、Artifact 写入、受控 Git merge 建立统一但可扩展的 effect 接口。
- [x] 覆盖每个持久化边界、进程 kill、超时、返回丢失和 reconcile 失败。

验收：故障矩阵中重复副作用为零；无法证明外部效果是否发生时保持 indeterminate 并要求恢复动作，不静默重放。

### CM-08：补齐 Orca indeterminate effect 恢复入口

目标：所有 Orca 外部 effect 都能通过一致的公开流程检查、协调或由授权操作者重置。

- [x] 盘点 dispatch、gate、reply、delivery、merge、cleanup 的 effect identity 和可观测终态。
- [x] 能查询外部系统的 effect 实现 reconcile；不能查询的明确保持 indeterminate。
- [x] 提供稳定的 inspect/reconcile API 与 CLI 投影，不要求用户直接编辑 receipt 文件。
- [x] reset 必须绑定 actor、原因、旧 receipt digest 和审计事件；不能覆盖已确认 completed 的 effect。
- [x] 覆盖进程在请求发送前、发送后/响应前、响应后/receipt 前崩溃的测试。

验收：每类 Orca effect 都有“自动协调”或“明确的人工恢复”路径；恢复动作本身幂等且可审计。

### CM-09：建立高风险模块故障注入与 coverage 门槛

目标：把正确性闭环变成持续门禁，而不是一次性修复。

- [x] CI 开启 branch coverage，仅先对 control/runtime/engineering/resident/coordinator 的新增和高风险分支设阈值。
- [x] 建立可复用 crash-point harness，支持子进程在持久化边界被终止。
- [x] 覆盖 lease takeover、effect commit、outbox ack、protected-path postcondition 和目录 `fsync`。
- [x] 保留 mutation check：移除 fencing/postcondition/幂等校验时，目标测试必须失败。
- [x] coverage 阈值分阶段提升，不用排除规则隐藏难测分支。

验收：上述五类不变量均有能捕获回归的测试；CI 输出分支覆盖率并阻止阈值下降。

### CM-10：真实 Agent/Orca crash 与长时 soak

目标：补足离线单测无法证明的进程、CLI 和外部工具行为证据。

当前状态：确定性 soak 已通过；待执行报告已固定 Adapter/Orca 协议和七个 crash point，并记录本机安全版本探针。Claude Code 2.1.272 已完成无模型协议检查并纳入精确版本证据；用户已明确要求跳过 OpenCode，门禁以带操作人、时间、原因和审批引用的单项豁免记录该范围变化，其他 Adapter 不可豁免。preflight 当前仍有三项阻塞：工作树未提交、缺少 canary 操作人、缺少模型调用费用授权；发布门禁保持 `blocked_pending_real_adapter_canary`。

- [ ] 固定 Codex、Claude Code、Pi、Orca 的受控测试场景和版本证据；OpenCode 按用户指示以审计豁免跳过。
- [ ] 在 Agent 写入前后、输出解析前后、Orca 消息/merge/cleanup 边界做进程 kill。
- [x] 运行至少覆盖 lease 过期与多次 checkpoint 的长时 soak，并记录重复 effect、恢复耗时、人工介入和 terminal 分歧。
- [x] 对不支持硬预算的 Adapter 继续 fail-closed，不以 soak 配置绕开能力检查。
- [x] 形成可重复执行的报告模板和发布门槛。

验收：连续受控 canary 无重复副作用、双终态或无法解释的状态漂移；失败样本保留可定位证据。

### CM-11：冻结最小公共 API 和持久化 schema

目标：在状态机语义稳定后，阻止恢复格式和公开调用约定被无意破坏。

- [x] 只对外部 JSON、状态文件和跨模块公共边界引入 `TypedDict`/数据类，不顺手改内部宽类型。
- [x] 为 run/task/receipt/checkpoint/queue schema 定义版本、迁移方向和未知版本 fail-closed 规则。
- [x] 冻结最小 Python/CLI API，增加签名、JSON schema 和向后兼容 fixture 测试。
- [x] 明确 pre-alpha 中仍允许变化的非公共接口。

验收：旧版本 fixture 可迁移，新未知版本被拒绝；公共签名和 schema 的破坏性变更必须显式更新契约。

### CM-12：补威胁模型、SBOM 和签名发布

目标：为真实团队试点提供可审查的安全与供应链证据。

- [x] 威胁模型覆盖本地凭据、恶意仓库指令、workspace 越权、Adapter 子进程、receipt 篡改和多用户边界。
- [x] 明确当前不是 OS 沙箱、网络隔离或凭据保险库，并记录剩余风险。
- [x] 发布产物生成 SBOM，并与 wheel/sdist 的来源提交关联。
- [x] 建立签名发布和验签说明；CI 权限最小化，发布身份与普通测试分离。
- [x] 在 SECURITY.md 中定义支持版本、报告渠道和响应边界。

验收：试点团队能验证产物来源、依赖清单和剩余风险；未签名或来源不明的产物不会被标记为正式发布。

### CM-13：按状态机 seam 拆分复杂度热点

目标：随着正确性任务落地，把状态迁移规则从巨型条件分支收进深模块，而不是单纯追求短函数。

当前状态：已沿 Resident 控制/终态投影落地表驱动状态机，并随 CM-05～CM-08 的契约稳定完成编排 seam 拆分。CLI 按命令族路由，Runtime 按运行态/调度/结算拆分，Engineering 按实现/检查评审/修复拆分，Economics 按事件边界拆分，Adapter 将进程编排与各协议解析分离；未改变公共 CLI 或 `ResidentCoordinator` 接口。

- [x] CM-05 落地 `leases.py`，CM-07 落地 `effects.py`，CM-06 落地 `durable_outbox.py`。
- [x] 从现有不变量测试提炼内部 `state_machine.py`，由表驱动合法迁移和终态优先级。
- [x] 在契约稳定后再拆 `cli._dispatch`、`runtime.run`、`engineering.execute`、`economics._node_metrics` 和 adapters。
- [x] 保持现有 CLI 与 `ResidentCoordinator` 小接口，不把内部状态机泄漏给调用方。
- [x] 每次只沿一个已验证 seam 迁移，不做跨模块大爆炸重写。

验收：状态迁移拥有单一事实源；复杂度下降是结果，不以 LOC 或文件数量作为完成标准。

## 推荐执行波次

### Wave 0：当天止损与复现地基

- [x] CM-00 收窄声明。
- [x] 启动 CM-01 的崩溃复现测试。
- [x] CM-04 冻结 CI 依赖。

### Wave 1：高 ROI 正确性修复

- [x] CM-01 protected-path postcondition。
- [x] CM-02 terminal/pause 竞争。
- [x] CM-03 durability 统一。

### Wave 2：ownership 与事务交接

- [x] CM-05 Control Plane fencing。
- [x] CM-06 durable outbox。
- [x] 将 [Resident 安全多任务并发](roi-iteration-plans/04-safe-resident-concurrency.md) 保持在门槛之后；ownership 已闭环，但尚无真实队列压力或用户并发需求证据，因此仍不扩大顶层并发。

### Wave 3：副作用闭环

- [x] CM-07 普通 Graph effect protocol。
- [x] CM-08 Orca 恢复入口。
- [x] 完成后再解除高价值仓库无人值守写操作限制。

### Wave 4：生产证据与可维护性

- [x] CM-09 建立离线持续证据。
- [ ] CM-10 补齐真实 Adapter/Orca canary 证据。
- [x] CM-11、CM-12 作为正式试点门禁。
- [x] CM-13 已随前述正确性任务沿已验证 seam 渐进拆分，未发起跨模块重写。

## 组合级完成定义

- [x] 三项 P1 都有故障注入测试和反向 mutation check。
- [x] 同一 run 不存在双 owner、陈旧 settle 或陈旧 effect 派发。
- [x] 任何无法判断是否发生的副作用都进入 indeterminate，不自动重放。
- [x] protected path 越权修改在所有崩溃点都不能被恢复流程接受。
- [x] approval、queue、pause/cancel 的跨 owner 状态最终收敛且可审计。
- [x] checkpoint/event/outbox 采用统一 durability contract。
- [x] README 的能力声明与实际覆盖范围一致。
- [x] 在真实 Adapter canary 形成证据前，项目仍标记为受控试点而非高风险生产可用。
