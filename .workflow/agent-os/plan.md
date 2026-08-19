# Agent OS 落地计划

## 已完成：Phase A — 本地异构纵向切片

### A1：统一执行 seam
[deps: foundation] [status: completed]

- `AgentRequest / AgentResult / ExecutorCapabilities`。
- `ExecutorRegistry` 按显式执行器、能力和工具确定性路由。
- 从请求自动推导 structured output、模型、工具和费用能力要求。

### A2：首批 Adapter
[deps: A1] [status: completed]

- Claude Code：JSON Schema、单结果 JSON、工具白名单、模型和费用上限。
- Pi Agent：JSONL 事件、最终消息、工具白名单、模型和用量解析。
- 统一超时、进程错误、协议错误和输出契约错误。

### A3：GraphRuntime 绑定
[deps: A1, A2] [status: completed]

- `kind=agent` 节点配置、Artifact 输入和严格结构化输出。
- token 进入图预算；executor、cost、session 写入节点完成事件。
- 静态拒绝共享工作区中的无序可写 Agent。

## 已完成：Phase B — 完整本地执行矩阵

### B1：Codex Adapter
[deps: A1] [status: completed]

- 核对 Codex CLI/任务协议，映射结构化输出、session、工具和用量。
- 通过与 Claude/Pi 相同的 Executor 契约测试。

### B2：Orca 编排后端
[deps: A1] [status: completed]

- 将 GraphSpec 编译为 Orca Run/Task/Dispatch/deps/gate。
- 将 worker_done、escalation 和 task event 归一化为 AgentResult/GraphEvent。
- 明确 GE 与 Orca 的调度所有权，禁止双重重试和双重 gate。

### B3：隔离工作区
[deps: B1, B2] [status: completed]

- 为可写节点创建独立 worktree/workspace。
- 定义变更集 Artifact、合并 gate 和冲突处理。
- 加入清理、保留和失败恢复策略。

### B4：契约与验收
[deps: B1, B2, B3] [status: completed]

- Codex、Orca、workspace、change-set 使用假 CLI/协议事件测试，不触发真实模型。
- `orca-plan` 提供纯编译入口；真实物化保持显式调用。
- Orca 运行时离线时不自动启动桌面应用。

## Phase C — OS 生命周期与控制面

### C1：异步执行协议
[deps: B4] [status: completed]

- submit、event stream、cancel、heartbeat、lease、resume。
- 持久任务队列和崩溃接管；副作用节点使用幂等键和 effect receipt。
- `LocalControlPlane` 以小型接口隐藏线程、原子状态和 lease 细节。
- 取消只阻止新节点调度；运行中节点采用协作式结束。

### C2：策略与路由
[deps: C1] [status: completed]

- 按能力、费用、延迟、健康度和数据权限选择执行器。
- 图级美元预算、供应商限流和熔断。
- `ExecutorRegistry` 保持唯一执行 seam；路由不拥有重试。
- 费用和未知费用的保守结算写入事件、checkpoint 与控制面结果。

### C3：操作界面
[deps: C1, C2] [status: completed]

- 运行图、Artifact 血缘、审批收件箱、失败回放和费用视图。
- `OperationsConsole` 从持久状态生成自包含、无第三方依赖的本地 HTML。
- `ApprovalInbox` 持久化 allow/deny，并生成恢复运行使用的 gate policy。
- 控制台默认不展示 Artifact 内容，只展示生产者、消费者、版本和校验和。

## Phase D — RSI 可控自进化

### D1：脱敏观测与质量反馈
[deps: C2, C3] [status: completed]

- 自动记录执行器、成功、费用、延迟和数据级别，不记录 prompt、输入或 Artifact 内容。
- Reality Anchor、验证器和人工可按 `task_id` 提交 0–1 质量反馈。
- 把任务质量置于费用和延迟之前，避免把“没报错”误判成“做得好”。

### D2：策略生命周期
[deps: D1] [status: completed]

- 聚合历史形成版本化候选，执行样本量、成功率、质量、费用和延迟评估。
- 必须人工批准才能激活；默认 10% 确定性灰度，可立即回滚。
- 学习策略只能调整软排序，不能放宽数据权限、预算、限流和熔断。

