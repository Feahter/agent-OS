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
