# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## Unreleased

- 修复 Claude Code `--safe-mode` 在版本间漂移导致的启动失败；按本机协议自适应参数，同时始终保持安全模式环境隔离。
- 将 Agent 节点 `max_tokens` 提升为执行器硬能力契约；无硬 Token 上限的执行器和 Orca worker 会在调用前失败关闭，复用键与并发预留同步纳入该边界。
- 提供公共 `CliAgentAdapter` 开发包，并新增经过版本/平台认证的 OpenCode JSONL 执行器、默认拒绝权限、故障注入和 RSI 费用观测。
- 修复 Intel macOS 上 Codex 启动脚本存在但架构原生程序缺失时仍被注册的问题，并将平台纳入兼容认证。
- 增加目标意图编译、五类高频任务模板、显式约束与可审阅计划摘要。
- 自动识别 Python、Node、Rust、Go 和 Make 项目验证命令；关键上下文缺失时在 Agent 调用前停止。
- 将目标、约束、模板和验证方式纳入审批摘要，并对调研任务强制只读执行。
- 增加统一的 `do / status / approve / control / result` 用户任务入口。
- 增加稳定 task ID、面向人的摘要、机器 JSON，以及产物、验证、费用和人工介入结果视图。
- 将任务运行态与可迁移 RSI 状态分区，避免原始目标和项目路径进入状态导出包。
- 增加隐私最小化的用户结果评测、工程报告导入和不可覆盖的基线快照。
- 增加 `evaluate record-engineering / baseline / status` 命令。

## 0.0.1 - 2026-08-17

首个公开预览版本：

- 提供严格 DAG、Artifact 契约、预算、审批、恢复和 Reality Anchor。
- 统一编排 Codex、Claude Code、Pi Agent 与 Orca。
- 提供安全复用、供应商治理、跨进程 single-flight 和受控合并。
- 提供带灰度、回归和回滚机制的 RSI 学习闭环。
- 提供标准工程循环和本地操作控制台。
