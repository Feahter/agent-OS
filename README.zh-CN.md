# Agent OS：面向编码 Agent 的 Graph Engineering 运行时

**用 Graph Engineering 编排编码 Agent。**

[English](README.md) | [简体中文](README.zh-CN.md) | [长期路线图](ROADMAP.zh-CN.md)

[![Version](https://img.shields.io/badge/version-0.0.1-blue.svg)](https://github.com/Feahter/agent-OS/releases/tag/v0.0.1)
[![CI](https://github.com/Feahter/agent-OS/actions/workflows/ci.yml/badge.svg)](https://github.com/Feahter/agent-OS/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Agent OS 是一个本地优先的运行时，把 Codex、Claude Code、Pi 和 Orca 接入同一套受控执行系统。任务由经过验证的图描述，节点通过显式 Artifact 交换数据；审批、预算、验证、恢复和学习由统一控制面管理。

运行时没有第三方依赖，需要 Python 3.9+ 和兼容 POSIX 的系统。

> **当前状态：** `0.0.1` 是预览版本。核心契约已有测试覆盖，公开 API 仍可能调整。

## 为什么需要 Agent OS

单个编码 Agent 已经很好用，但不同工具没有共同的执行语义。它们各自管理会话、权限、输出和重试。脚本可以同时启动几个 Agent，却很难可靠回答：哪个结果可以信任、某个副作用是否已经发生、崩溃恢复时哪些步骤能够重放。

Agent OS 把这些判断收进一个小而可审计的运行时：

- 依赖、并发和终止条件都写进显式任务图。
- 节点只能读写声明过的 Artifact。
- 只有经过 Reality Anchor 确认的结果才能进入持久缓存。
- 预算、审批、重试、时限和保护路径默认失败关闭。
- RSI 可以改进路由和保守的流程候选，不能放宽安全约束。
- prompt、凭据和运行状态由操作者掌握。

## 执行闭环

标准工程流程有明确上限：

```text
任务目标
  → 只读探索
  → 计划 Artifact
  → 绑定摘要的人工批准
  → 实现
  → 自动检查
  → 独立只读审查
  → 有限修复循环
  → Reality Anchor 报告
  → 不含 prompt 的学习信号
```

每次状态转换都有结构化证据。工作区、策略或计划一旦变化，原有批准立即失效。写操作由 Effect Receipt 保护，进程在结果不确定时崩溃，也不会静默重放同一副作用。

## 已包含的能力

| 模块 | 作用 |
| --- | --- |
| 图运行时 | 校验 DAG、Artifact 契约、并发、重试、gate、预算和终点 Reality Anchor。 |
| Agent Adapter | 统一 Codex、Claude Code、Pi 的能力、工具、用量、费用和结构化输出。 |
| 策略路由 | 按能力、数据级别、质量、费用、延迟、限流和熔断状态选择执行器。 |
| 工程工作流 | 执行探索、计划、批准、实现、检查、独立审查和有限修复。 |
| Orca 协调 | 把图编译为 Run/Task/Dispatch 契约，协调隔离 worker 和受控合并。 |
| 恢复机制 | 持久化 checkpoint、Effect Receipt、lease 和事件游标，支持崩溃续跑。 |
| RSI | 基于脱敏遥测和质量反馈学习，候选需评估、批准、灰度和可回滚。 |
| 安全复用 | 合并并发重复任务，只持久化经过明确验证且策略兼容的结果。 |
| 本地运维 | 提供状态、审批、血缘、费用、自诊断、迁移和发行校验。 |

## 快速开始

```bash
git clone https://github.com/Feahter/agent-OS.git
cd agent-OS
python3 -m pip install -e .

agent-os setup

agent-os validate examples/minimal_graph.json
agent-os demo examples/minimal_graph.json --work-dir /tmp/agent-os-demo
```

`setup` 会初始化本地可迁移状态，检查 Python、文件系统语义、Codex、Claude
Code、Pi 和可选 Orca，并按优先级给出修复步骤。它只执行 help/version 探测，
不会调用模型。需要版本化诊断数据时使用 `--json`，需要更换本地状态目录时使用
`--home /path/to/state`。

### 日常任务入口

无论底层选择哪个本地 Agent，都使用同样五个动作。常见 Python、Node、Rust、Go 和 Make 项目可自动推断有限的验证命令；需要明确项目规则时再初始化策略：

```bash
agent-os do "修复登录超时并补回归测试" \
  --workspace /path/to/project \
  --constraint "保持公开接口不变"

agent-os status task-0123456789abcdef
agent-os approve task-0123456789abcdef --actor operator
agent-os result task-0123456789abcdef
```

`do` 会先把目标编译成可审阅意图，再进行只读探索和计划，返回稳定 task ID，并停在绑定摘要的审批点。审批同时绑定目标、约束、模板、验证命令、项目规则和计划步骤，任一内容变化都会使原审批失效。`approve` 只执行这份计划，完成有限检查和独立审查后才返回。批准前可用 `control TASK_ID cancel --actor NAME` 取消任务。五个动作都可加 `--json` 获得机器可读输出。

需要退出终端后继续运行时，把批准改为后台执行。本地常驻协调器会自动启动，使用持久优先级队列，并继续复用前台任务的同一份状态与报告：

```bash
agent-os approve task-0123456789abcdef \
  --actor operator \
  --background \
  --priority 10

agent-os status task-0123456789abcdef
agent-os control task-0123456789abcdef pause --actor operator
agent-os control task-0123456789abcdef resume --actor operator
agent-os control task-0123456789abcdef reprioritize --priority 20 --actor operator
agent-os control task-0123456789abcdef cancel --actor operator

agent-os center
agent-os center --json
```

暂停和取消会在 Agent 调用或验证步骤之间的下一个安全检查点生效；已经开始的单次 Agent 调用会先运行到该检查点，系统不会假装支持任意时刻强行挂起。进程重启或任务继续后，已完成的写操作会从 Effect Receipt 恢复，不会静默重放。

`center` 是工程任务、高级 Graph 和 Orca 作业共用的一屏任务中心。需要审批、回答或恢复的事项优先显示，其后是运行中与已完成任务；显示条数限制不会截断总数统计。常驻协调器在 macOS 或 Linux 上调用系统原生通知能力，提示等待、暂停和任务终态。通知采用尽力投递和持久去重，通知失败不会改变任务结果；通知记录只保存在 `runtime/resident`，不会进入可迁移 RSI 状态。

首批模板为 `fix`、`test`、`refactor`、`research` 和 `release`，默认自动识别，也可用 `--template` 指定；调研任务会被强制为只读。目标过于含糊或找不到可信验证命令时，`do` 会在调用 Agent 前停止并说明缺少什么。若要自定义检查与预算，可先运行 `agent-os engineer init --workspace /path/to/project`，再编辑 `.agent-os/engineering.json`。

默认主目录是 `~/.agent-os`，可用 `AGENT_OS_HOME` 或 `--home` 覆盖。`tasks/` 保存运行态，也是状态和结果的唯一事实来源；`state/` 只保存可迁移、无 prompt 的学习与策略状态。原始目标、项目路径和运行证据不会进入状态导出包。

这些命令可能调用真实本地 Agent 并产生模型费用。GraphSpec 和现有底层命令继续作为高级接口保留。

运行全部测试：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

### 建立用户结果基线

评测记录只保留任务类别、验证结果、耗时、费用、人工介入和恢复指标，不复制目标、项目路径、prompt 或原始响应：

```bash
agent-os evaluate record-engineering \
  --root /path/to/evaluation-state \
  --case examples/evaluation_case.json \
  --report /path/to/task/report.json \
  --run-id pilot-001 \
  --user-inputs 2 \
  --human-decisions 1

agent-os evaluate baseline \
  --root /path/to/evaluation-state \
  --name v0.0.1
```

基线以“每个验证成功结果”的时间、费用和人工介入衡量体验。已有名称不能覆盖，后续版本可以用相同评测场景生成新快照进行比较。

### 使用工程工作流

先初始化项目策略。每个任务的状态目录必须放在目标工作区之外：

```bash
agent-os engineer init --workspace /path/to/project

agent-os engineer plan \
  --workspace /path/to/project \
  --task-dir /tmp/agent-os-tasks/task-001 \
  --agent-os-root /path/to/agent-os-state \
  --objective "修复登录超时并补回归测试"

agent-os engineer run \
  --workspace /path/to/project \
  --task-dir /tmp/agent-os-tasks/task-001 \
  --agent-os-root /path/to/agent-os-state \
  --approved-by operator \
  --plan-digest PLAN_DIGEST
```

交互终端可以用 `engineer ship` 合并计划和执行，但命令仍会要求明确的 `y/N` 确认。自动化环境必须使用分离的 `plan` 和 `run --plan-digest` 流程。

### 运行异构 Agent 图

`examples/heterogeneous_agents.json` 让 Claude Code 生成 Artifact，再由 Pi 独立审查：

```bash
agent-os agent-run examples/heterogeneous_agents.json \
  --work-dir /tmp/agent-os-run \
  --workspace /path/to/project \
  --agent-os-root /path/to/agent-os-state
```

这条命令会调用真实模型并可能产生费用。没有依赖关系的 Agent 不能共享可写工作区；隔离工作区必须交给 Orca 后端执行。

高级集成可以把 Graph 或 Orca 作业提交给同一个 `ResidentCoordinator`，退出调用进程后仍由统一优先级队列恢复。常驻层只保存作业类型、状态定位符、顺序和控制意图；Graph 状态仍属于 `LocalControlPlane`，gate 决策仍唯一写入 `ApprovalInbox`，Orca 的 Run/Task/Dispatch 和 Effect Receipt 仍属于 `OrcaCoordinator`：

```python
from pathlib import Path
from grapheng import GraphSpec, ResidentCoordinator

resident = ResidentCoordinator(Path.home() / ".agent-os")
graph = GraphSpec.from_json(Path("graph.json"))
resident.schedule_graph(graph, Path("/path/to/project"), priority=10)
# 或：resident.schedule_orca(graph, Path("/path/to/project"), priority=10)
resident.start_background()
```

## GraphSpec 契约

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
      "deps": ["draft"],
      "reads": ["report"],
      "writes": ["verification"],
      "verifier_for": "draft",
      "reality_anchor": true,
      "gate": "release"
    }
  ]
}
```

节点只能读取声明过的 Artifact，返回值必须精确匹配 `writes`。执行前的图校验会拒绝环、缺失生产者、无序写入、不安全的共享工作区和没有 Reality Anchor 的终点路径。

## 支持的工具

| 工具 | 已验证版本（Darwin arm64） | 协议 | 接入方式 |
| --- | --- | --- | --- |
| Codex | `0.148.0-alpha.9` | `exec-jsonl-v1` | 本地 CLI Adapter |
| Claude Code | `2.1.234` | `json-envelope-v1` | 本地 CLI Adapter |
| Pi | `0.84.1` | `message-end-jsonl-v1` | 本地 CLI Adapter |
| Orca | `1.4.180` | `orca-json-command-v1` | 图编译器、后端与协调器 |

Adapter 会把统一的 `read / shell / edit / write` 工具契约翻译成各产品协议。上述版本于 2026-08-18 在 Darwin arm64 上完成只读协议验收，证据保存在版本库并随发行包迁移。认证范围包含平台：同一版本出现在其他操作系统或架构时会标记为 `unverified_platform`，且不会进入 `ready_executors`。

Codex 执行器发现会运行安全启动探测，诊断则运行帮助和版本探测；两者都不会调用模型，Orca 版本从应用包读取。Codex 在注册前会运行 `codex exec --help`，因为 npm 启动脚本可执行并不能证明对应架构的原生程序实际存在。Codex `0.148.0-alpha.9` 尚未在 Darwin x86_64 上通过认证。版本和平台与证据完全匹配时才标记为已验证；协议匹配但版本或平台未知时只警告且不认证为可执行；程序缺失或协议缺项则失败关闭。

离线故障基线覆盖限流、超时、进程崩溃、损坏输出和协议漂移。限流与超时被归类为可重试故障，但仍受图重试、预算和供应商治理约束；进程崩溃和未知协议不会被伪装成成功结果。

## 受治理的 RSI

这里的 RSI 指受控的策略改进，不是让运行时自行改写代码。

1. Agent 调用记录不含 prompt 的成功率、延迟和费用。
2. Reality Anchor、验证器或操作者补充质量分。
3. 系统生成版本化候选，用硬阈值或冻结回归集评估。
4. 候选必须经过人工批准才能激活。
5. 新策略先进入确定性灰度，指标回退会触发回滚。

学习策略不能覆盖数据权限、预算、供应商限流、熔断、gate 或 Reality Anchor。某类任务的样本不足时，路由会回退到全局策略，不根据少量数据做激进判断。

## 安全与恢复

- 图、策略和工作区指纹把批准绑定到审阅时的状态。
- Effect Receipt 阻止结果不确定的外部写入被盲目重放。
- checkpoint 会先核对图身份，再恢复已完成节点。
- 多个进程共享供应商限流和熔断状态时使用原子协调。
- 持久复用要求请求身份、作用域和验证结果完全匹配。
- `confidential` 和 `restricted` 请求默认跳过持久复用。
- Agent OS 导出包不包含凭据、prompt、原始响应、worktree 和活动 lease。
- 受控 Git 合并要求隔离来源、精确验证、命名 gate 和未漂移的目标分支。

遇到状态损坏、未来 schema 或外部协议漂移时，系统会停止执行，而不是猜测恢复。

## 状态迁移

稳定的 Agent OS 根目录保存 learning、optimization、reuse、approvals 和 routing 状态。导出包使用严格文件白名单、逐文件 SHA-256 和 staging schema 转换。

```bash
agent-os agent-os status --root /path/to/agent-os-state
agent-os agent-os compatibility
agent-os agent-os export --root /path/to/agent-os-state --bundle /tmp/agent-os.bundle
agent-os agent-os import --root /path/to/restored-state --bundle /tmp/agent-os.bundle
agent-os agent-os doctor --root /path/to/agent-os-state
```

## 项目结构

```text
grapheng/     运行时、Adapter、协调、治理与 RSI
examples/     GraphSpec 与策略示例
tests/        契约、恢复和跨进程集成测试
.workflow/    实现计划与验收证据
```

## 当前限制

- `agent-run` 和 `engineer` 仍是同步 CLI；高级 Graph/Orca 后台提交目前通过 Python 接口使用。
- Orca 已复用 Agent OS 常驻生命周期，不再需要独立 daemon；问题与升级处理仍通过 Coordinator Python 接口完成。
- 数据分级是策略约束，各工具仍需自行管理凭据生命周期。
- 当前兼容认证精确绑定上表版本；升级工具后应先运行 `doctor` 并补充新的协议证据。
- 真实项目和真实 Orca 接入需要先做小流量、可观察的灰度。
- 跨主机状态、多租户隔离和带信任根的发行签名尚未实现。

## 参与贡献

项目将按用户目标驱动、统一控制面、受治理 RSI 和状态可迁移的方向逐步演进，详见[长期路线图](ROADMAP.zh-CN.md)。提交 Pull Request 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题按 [SECURITY.md](SECURITY.md) 的方式私密报告。

## 许可证

[MIT](LICENSE)
