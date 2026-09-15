# Agent OS 威胁模型

## 范围与安全目标

本模型覆盖本地单用户、受控试点部署中的 Graph Runtime、Resident、Agent CLI Adapter、Orca、工作区和持久化状态。安全目标是：未经批准不扩大写权限；越权工作区修改不可被恢复流程接受；陈旧 owner 不得写回；无法证明的外部副作用保持 indeterminate；发布产物来源和依赖可核验。

当前不承诺抵御已取得同一 OS 用户权限的攻击者，也不提供多租户隔离。

## 资产与信任边界

- 资产：源代码、Git 历史、本地 Agent 凭据、模型输入输出、审批记录、checkpoint、receipt、队列、发布密钥身份与构建产物。
- 受信任边界：操作者、当前 OS 用户、经审阅的 policy/GraphSpec、GitHub OIDC 发布身份。
- 非受信任输入：仓库内容与说明文件、模型输出、Agent/Orca CLI JSON、外部工具版本、恢复时读取的磁盘状态、下载的发行包。
- 进程边界：Agent OS 会启动 Codex、Claude Code、Pi、OpenCode 与 Orca 子进程。这些工具拥有各自的更新、认证、网络和配置边界。

## 威胁与控制

| 威胁 | 已有控制 | 剩余风险/操作要求 |
| --- | --- | --- |
| 本地凭据泄露 | 可迁移 bundle 明确排除凭据、prompt、原始响应和 worktree；Adapter 使用有限参数与工具映射。 | 子进程继承运行用户环境，并可能读取各工具自己的凭据存储。使用专用低权限账号/凭据，避免把无关 secret 放进环境。Agent OS 不是凭据保险库。 |
| 恶意仓库指令或 prompt injection | Graph/policy/workspace 指纹绑定审批；写工具需显式声明；高风险路径有 gate、Reality Anchor、protected-path 校验和隔离 workspace。 | 模型仍可能被仓库文本影响。不要把仓库内容视为可信指令；高价值仓库必须人工审阅计划和 diff，优先只读及隔离 workspace。 |
| workspace 越权或路径穿越 | workspace identity、受保护路径 snapshot、原子持久化、受控 merge 和 postcondition；未知 effect 类型 fail-closed。 | 这不是内核级文件系统隔离。运行用户可访问的其他路径并不会自动变得不可访问；需配合 OS 权限、容器或虚拟机。 |
| Adapter 子进程失控、协议漂移或输出欺骗 | 受控参数、超时、输出上限、结构化 JSON 校验、精确版本/平台证据和 fail-closed discovery。 | 子进程自身可能联网、更新或调用插件；仅安装审阅过的版本，并用外部网络策略限制出站访问。 |
| receipt/checkpoint/queue 篡改 | 严格 schema、身份/digest 校验、fencing、原子替换、目录 `fsync`、未知版本拒绝。 | 同一 OS 用户可同时修改状态和源码，因此本地 digest 不构成对该攻击者的密码学认证。异常时保留状态副本并停止恢复。 |
| 多进程陈旧 owner | lease generation、不可复用 token、所有关键 settle/effect publication 的 fencing。 | 只覆盖共享 POSIX 文件系统语义；跨主机、弱一致网络文件系统不在支持范围。 |
| 多用户读取或互相控制任务 | 当前目录权限由 OS 用户和 umask 决定；没有远程控制面。 | 未实现多租户身份、授权、审计隔离。不同信任域必须使用不同 OS 账号或隔离主机。 |
| 依赖或发布产物被替换 | CI frozen `uv.lock`、wheel/sdist smoke test、CycloneDX SBOM、SHA-256 清单、GitHub OIDC build-provenance attestation。 | 本地 `agent-os release` 目录只有校验和，不自带外部签名；正式分发必须来自 tag 发布工作流并按验证指南核验。 |

## 明确不提供的能力

Agent OS 目前不是 OS 沙箱、网络隔离层、恶意代码分析器、凭据保险库或多租户授权系统。CLI 中的 `sandbox`/安全模式属于各 Adapter 的进程参数与工具策略，不能替代容器、虚拟机、主机防火墙、最小权限账号和独立 secret 管理。

## 安全部署基线

1. 使用专用 OS 账号和最小权限 Agent 凭据；清理无关环境变量。
2. 对未知仓库先执行只读任务，审阅计划、GraphSpec、policy 和 Adapter 版本证据。
3. 写任务默认隔离 workspace；shared-workspace 写入只允许 verified-idempotent 或 reconcilable effect。
4. 对网络、包管理器、Git remote 和云凭据施加 Agent OS 之外的策略。
5. 保留 receipt、checkpoint、事件和 canary 报告；状态损坏或 indeterminate effect 时停止自动执行。
6. 仅消费通过 [发布验证](release-verification.md) 的正式产物；漏洞按 [SECURITY.md](../SECURITY.md) 私密报告。
