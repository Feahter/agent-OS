# Agent OS Phase A–F1 验收结果

## 已交付

- 一个小型统一执行接口和三个真实 Adapter：Codex、Claude Code、Pi Agent。
- 本地能力发现命令与 `agent-run` 图运行入口。
- GraphSpec Agent 配置、能力/工具路由、严格输出契约和 token 结算。
- executor、cost、session 的节点事件审计。
- 共享工作区竞态保护和费用能力防静默降级。
- Orca Run/Task/deps/gate 编译、显式 Dispatch、worker 清理/保留契约。
- `worker_done / escalation / question` 到统一结果和 GraphEvent 的归一化。
- 隔离 worktree change-set、合并 gate 与冲突判定。
- 可持久恢复的本地控制面、协作取消、heartbeat、lease 和事件游标。
- Effect receipt 的 completed/indeterminate 状态与幂等重放保护。
- 能力、费用、延迟、健康度、数据权限的确定性执行器路由。
- 供应商级限流、熔断和冷却恢复；图策略继续独占重试权。
- 图级美元预算准入、保守结算、事件审计和 checkpoint 恢复。
- 自包含操作控制台：运行图、Artifact 血缘、审批、失败回放与费用。
- 审批收件箱的持久决策和可直接用于 resume 的 gate policy。
- 基于 revision 与 event cursor 的实时操作投影；状态未变时不重传完整快照。
- loopback-only 实时网页与随机 action token；界面审批仍唯一写入原 `ApprovalInbox`。
- 审批写入使用 POSIX 文件锁，并对 run id、未知字段、事件游标、端口和非回环绑定失败关闭。
- Prompt-free RSI 遥测、Reality Anchor 质量反馈和版本化策略候选。
- 评估、人工批准、10% 灰度激活和一键回滚的策略生命周期。
- 学习结果接入统一路由，但不能覆盖权限、预算、限流与熔断硬约束。
- 任务类型、工具集、模型族条件化学习；局部样本不足时安全回退全局策略。
- 质量分先作为准入门槛，达标候选按费用、延迟排序；未知质量不能获得学习优先级。
- 重复失败模式到提示追加/依赖边候选的确定性生成，低频噪声不会触发。
- 不可变 Reality Anchor 回归集、逐例覆盖、质量/费用/延迟联合准入。
- 候选变更摘要绑定、人工批准、确定性灰度、canary 越界自动回滚。
- 激活候选统一接入本地 Agent、Orca 编译和控制台状态投影。
- 已验证结果按语义摘要、执行器、模型、工具、输出契约、分类和租户范围精确复用。
- `confidential / restricted` 默认绕过；TTL、内容校验和或策略不匹配均安全 miss。
- 同进程重复请求只执行一次，leader 失败一致传播，未验证结果不会进入持久缓存。
- 命中以 0 token/0 费用结算，并记录命中、合并、绕过、失效原因和累计节省。
- `AgentOS` 统一收束 learning、optimization、reuse、approvals、routing，旧路径参数保持兼容。
- 迁移包使用严格白名单、逐文件 SHA-256、schema 版本、来源重验和空目标导入。
- 未管理文件、符号链接、路径穿越、损坏内容和未来 schema 均被拒绝。
- Reality Anchor 通过显式 `verified_reuse` 契约自动发布已验证 Agent 结果。
- 发布候选绑定 Agent 请求及输入/输出 Artifact 的准确版本和校验和，不读取 `latest` 猜历史版本。
- ready receipt 不保存 prompt/input 原文；发布失败在恢复时补发，已完成 Agent 不会重复执行。
- 相同发布副作用具有幂等语义；敏感来源、低质量、验证失败和版本不匹配均不会进入复用库。
- `agent-run`、`LocalControlPlane`、结构化事件与控制台共享发布状态。
- Codex、Claude Code、Pi 经统一复用 seam 共享跨进程 single-flight，不需要 Adapter 各自协调。
- 共享 lease 支持 heartbeat 续租、过期 owner 接管、fencing token 和登记式结果交付。
- 跨进程 follower 以 0 token/0 费用接收结果；leader 失败只传播错误类型和摘要，不落原始错误文本。
- 运行态收据带 schema、checksum 和短 TTL；prompt/input、raw response、session 不写入收据。
- 数据级别、租户范围、执行器、模型、工具、输出契约和费用上限共同决定 flight 身份。
- 活动租约与未验证交付状态不进入 Agent OS 迁移包，控制台可查看租约健康投影。
- Agent OS 根状态 schema 与可移植 bundle schema 已分离；根状态保持 v1，bundle 当前为 v2。
- bundle schema 使用连续、无歧义的转换注册表；当前实现真实的 v1→v2 数据转换。
- 旧 verified reuse 条目会补入 `max_cost_usd` 边界并重算 key、checksum 与文件名，不只修改版本号。
- 导入先校验原包，再在同文件系统 staging 中转换、重建清单和校验和，并完整重验 RSI、优化、复用和审批状态。
- 最终以目录为单位提交；提交失败恢复原空根目录，转换失败不会留下部分状态。
- 活动或损坏 single-flight 状态阻止导入；publication receipt、lease、凭据和未验证结果仍不可迁移。
- 持久迁移审计记录源/目标版本、转换器和变更路径，但不保存源机器绝对路径。
- `OrcaCoordinator` 已形成独立持久状态机；GE 独占依赖调度、并发、token、retry、gate 和发布决策，Orca 独占 Run/Task/Dispatch/worker 生命周期。
- 工程任务、高级 Graph 和 Orca 已收束到同一个本地常驻优先级队列；队列只保存状态定位符和调度控制，不复制三个执行模块的事实状态。
- `agent-os center` 只读投影工程任务、高级 Graph 和 Orca 的事实状态，统一展示关注项、运行状态、费用与 Token；显示上限不影响全量统计。
- 本地桌面通知覆盖等待、暂停和终态，使用系统命令参数数组、持久去重和故障隔离；通知日志属于运行态，不进入 RSI 迁移包。
- 高级 Graph 可先由 `LocalControlPlane.prepare` 持久化，再由常驻进程启动；阻塞 gate 仍唯一读取 `ApprovalInbox`，allow 后自动恢复且 checkpoint 节点不重复。
- Orca 常驻恢复继续经过原 `OrcaCoordinator` 与 Effect Journal；队列落盘前崩溃时只对账终态，不重复 materialize、dispatch、cleanup 或 ack。
- 独立就绪节点按并发上限成波启动，依赖节点只在上游 Artifact 成功提交后启动；临时 token 预留不足会等待下一波，真实用量越界则失败关闭。
- `question / escalation / worker_done` 进入同一 Delivery 循环；问题必须使用真实消息 ID 回复，升级支持 continue/retry/fail，重试显式携带 `retry_of`。
- Delivery 必须整批完成并执行 worker release/retain 后才 ack；消息 ID 碰撞、Delivery 内容漂移、未知或陈旧 Dispatch 均被保守处理。
- materialize、dispatch、gate、reply、stop、cleanup、ack 均由稳定身份 Effect receipt 保护；新 Coordinator 实例可恢复且不会重复创建 Run/Dispatch 或重复提交 Artifact。
- Orca worker 输出已接入 Artifact、结构化事件和 Reality Anchor 自动发布；隔离 change-set 冲突、错误 task/dispatch 身份和无效 workspace 会失败关闭。
- `ProviderGovernanceStore` 以原子 JSON、文件锁、generation 和 checksum 统一持久化供应商请求窗口、连续失败、熔断与 half-open probe。
- 多个 Codex、Claude Code、Pi 或控制器进程共享同一配额与熔断状态；两个进程争抢最后一个 RPM 配额时只允许一个成功。
- 冷却后只有一个探针租约可执行；探针成功关闭熔断，失败重新打开，owner 消失后在超时点由一个新 owner 接管。
- wall clock 回退不会提前释放请求窗口、冷却或探针；更严格的新策略会立即解释既有状态，RSI 不能覆盖权限、费用、延迟及供应商硬约束。
- 治理状态损坏、checksum 不符和未来 schema 均失败关闭；verified reuse 命中仍不消耗供应商配额。
- Agent OS 新增 `routing/` 运行态与状态投影；治理状态明确不进入 bundle，活动或损坏状态会阻止 import 静默覆盖。
- GraphSpec 新增显式 `controlled_merge` 契约；合并源必须位于隔离工作区，验证器必须绑定来源、Reality Anchor、精确版本复用契约和既有人工 gate。
- Orca 计划和 worker contract 会投影合并约束；同步 `GraphRuntime` 明确拒绝该能力，避免无执行语义的静默忽略。
- `ControlledGitMerger` 冻结 change-set、来源/目标提交、文件清单及来源 Artifact 版本，只执行带审计消息的 `--no-ff` merge commit。
- 目标分支漂移、脏工作区、文件不匹配、验证版本错配、质量不足、gate 拒绝或 Git 冲突均失败关闭；冲突后恢复干净目标工作区。
- 来源 worker 在等待验证时临时 retain，成功合并后按原策略 release；拒绝时保留现场供人工检查。
- merge 使用可协调 Effect receipt；提交后崩溃可由精确父提交和授权消息恢复，不会重复 merge。
- Coordinator schema 升至 v2，候选自带 checksum，v1 非合并状态可安全迁移；候选、receipt 与清理状态进入运行态快照但不进入迁移包。
- `AgentOSDistribution` 已收束 Python/源码布局/文件系统/状态检查、兼容矩阵、迁移演练、恢复验证和发行生成。
- Claude Code、Codex、Pi 与 Orca 的协议诊断只调用 `--help / --version`，不触发模型、真实 Run、Task 或 Dispatch。
- 兼容矩阵现已携带精确认证版本、协议、平台和最近验证时间；未知版本不认证 ready，协议缺项失败关闭，证据文件随发行包迁移。
- 离线 fake CLI 覆盖限流、超时、进程崩溃和损坏输出；限流与超时进入可重试类型，但不绕过预算、供应商治理或 Graph 重试上限。
- 当前 v2 bundle 和旧 v1 bundle 均经过 staging 转换与两次 round-trip 稳定性验证，演练期间源状态保持不变。
- 自包含发行目录包含运行源码、测试、fake protocol fixture、示例、干净 `state.bundle`、`COMPATIBILITY.json`、`RESTORE.md` 与发行清单。
- 发行清单逐文件绑定 SHA-256、大小和 release identity；目标已存在、身份被改写或任意内容被篡改都会失败关闭。
- `EngineeringWorkflow` 已把只读探索、计划 Artifact、摘要审批、实现、检查、独立审查和有限修复形成标准日常入口。
- 计划同时绑定策略、项目规则和工作区基线；计划内容篡改、批准摘要不匹配、批准后代码或规则漂移都会被拒绝。
- 分离式执行强制提供计划摘要；一键 `ship` 只允许交互终端，并在生成计划后始终要求明确确认。
- 项目检查以参数数组直接执行，不使用 shell 拼接；保护路径在每次写调用后重新校验，链接 worktree
  的 `.git` 指针和真实 Git/common 元数据同样受到保护。