### D3.1：任务条件化学习
[deps: D2] [status: completed]

- 按任务类型、工具集和模型族聚合质量、费用、延迟和成功率。
- 条件样本不足时回退到全局估计，避免冷启动和稀疏样本误导路由。
- 质量门槛同时要求最小 Reality Anchor/验证反馈样本，禁止把“未知质量”当成达标。

### D3.2：提示与图拓扑候选
[deps: D3.1] [status: completed]

- 从重复失败模式生成追加式提示或保守拓扑候选，不在运行中直接改写代码。
- 冻结回归集以 fixture digest 固定身份；评估要求 Reality Anchor、质量、费用和延迟同时达标。
- 人工批准后默认 10% 确定性灰度；canary 任一指标越界会自动停用并恢复上一版本。
- 本地 `agent-run`、Orca 编译与操作控制台共享同一激活状态。

### D3.3：安全复用与去重
[deps: D3.2] [status: completed]

- 仅复用已验证、策略兼容、来源可追溯且未过期的 Artifact。
- 缓存键不保存 prompt/输入原文，敏感级别和租户边界不可跨越。
- 并发重复任务合并为单次执行，记录命中、节省费用与失效原因。
- 未验证结果只允许当前进程内合并；持久化必须显式提供 Reality Anchor 来源。
- 统一 Agent OS 根目录、严格文件白名单、版本清单和无覆盖导入。
- 迁移包排除凭据、worktree、运行 Artifact、原始 prompt/input 与未验证结果。

### D4.1：Reality Anchor 自动发布
[deps: D3.3] [status: completed]

- Agent 执行收据绑定精确输入/输出 Artifact 版本，验证器不能通过 `latest` 猜测历史结果。
- GraphSpec 通过 `verified_reuse` 显式声明通过字段、质量字段和最低质量，不从任意真值推断。
- 只有读取目标 Agent 全部输出的 Reality Anchor 才能发布；敏感数据和中途被覆盖的输出在静态阶段拒绝。
- 发布前先持久化无 prompt/input 原文的 ready receipt；恢复时幂等补发，不重复执行 Agent。
- 本地 `agent-run`、`LocalControlPlane`、JSONL 审计和操作控制台共享发布状态。

### D4.2：跨进程 Single-flight
[deps: D4.1] [status: completed]

- 用共享租约把不同 Codex、Claude Code、Pi 进程中的相同请求收束为一个 leader。
- 文件锁保护租约获取和 fencing token；heartbeat 续租，过期 owner 可由一个等待者接管。
- 等待者先登记再接收最小化结果；失败只传播类型和摘要，收据有 schema、checksum 和短 TTL。
- 未验证结果只进入不导出的运行态交付目录，不进入持久复用库；敏感请求继续绕过。
- 复用键纳入费用上限，leader 才消耗供应商限流；共享租约不放宽权限、预算或熔断。
- 多进程事件日志使用文件锁与原子追加；Agent OS 状态和控制台显示活动/过期/损坏租约。

### D4.3：迁移 Schema 转换器
[deps: D4.1] [status: completed]

- 为 Agent OS bundle 建立逐版本、可审计的 schema 转换注册表。
- 导入先在 staging 中转换、校验和重建与完整验证，再原子写入空目标。
- 不迁移运行期 publication receipt、凭据、工作区、原始 prompt/input 或未验证结果。
- 根状态 schema 与 bundle schema 分离；当前根状态保持 v1，导出包升级为 v2。
- v1→v2 会为旧复用键补入费用边界、重算 key/checksum 并安全重命名条目。
- 目录级提交失败时恢复原空根目录；活动/损坏 single-flight 状态禁止被导入覆盖。
- 迁移审计只记录版本、转换器和变更路径，不记录源机器绝对路径。

## Phase E — 无人值守协调与长期治理

### E1：Orca Coordinator Loop
[deps: D4.3, B2] [status: completed]

