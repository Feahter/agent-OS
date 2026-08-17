# Graph Engineering Foundation

## 阶段目标

交付一个兼容 Python 3.9、无第三方运行时依赖的 Graph Engineering 内核，作为后续
Codex、Orca 和 Claude worker 适配层的共同基础。

## 工作单元

### Task 1: GraphSpec 与静态校验（已完成）
[deps: none] [owner: self] [isolate: no]

- 定义节点、依赖、读写契约、重试、预算、门控、验证者和现实锚点。
- 检查循环、缺失依赖、并发写冲突和无效验证拓扑。

### Task 2: Artifact 与治理接口（已完成）
[deps: Task 1] [owner: self] [isolate: no]

- 提供不可变、带版本和校验和的 Artifact Store。
- 提供节点处理器注册、声明式输入输出、预算和人工门控接口。

### Task 3: DAG 运行时（已完成）
[deps: Task 1, Task 2] [owner: self] [isolate: no]

- 基于真实依赖调度并行节点。
- 支持失败传播、选择性重试、预算准入和门控阻断。

### Task 4: 事件、检查点与恢复（已完成）
[deps: Task 3] [owner: self] [isolate: no]

- 输出可关联到 run、graph、node 的 JSONL 事件。
- 原子保存检查点；恢复时跳过已完成节点并重新评估阻断节点。

### Task 5: CLI、示例与验证（已完成）
[deps: Task 1, Task 3, Task 4] [owner: self] [isolate: no]

- 提供 validate/demo 命令和示例 GraphSpec。
- 用标准库 unittest 覆盖校验、调度、重试、门控、事件和恢复。

## 暂不包含

- Orca/Codex worker 启动适配器。
- Web/TUI 图可视化。
- 通用知识图谱抽取与 GraphRAG。
- 分布式队列、远程 Artifact Store 和生产级身份认证。

## 后续任务拆分

### Phase 2: Agent 适配层

- A1（已完成）：统一 AgentExecutor、能力注册、工具翻译和结构化结果协议。
- A2（已完成）：实现 Claude Code、Pi Agent CLI Adapter 和 Graph 节点绑定。
- A3：扩展异步生命周期、流事件、取消、心跳和租约。
- A4：实现 Codex Adapter 与 Orca Run/Task/Dispatch 编排后端。
- A5：建立跨 Adapter 契约测试，继续统一错误分类和恢复语义。

### Phase 3: 控制面与可观测性

- C1：提供图运行详情、节点状态和 Artifact 血缘视图。
- C2：实现可恢复的人工审批队列和审批审计。
- C3：从 JSONL 事件确定性重建运行视图，增加失败回放。
- C4：增加节点级耗时、token、重试和关键路径统计。

### Phase 4: 生产化

- P1：远程 Artifact Store、分布式租约和幂等提交。
- P2：身份、权限、密钥隔离和策略版本化。
- P3：GraphSpec schema 版本与 checkpoint 迁移。
- P4：故障注入、并发压力和崩溃恢复测试。