- 检查失败和 review finding 都会触发 repair、复测和全新无状态审查，达到循环/调用/时长任一上限即失败关闭。
- 准备阶段调用、token、已知费用和费用完整性进入摘要保护的计划及最终报告；`max_agent_calls` 覆盖
  全部五类角色，每次 Agent/检查超时不超过剩余总时长。
- 实现与修复调用关闭复用并使用 Effect receipt；副作用结果不确定时第二次执行不会再次调用 Agent。
- 探索、计划和审查仍可按工作区指纹安全复用；最终审查分数进入现有 RSI 质量反馈。
- `task-dir` 与 workspace、Agent OS 根目录必须完全分离且只能绑定一次任务，避免旧报告、receipt 或迁移
  状态被覆盖/混用；`engineer init / plan / run / status / ship` 已接入 CLI。
- `agent-os setup` 已形成首次使用入口：幂等初始化本地状态，并复用 `AgentOSDistribution` 完成环境与 Agent 诊断。
- 诊断 schema v3 明确区分 blocked、needs agent、ready with warnings 与 ready，同时公开阻塞检查、可用执行器和结构化下一步。
- Python、源码、文件系统、状态、缺失 Agent、协议漂移、未认证版本/平台均映射为带优先级的修复动作；复跑命令使用参数数组。
- 默认摘要只展示用户需要处理的检查，`--json` 输出同一事实；setup 只检查 help/version，明确为 0 次模型调用。