- 把 Orca Task、worker、question、escalation、retry 和 gate 事件接入统一控制面状态机。
- 保持 GE 独占图重试与 gate 决策，Orca 独占 worker 生命周期，禁止双重调度。
- 将 Orca worker 的 Reality Anchor 结果接入现有自动发布与 Outcome receipt seam。
- 以持久状态、文件锁和 Effect receipt 恢复 materialize、dispatch、reply、cleanup 与 Delivery ack。
- 整批 Delivery 全部处理、问题/升级已解决且 worker 已 release/retain 后才确认；内容漂移与陈旧 Dispatch 拒绝。
- token 预留只推迟并行波次，实际越界失败关闭；隔离 change-set 冲突不得提交 Artifact。

### E2：跨进程路由治理状态
[deps: E1, C2] [status: completed]

- 持久化供应商健康、限流、熔断与冷却状态，并提供进程安全的原子更新。
- 重启后保守恢复，不允许陈旧健康状态绕过权限、预算或供应商保护。
- JSON 状态使用 schema、generation、checksum、原子替换和 POSIX 文件锁；损坏或未来版本失败关闭。
- 冷却后只允许一个带超时租约的 half-open probe；owner 消失后可安全接管，成功关闭、失败重开。
- 系统时钟回退不提前释放额度或熔断；更严格的新策略和权限、费用、延迟硬约束立即生效。
- `routing/` 运行态进入 Agent OS 状态投影但明确排除迁移；活动或损坏状态阻止导入覆盖。

### E3：受控变更集合并
[deps: E1, B3] [status: completed]

- GraphSpec 以 `controlled_merge.verifier / target_branch` 显式声明合并，不从 prompt 推断。
- 合并源必须使用隔离工作区；验证器必须精确指向来源、是 Reality Anchor、配置 `verified_reuse` 并复用现有 gate。
- 变更完成时冻结 change-set、Git 提交和来源 Artifact 版本；验证完成且 gate 通过后才执行 `--no-ff` 可审计合并。
- 冲突、脏工作区、目标分支漂移、文件清单或验证版本不匹配时失败关闭，不做隐式覆盖。
- merge 由可协调 Effect receipt 保护；崩溃后按精确父提交和审计消息恢复，不重复执行。
- 候选、receipt 与 workspace 清理进入 Coordinator 运行态投影，但明确不进入 Agent OS 迁移包。

### E4：实时操作与审批面
[deps: E1, C3] [status: completed]

- `OperationsAPI` 以 revision 和 event cursor 提供条件快照与增量事件，状态未变时不重复传输完整快照。
- 本地实时操作台展示任务、费用、Artifact、single-flight 和 publication 状态。
- 界面内审批仍唯一写入同一 `ApprovalInbox`，跨进程写入使用文件锁，不建立第二套决策来源。
- HTTP Adapter 仅允许 loopback，审批使用随机 action token，并设置 no-store、CSP 和防嵌套响应头。

### E5：可移植发行与自诊断
[deps: E2, E3, E4] [status: completed]

- `AgentOSDistribution` 统一提供安装/文件系统/状态检查、版本兼容矩阵、迁移演练和备份恢复验证。
- Claude Code、Codex、Pi 与 Orca 只通过 `--help / --version` 检查协议，不调用模型或创建编排对象。
- 当前 bundle 与旧 v1 bundle 都经过 staging 迁移和双重 round-trip 稳定性验证，演练不修改源状态。
- 自包含发行目录收纳运行源码、测试、示例、兼容矩阵、恢复手册和干净状态包，不携带凭据或运行态。
- 发行清单逐文件绑定 SHA-256、大小和 release identity；拒绝覆盖既有目标，并可检测身份或内容篡改。

### E6：标准工程循环
[deps: E5, D1, D3.3] [status: completed]

- 将“探索 → 计划 → 审批 → 小步实现 → 自动检查 → 独立审查 → 修复复审”收束为
  `EngineeringWorkflow.prepare / execute` 两个稳定入口，不改变 GraphSpec 的 DAG 语义。
