# Changelog

本项目遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## Unreleased

- 统一本地主目录契约：`setup --home H`、日常任务和 Resident 现在共同使用 `H/state` 作为可迁移状态根；即使 `H/runtime` 已存在也能安全初始化，不再形成启动死锁或两套状态。
- 将安装完整性与源码发行完整性拆开：wheel 安装后的 `setup` 不再依赖 README、测试和 `.workflow`，源码 release 仍严格要求完整 checkout；CI 新增真实 wheel/sdist smoke test。
- 修复规划失败遗留无 ID 孤儿任务、后台 Resident 启动失败遗留批准状态，以及崩溃后无法通过 `status` / `center` / `resume` 恢复的问题。
- 遥测统一对 `detail` / `reason` / `error` 等自由文本只保存摘要指纹与长度；Agent 执行类错误由 CLI 输出单行错误，不再显示 traceback。
- 补充本机 Claude Code `2.1.266`、Pi `0.85.1` 与 Orca `1.4.192` 的 Darwin arm64 协议证据；Claude 另通过一次 `$0.25` 硬上限的只读结构化输出 canary（实际费用 `$0.02937175`）。
- 将原子写、JSON 读取和文件锁收敛到唯一实现 `grapheng/_store.py`：原先散落在 16 个模块的副本语义已经漂移（部分缺 `fsync`、全部未 flush 父目录），统一实现补上目录 fsync 以保证重命名本身可在断电后存活；`scripts/check_shared_primitives.py` 在 CI 中阻止副本回归。
- 为 Agent 调用增加硬输出上限：`run_bounded_process` 增量抽取 stdout/stderr，越限即终止进程并失败关闭，不再把整个流缓冲进内存；上限可用 `AGENT_OS_MAX_AGENT_OUTPUT_BYTES` 调整。
- 将 Agent 失败归因显式化：`FailureClassification` 区分 `exit_code` / `structured` / `heuristic` 证据来源，路由与熔断不再把子串猜测当作事实。
- 增加结构化遥测 `grapheng/telemetry.py`：无提示词的 JSONL 事件日志写入 `<home>/runtime/logs/`，同时经 `grapheng` logger 输出；覆盖 Graph 节点迁移、常驻任务状态迁移和 Agent 调用。
- 增加 `grapheng/index.py` sqlite 投影索引：`center` 不再对每个历史任务重复读盘，仅在源文件指纹变化时重算；索引失败一律回退重算。
- 拆分 Orca 协调器：协议解码（`orca_protocol`）、状态文档校验（`orca_state`）、结果发布（`orca_publication`）成为可独立测试的模块。
- CLI 不再对预期内的契约失败抛出 traceback，改为可读错误与退出码 2。
- 修复 `OrcaCoordinator._executor_id` 被误标为 `@staticmethod` 却引用 `self`，以及 `ExecutorProfile` 校验消息把 `dataclasses.field` 函数当成字段名输出。
- CI 增加 ruff、mypy、macOS 与 Python 3.13 覆盖，测试统一为 pytest。

- 将协议、版本和平台兼容证据提升为运行时硬闸门；`setup` 未认证的 Agent 不再被真实任务发现，并补充本机 Codex `0.149.0-alpha.4.1` 与 Claude Code `2.1.241` 的 Darwin arm64 零模型证据。
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