## 验证

```text
PYTHONPATH=. python3 -m unittest discover -s tests -v
Ran 216 tests — OK

python3 -m unittest tests.test_task_center tests.test_resident -v
Ran 18 tests — OK

python3 -m unittest tests.test_engineering -v
Ran 15 tests — OK

工程循环契约测试
验证策略路径安全、计划防篡改、摘要审批、工作区漂移、检查/审查修复、循环上限、保护路径、
链接 worktree Git 元数据、完整调用/成本计量、剩余时长裁剪、一次性任务目录、非交互审批防绕过、
不确定写副作用禁止重放、只读/写复用边界、RSI 反馈以及 init/status CLI；未调用真实模型

PYTHONPATH=. python3 -m unittest tests.test_distribution -v
Ran 11 tests — OK

PYTHONPATH=. python3 -m grapheng.cli setup --home /tmp/agent-os-setup-smoke --source-root .
本机状态初始化成功；Codex、Pi、Orca ready，Claude Code 版本未认证并收到 optional 修复建议；0 次模型调用

python3 -m unittest tests.test_console -v
Ran 5 tests — OK

python3 -m unittest tests.test_validation tests.test_orca tests.test_runtime tests.test_os tests.test_merge tests.test_coordinator -v
Ran 72 tests — OK

python3 -m unittest tests.test_governance tests.test_routing tests.test_reuse tests.test_os -v
Ran 32 tests — OK

python3 -m unittest tests.test_coordinator tests.test_orca -v
Ran 24 tests — OK

PYTHONPATH=. python3 -m grapheng.cli executors
发现 claude-code、pi-agent

PYTHONPATH=. python3 -m grapheng.cli validate examples/heterogeneous_agents.json
{"graph_id": "heterogeneous-review", "valid": true, "nodes": 2}

PYTHONPATH=. python3 -m grapheng.cli validate examples/verified_reuse_agents.json
{"graph_id": "verified-reuse-agents", "valid": true, "nodes": 2}

Reality Anchor 自动发布集成测试
验证正常发布、低质量拒绝、精确版本恢复补发、幂等重放和控制台投影；未调用真实模型

跨进程 single-flight 集成测试
验证双进程只执行一次、长任务 heartbeat、Leader 崩溃接管、失败脱敏传播、费用边界隔离和收据防篡改

跨进程路由治理集成测试
验证并发 RPM 原子准入、失败跨进程累计、单 half-open probe、租约接管、时钟回退、策略收紧和状态防篡改

PYTHONPATH=. python3 -m grapheng.cli orca-plan examples/orca_agents.json
输出含 2 个 Task、依赖、gate、workspace 与所有权字段的纯 JSON 计划

rsi-opt suggest → evaluate → approve → activate → orca-plan
冻结回归通过，100% 验证灰度中提示候选成功进入 Codex/Orca 任务契约

PYTHONPYCACHEPREFIX=/tmp/grapheng-pycache python3 -m compileall -q grapheng tests
通过

uv build --offline
sdist 与 wheel 构建成功

OperationsServer loopback HTTP 冒烟验收
GET / 返回 200，实时页面与 Cache-Control: no-store 生效

agent-os status → export → import → status
空状态迁移完成，root schema_version=1、bundle_schema_version=2；迁移审计与源/目标状态投影一致

v1 bundle → staging converter → v2 import
旧复用键和校验和真实转换；转换链缺口、转换器异常、future schema、活动/损坏 flight 均被拒绝

目录级提交故障注入
第二次目录替换失败后恢复原空根目录，目标无部分状态

rsi status / orca-plan --agent-os-root
统一根目录可驱动 RSI 与纯 Orca 编译；未创建 Run/Task/Dispatch

agent-os compatibility → doctor → rehearse → release → verify-release
本机 Python、Claude Code、Codex、Pi、Orca 协议诊断通过；未调用真实模型或创建 Orca 对象

发行副本 verify-release → import → doctor → 全量测试
E6 发行目录包含 66 个受清单约束的文件；恢复后的干净状态可读，发行副本内 161 项测试通过
```

