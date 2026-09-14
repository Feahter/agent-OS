# ROI 计划 3：经济性观测与可复现实验

## 结论

当前 token 已可完整复算，但 Luna run 的成本是 unknown。没有可信的 measured/estimated/unknown 区分，后续“省了多少钱”都无法审计。本计划的直接节省有限，但它能阻止错误投资，并为计划 1、4、5 提供共同的 ROI 判定基础。

预计投入 1–2 人周。前两天先交付最小基准切片，剩余部分可以与计划 1、2 并行。

## 模块与 seam

在现有 `ModelUsage` 和评测记录之上增加内部深模块 `RunEconomics`。它只接收结构化 usage、时间、验证结果和明确版本的价格表，输出一个不可歧义的经济性快照。

调用方只需要知道三种成本来源：

- `measured`：provider 明确返回。
- `estimated`：由操作者提供、带版本和生效时间的价格表计算。
- `unknown`：缺少完整条件。

价格估算是 Adapter 之外的实现，绝不修改 provider-reported `ModelUsage.cost_usd` 和 `cost_complete`。

## 实施清单

### A. 冻结基准协议

- [ ] 为基准定义 `benchmark_id`、场景版本、模型、reasoning、执行器版本、DAG fingerprint 和输入 fingerprint。
- [ ] 固定三档场景：微型六节点、典型工程任务、含恢复/重试的故障场景。
- [ ] 每档至少提供 concurrency 1/2 对照；计划 4 后再增加 4。
- [ ] 原始 prompt 和响应不进入基准索引，只保存脱敏指纹、计数和验证结果。
- [ ] 结果文件不可覆盖；重复运行产生新 run ID。

### B. 统一经济性快照

- [ ] 记录每节点和每 run 的 input/cache/output/total、完整性和来源。
- [ ] 记录 queue wait、model latency、critical-path latency、wall clock 和验证耗时。
- [ ] 记录 reuse hit/miss/coalesced/bypassed 及 saved tokens。
- [ ] 记录验证成功、修复循环、人工决策和恢复次数。
- [ ] 将“每个模型调用”与“每个验证成功结果”分开，默认展示后者。

### C. 可选价格目录

- [ ] 价格目录由操作者显式安装或更新，包含 provider、模型、input/cache/output 单价、币种、版本和生效时间。
- [ ] 缺少任一必需 token 分项时，估算成本标记 partial，不补零。
- [ ] provider measured cost 与 estimated cost 并列保存，不互相覆盖。
- [ ] 价格目录过期时输出告警；禁止静默抓取网络价格改变历史结果。
- [ ] 价格变化后旧 run 仍可按原目录复算，也可明确选择新目录做情景分析。

### D. ROI 报告

- [ ] 输出 baseline/candidate 的成功率、token、成本区间、墙钟、人工介入对比。
- [ ] 报告样本量、中位数、p95 和失败样本，不只展示最佳值。
- [ ] 当 cost unknown 时只报告 token ROI，不生成美元 ROI。
- [ ] 将 cache read 单独显示，避免把 provider cache 命中误算为上下文减少。
- [ ] 提供 JSON 输出供 RSI 离线评估，CLI 人类输出保持简洁。

## 基准矩阵

| 维度 | 最小集合 |
| --- | --- |
| 场景 | 微型 DAG、典型工程、恢复/重试 |
| 执行器 | Codex、Pi；其他已认证 Adapter 按需加入 |
| 并发 | 1、2 |
| 重复 | 每组合至少 5 次，跨两个时间窗口 |
| 结果 | success、verified、tokens、cost source、wall clock、human actions |

## 测试清单

- [ ] measured zero、estimated zero、unknown 三者序列化和 CLI 渲染可区分。
- [ ] token 分项不完整时，估算成本不会标 complete。
- [ ] cached input 在不同 provider 口径下不会被重复计入 total。
- [ ] 同一 run 使用同一价格目录可确定复算。
- [ ] 未验证成功的 run 不进入“每个验证成功结果”的分母。
- [ ] 变异测试：把 unknown 当零时，经济性契约测试必须失败。
- [ ] 兼容旧 checkpoint/report/task status，缺失新字段保守为 unknown。

## 验收指标

- [ ] 所有已认证 Adapter 的 total token 完整性可见；缺失项明确为 null/incomplete。
- [ ] 基准运行 100% 带场景和执行环境 fingerprint。
- [ ] measured/estimated/unknown 成本口径零混淆。
- [ ] 任意候选策略可生成一份包含失败样本的前后对照报告。
- [ ] Luna 六节点结果可从 events、checkpoint、result 三方复算一致。
- [ ] 经济性报告不持久化 prompt、原始响应、凭据和真实项目路径。

## 风险与停止条件

- 指标系统反过来增加热路径 I/O：采集放在锁外，支持批量落盘。
- 价格表维护变成隐性外部依赖：核心能力允许永远停留在 unknown，不阻塞任务。
- 指标太多导致无法决策：默认报告只保留成功率、verified-result token/cost、墙钟和人工介入。
- 若一个指标不能驱动计划 1、4、5 的接受/拒绝决策，就不进入默认看板。

## 完成定义

- [ ] `RunEconomics` 形成单一深模块，业务调用方不复制 completeness 算术。
- [ ] 基准可一条命令重复运行，结果不可覆盖、可复算。
- [ ] 至少一项计划 1 的候选优化用该报告做出接受或拒绝决策。
