# ROI 计划 1：上下文与 token 降本

## 结论

这是当前直接 ROI 最高的计划。实测六个小节点用了 109,624 input tokens，只产生 349 output tokens；输出/input 比约 0.32%。调度已经把可并行节点压进关键路径，但每次 Agent 调用携带的固定上下文仍然过大。

预计投入 1.5–3 人周。首个退出目标是 total tokens 降到 70,000 以下，挑战目标 55,000 以下，同时保持 6/6 成功和不超过 65 秒的墙钟时间。

离线实现状态（2026-09-15）：上下文编译、reads 投影、复用和历史 reservation 已完成并通过确定性测试；真实六节点 token、质量和时延对照按用户要求暂不执行，因此退出目标保持未验收。

## 模块与 seam

把上下文选择、压缩、指纹和预算收进一个内部深模块 `ContextCompiler`。它位于 `AgentRequest` 与共享 prompt 生成之间的 seam；调用方只提交任务目标、声明的输入 Artifact、输出键和预算，不需要理解各执行器的 prompt 格式。

建议接口保持为一个操作：

```text
compile(request, policy) -> CompiledContext
```

`CompiledContext` 提供正文、字节数、内容指纹、包含/省略项和预算决策。Codex、Claude、Pi、OpenCode Adapter 继续只处理 provider 协议，不各自实现上下文裁剪。

## 实施清单

### A. 建立可归因基线

- [x] 为每个节点记录 prompt envelope 字节数、输入 Artifact 字节数和静态契约字节数。
- [x] 记录 provider 的 input/cache/output/total token 完整性，不根据字节数伪造 token。
- [ ] 将六节点基准按节点拆出 token 表，识别固定开销和随输入增长的开销。
- [x] 增加一个 1 KB、10 KB、100 KB Artifact 梯度场景，避免只优化微型 prompt。

### B. 压缩重复的执行契约

- [x] 审计共享 `_prompt` 中每次重复发送的文本和 JSON 键。
- [x] 将执行契约改成最短、确定、版本化的内部模板；不减少输出键校验和工具约束。
- [x] 删除能从结构化参数确定、但仍在自然语言中重复的字段。
- [x] 对 JSON 使用稳定紧凑编码，确保相同语义得到相同指纹。
- [x] 对每个 provider 检查系统提示与用户提示是否重复表达同一规则。

### C. 只发送依赖所需上下文

- [x] 保持 Artifact 声明为唯一数据入口，不把完整运行状态注入节点。
- [x] 由节点 `reads` 在预算前仅投影声明的 Artifact key；Artifact 内部子字段/文本片段尚无声明 seam，不把 key 级裁剪夸大为子字段切片。
- [x] 在编译结果中记录省略原因，调试时可解释“为什么没有发送某段上下文”。
- [x] 超出预算时失败关闭或请求改图，不做静默截断。
- [x] 增加 cached input 是 input 子集与 provider 独立计量两类回归，防止重复相加。

### D. 提高安全复用命中率

- [x] 复核 `VerifiedArtifactCache` 的 key，区分真正影响结果的字段和运行时噪声。
- [x] 只复用经过 Reality Anchor 验证、策略兼容且未过期的结果。
- [x] 保留跨进程 single-flight，测量 hit/miss/coalesced/bypassed 及 saved tokens。
- [x] 为低风险纯提取节点增加复用资格测试；写工作区或依赖实时状态的节点继续 bypass。
- [x] 不以持久 session 作为第一阶段方案；除非能证明隔离、恢复和可重放语义不退化。

### E. 预算前置

- [x] 在启动 Agent 前用历史分位数生成节点 token reservation，而不是固定拍值。
- [x] 历史不足时使用保守上界；预测只能影响 admission，不能覆盖 provider 实测 usage。
- [x] 当实际用量持续偏离估算时输出可行动告警，而不是自动放宽图预算。

## 测试清单

- [x] `ContextCompiler` 接口测试覆盖稳定指纹、紧凑编码、预算溢出和省略说明。
- [x] 四个 Adapter 的协议 fixture 保持结构化输出与 usage 分项完整。
- [x] 变异测试：恢复旧的重复契约后，token/字节预算回归必须变红。
- [x] 变异测试：绕过 reads 投影后，大 Artifact 场景必须超过预算并失败。
- [x] 复用测试验证未通过 Reality Anchor 的结果永不持久化。
- [x] 全量恢复测试证明 checkpoint 续跑不会改变 context fingerprint。

## 灰度与验收

- [ ] 先对只读、结构化输出节点开启，灰度比例 10%。
- [ ] 对照组与实验组使用同一模型、reasoning、DAG、输入和并发参数。
- [ ] 6/6 成功，Artifact 内容与基线一致。
- [ ] total tokens ≤ 70,000；若未达到 15% 降幅，停止继续复杂化方案。
- [ ] 墙钟 ≤ 65 秒；p95 单节点延迟不得增加超过 15%。
- [ ] 未知成本仍显示 unknown；不得用 token 降低推导虚假美元收益。
- [ ] 连续 30 个验证成功结果无质量回归后再默认开启。

## 风险与停止条件

- 上下文不足导致模型猜测：出现一次误报完成即关闭对应裁剪规则。
- 指纹遗漏关键输入导致错误复用：发生冲突时禁用该任务类型复用并扩充 key。
- provider cache 口径不同：保留原始分项和 completeness，不做跨 provider 强行统一。
- 如果降本主要来自输出变短但验证质量下降，则判定无收益。

## 完成定义

- [x] 上下文复杂度集中在一个深模块，Adapter 没有新增裁剪分支。
- [ ] 达成 token 退出目标，且质量、恢复、安全边界均不下降。
- [x] 结果可由基准工具复算，文档中不依赖手工挑选的成功样本。