Adapter 与 Orca 集成测试使用本地假 CLI，覆盖真实参数调用和返回协议，不消耗模型额度。
受控合并测试只操作临时 Git 仓库；本轮未执行 `agent-run`、真实 Orca Run/Task/Dispatch，未触碰用户项目分支。

## 当前限制

- `agent-run` 与 `engineer` CLI 仍是同步入口；高级 Graph 与 Orca 的常驻提交目前通过 Python API 使用。
- 数据权限是声明式策略，不负责创建或轮换供应商凭据。
- Orca Coordinator 已接入共享常驻进程，不需要单独 daemon，但尚无专用 CLI；本轮未对真实 Orca 状态做端到端写入验收。
- 当前候选生成只覆盖四类可解释失败；不会自由生成节点或删除依赖、gate、Reality Anchor。
- 实时操作台是单机 loopback 前台进程，不是远程多用户管理面。
- 受控合并已具备临时仓库端到端验证，尚未在真实用户项目与真实 Orca worker 上执行验收。
- `release-manifest.json` 是完整性与发行身份清单，不是带私钥信任根的密码学签名。

## 扩展本地 Agent 发现（2026-08-19）

- `agent-os setup` 新增 OpenCode、OpenClaw、Hermes Agent、Aider、Gemini CLI 与 GitHub Copilot CLI 发现。
- 已安装但未接入的工具进入 `discovered_agents`，并显示为 `discovered but not integrated`；不会进入 `ready_executors`。
- 未安装状态不产生噪声或降低健康度；help/version 异常保持未认证并给出稳定诊断状态。
- Compatibility matrix 公开 discovery-only inventory，后续真实 Adapter 必须另行补齐协议测试与版本/平台证据。
- 全部探测仅调用 help/version，本阶段没有执行 Agent 任务或触发模型调用。

