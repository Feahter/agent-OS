# Agent OS 0.1 执行结果

## P1：真实项目评测基线

### 已完成

- 定义版本化 `EvaluationCase`，只保存任务分类、规模、数据级别和标签。
- 从工程工作流报告提取验证、质量、耗时、费用、调用量、人工介入和恢复指标。
- 原始目标、工作区路径、审批人、prompt、响应和错误正文不会进入评测记录。
- 提供 `evaluate record-engineering / baseline / status` 命令。
- 基线名称不可覆盖，记录损坏、版本未知或字段不完整时失败关闭。
- 增加离线示例与 6 项评测契约测试。

### 验证

- `PYTHONPATH=. python3 -m unittest discover -s tests -v`：167 项通过。
- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m compileall -q grapheng tests`：通过。
- `uv build --offline`：sdist 和 wheel 构建成功，包含评测模块和示例。

### 待执行

P1.4 需要选择三个外部项目并调用真实 Agent，可能产生模型费用和项目改动。执行前需逐个确认项目、任务、预算和隔离方式。

## P2：统一任务接口

### 已完成

- 提供 `do / status / approve / control / result` 五个稳定动作，并保留 GraphSpec 高级入口。
- `do` 返回稳定 task ID，计划批准自动绑定摘要，用户无需复制 digest。
- 默认输出面向人的状态、下一步和费用摘要；`--json` 提供稳定机器输出。
- `result` 汇总实现结果、计划与报告 Artifact、检查、独立审查、费用和人工介入。
- 五个动作直接读取同一份工程计划、状态和报告；`task.json` 只保存定位信息，不复制任务阶段。
- 任务运行态与可迁移 RSI 状态分区，原始目标、项目路径和运行证据不进入导出包。
- `control` 当前支持批准前取消；后台暂停、恢复和崩溃续跑由 P4 常驻协调器接管。

### 验证

- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m unittest tests.test_tasks tests.test_engineering -v`：20 项通过。
- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m unittest discover -s tests -v`：173 项通过。
- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m compileall -q grapheng tests`：通过。
- `uv build --offline`：sdist 和 wheel 构建成功，包含统一任务模块与测试。

## P3：意图编译与任务模板

### 已完成

- `do` 将自然语言目标、显式约束、项目类型、验证方式和安全假设编译进现有 `plan.json`，没有新增并行任务状态。
- 首批模板覆盖修复、测试、重构、调研和发布准备；普通变更使用安全的通用后备模板。
- 自动识别 Python、Node、Rust、Go 和 Make 项目，并优先使用项目已有策略；无法确定关键目标或可信验证方式时在 Agent 调用前失败关闭。
- 默认状态同时展示意图、约束、验证命令和计划步骤；目标、约束、模板、策略或计划变化都会使批准摘要失效。
- 调研模板只能使用读取与查询能力，执行后再次核对完整工作区指纹，发现任何改动都会失败关闭。
- 继续沿用工程工作流的预算、保护路径、独立审查、Reality Anchor 和 RSI 反馈，不绕过既有治理。

### 验证

- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m unittest tests.test_intents tests.test_tasks tests.test_engineering -v`：31 项通过。
- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m unittest discover -s tests -v`：184 项通过。
- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m compileall -q grapheng tests`：通过。
- `uv build --offline`：sdist 和 wheel 构建成功，包含意图编译模块与测试。

## P4：本地常驻协调器（P4.1–P4.3）

### 已完成

- 单实例常驻进程使用持久优先级队列承接工程任务、高级 Graph 和 Orca 作业；旧 v1 工程任务队列会原位迁移到异构作业 schema。
- 队列只保存 `kind/reference`、优先级、顺序和控制意图；工程 `plan/status/report`、Graph 控制面、审批决定以及 Orca 状态均继续由原模块拥有。
- `LocalControlPlane` 支持先持久准备、再由另一进程启动；Graph gate 阻塞后进入等待，原 `ApprovalInbox` 写入 allow 后由常驻进程自动恢复。
- Graph 恢复继续使用 checkpoint，已完成节点不会因审批续跑而重复；运行 lease 未过期时保守等待，过期后才接管。
- Orca 作业通过同一常驻生命周期调用原 `OrcaCoordinator`；materialize、dispatch、cleanup、ack 和受控合并继续由 Effect Journal 防重复。
- Orca 取消会在安全检查点停止活动 dispatch、执行既有保留/清理策略并持久化终态；重启重复取消不会重放外部副作用。
- 故障注入覆盖“Graph 已完成节点后等待审批”和“Orca 已完成外部副作用但队列尚未落终态”两个崩溃点。

### 状态归属

- 工程任务：`tasks/<task-id>/plan.json|status.json|report.json`。
- 高级 Graph：`runtime/graphs/runs/<run-id>`；审批继续位于可迁移 Agent OS `approvals/`。
- Orca：`runtime/orca/<job-id>`，包含 Coordinator state、event 和 Effect Receipt。
- 常驻队列：只做排序、等待投影和控制，不复制上述运行内容，也不进入 RSI 导出包。

### 验证

- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m unittest discover -s tests -v`：199 项通过。
- `PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache python3 -m compileall -q grapheng tests`：通过。
- `uv build --offline`：sdist 和 wheel 构建成功，包含异构常驻作业适配器。
- 故障测试额外覆盖：Orca 从崩溃恢复后的暂停态收到取消请求，只停止并清理活动 dispatch 一次。