- `ProjectPolicy` 统一项目规则文件、参数数组检查命令、保护路径、角色执行器和循环/调用/时长上限。
- 计划 Artifact 绑定策略摘要、项目指令摘要和工作区基线摘要；篡改、审批摘要不匹配或批准后漂移均失败关闭。
- 准备阶段 token、费用和调用数进入计划与最终报告；调用上限覆盖全部角色，单次超时不超过剩余总时长。
- 探索、计划、审查只授予 `read`；实现、修复使用写工具并由 Effect receipt 保护，不确定结果禁止盲目重放。
- 检查失败或独立审查 finding 进入有限修复；通过必须同时具备确定性检查与无 finding 审查，形成 Reality Anchor 报告。
- 写调用禁止复用；只读调用纳入工作区指纹后保留安全复用与 single-flight，避免以降本破坏工作区副作用。
- 质量分回写最近一次实现/修复任务，角色通过 `engineering.explore / plan / implement / review / repair`
  进入既有 RSI 条件化路由，不扩张 Agent OS bundle schema。
- CLI 提供 `engineer init / plan / run / status / ship`；`run` 强制绑定计划摘要，`ship` 只允许交互确认。
- 单次 `task-dir` 不得与项目工作区或 Agent OS 根目录相互嵌套，也不得复用于新任务；普通仓库与链接
  worktree 的 Git 指针、HEAD、index、config、refs 均纳入保护快照。

## Phase F — 0.1 用户旅程收束

### F1：首次设置与可执行诊断
[deps: E5, E6] [status: completed]

- `agent-os setup` 一次完成本地可迁移状态初始化、运行环境检查和 Agent 自动发现。
- 诊断区分 blocked、needs agent、ready with warnings 与 ready，不再用单一 healthy 掩盖可用性。
- 非通过项生成带优先级、稳定 action id 和参数数组复跑命令的结构化修复建议。
- 默认输出面向人的摘要，`--json` 保留版本化机器契约；两种输出共享同一诊断事实。
- Setup 只执行本地文件系统与 help/version 探测，明确记录 0 次模型调用，不创建 Orca 对象。
- 全新机器安装演练和真实 Agent/Orca 灰度仍作为外部证据门槛，不由离线测试替代。

### F2：扩展本地 Agent 发现
[deps: F1] [status: completed]

- Setup 增加 OpenCode、OpenClaw、Hermes Agent、Aider、Gemini CLI 与 GitHub Copilot CLI 的零模型发现。
- 复用 `AgentOSDistribution` 诊断 seam，不建立第二套发现控制面。
- 严格分离 discovery-only inventory 与已认证 Adapter；仅发现的工具不得进入 `ready_executors`。
- 只执行 `--help / --version`，记录安装路径、版本状态和接入状态；未安装不降低健康度。
- 版本未知、help 失败或探测异常均保持未认证，并给出可机器读取的可选接入建议。
- 下一阶段按用户价值、协议稳定性和维护成本排序，实现首个新增真实 Adapter 与公开一致性套件。

### F3：公共 Adapter Kit 与 OpenCode 执行器
[deps: F2, E2] [status: completed]

- 将既有私有 CLI 基类深化为公共 `CliAgentAdapter`，集中提供参数数组执行、环境隔离、超时、进程故障和工具映射，不建立第二套 `AgentExecutor` 接口。
- OpenCode 使用官方 `opencode run --format json --pure` 非交互协议，最后一个文本事件形成结构化结果，逐步事件聚合 Token 与费用，会话 ID 归一化但产品会话不进入迁移包。
- 工具权限通过 `OPENCODE_PERMISSION` 默认拒绝后按 `read / shell / edit / write` 最小放行；禁用项目配置、外部插件和交互提问，不使用危险的 `--auto`。
- OpenCode 不宣称 CLI 未提供的硬费用上限；实际费用、延迟、成功与故障仍通过既有 Registry → PolicyRouter seam 进入脱敏 RSI 观测。
- 以官方 v1.18.18 Darwin arm64 发布二进制完成 `--version` 与 `run --help` 只读验收，并将版本、平台、协议与制品摘要写入兼容证据。
- 将 OpenCode 从 discovery-only 提升到认证 Adapter；OpenClaw、Hermes、Aider、Gemini CLI 与 GitHub Copilot CLI 继续保持仅发现和失败关闭。
- 下一阶段先在内置执行器之外验证公共 Kit 的兼容面，再按用户价值、协议稳定性、权限控制与维护成本选择 OpenClaw 或 Hermes 作为下一项 Adapter。