```text
PYTHONPATH=. python3 -m unittest discover -s tests -v
Ran 218 tests — OK

PYTHONPYCACHEPREFIX=/tmp/agent-os-discovery-pycache python3 -m compileall -q grapheng tests
通过

uv build --offline
sdist 与 wheel 构建成功

agent-os setup --home /tmp/agent-os-discovery-smoke-20260819 --source-root .
本机初始化成功，显示 0 次模型调用；新增候选均未安装，未产生误发现或误认证
```

## 公共 Adapter Kit 与 OpenCode 执行器（2026-08-19）

- 原私有 CLI 基类已深化为公共 `CliAgentAdapter`，现有 `AgentExecutor` 继续作为唯一运行时接口。
- OpenCode v1.18.18 通过官方 Darwin arm64 发布二进制的 `--version` 与 `run --help` 只读验收；兼容证据绑定官方制品 URL 和 SHA-256。
- OpenCode 执行采用 `run --format json --pure`，禁用项目配置，以默认拒绝策略最小映射 `read / shell / edit / write`，不会启用 `--auto`。
- JSONL Adapter 归一化最终结构化文本、会话 ID、跨步骤 Token 与费用；error、429、损坏 JSONL、超时、进程崩溃和协议漂移均失败关闭。
- 实际费用、延迟、成功和故障复用现有 Registry → PolicyRouter 脱敏观测；OpenCode 不宣称未提供的硬费用上限。
- OpenCode 已从 `discovered_agents` 晋升为 `ready_executors` 候选；版本、平台或 help 协议不匹配时仍不会被认证。
- 产品会话继续由 OpenCode 自身管理，不进入 Agent OS 可迁移包；OpenClaw、Hermes、Aider、Gemini CLI 与 GitHub Copilot CLI 保持仅发现。

```text
PYTHONPATH=. python3 -m unittest discover -s tests -v
Ran 223 tests — OK

PYTHONPYCACHEPREFIX=/tmp/agent-os-opencode-pycache python3 -m compileall -q grapheng tests
通过

uv build --offline
sdist 与 wheel 构建成功

官方 opencode-darwin-arm64 v1.18.18 --version / run --help
版本与 --format、--model、--pure 协议验收通过；未安装、未调用模型
```
