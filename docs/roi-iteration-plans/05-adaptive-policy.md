# ROI 计划 5：自适应调度与模型路由

## 结论

这是长期上限最高、短期置信度最低的计划。当前只有一组 Luna 六节点实测，数据不足以安全决定“哪个节点该用哪个模型、并发多少、上下文多少、验证多强”。过早自适应容易把随机波动学成策略。

预计投入 4–8 人周。只有计划 3 已积累至少 30 个验证成功结果，且计划 1、2 已稳定后才启动。首轮目标是在质量不下降的前提下，让 verified-result token 或墙钟再下降 10%–25%。

当前决策（2026-10-08）：尚无至少 30 个真实 verified results，也没有两个执行器各 10 个可比结果。按数据准入规则不定义候选 profile、不做离线策略回放或影子路由，避免用合成样本替代真实收益证据；本轮保持固定策略，先实施 G 的证据基础与 H 的检查收据。

## 模块与 seam

建立深模块 `ExecutionPolicy`。它接收规范化任务特征、已认证执行器能力、预算、历史分位数和不可放宽约束，输出一份可解释的执行 profile：

```text
decide(task_features, constraints, evidence) -> ExecutionProfile
```

`ExecutionProfile` 只能在已批准范围内选择模型、执行器、并发、context budget、retry 和 verification profile。GraphRuntime、Adapter 和 Resident 不复制策略判断，只执行并记录决策。

RSI 只能提出候选 policy；冻结评测、人工批准、灰度和回滚仍由治理模块控制。

## AgentDiscover 借鉴与取舍

2026-10-08 对照用户提供的资料、AgentDiscover 官方 README 与源码快照
[`9fc5703`](https://github.com/mhdfb/AgentDiscover/tree/9fc570321cc5bc585cdd535ac175f6d2fa8e5887)
后，吸收以下机制。它面向有明确评分器的开放问题搜索；首轮已实施的证据投影与检查收据见下文，
其余机制仍为待实施设计，不改变上述真实样本门槛。论文正文未完成有效核对，README
报告的特定任务收益不能直接外推为 Agent OS 的成本收益。

| 可借鉴机制 | 当前 Agent OS 接入点 | 待补增量与边界 |
| --- | --- | --- |
| 每次尝试关联候选、父候选、想法和指导 | [`PublicationCandidate`](../../grapheng/publication.py)、[`ArtifactStore`](../../grapheng/artifacts.py)、[`RSICandidate`](../../grapheng/learning.py)、[`OptimizationCandidate`](../../grapheng/optimization.py) | 已有产物和策略候选；补跨尝试的派生、失败与指导关系，引用已有 ID，不另建候选事实源。 |
| Agent 自主查历史、选实验、决定何时提交 | [`ContextCompiler`](../../grapheng/context_compiler.py)、已有 Agent 节点 | 只在批准的节点职责、工具、候选数和预算内探索；新任务依赖、写范围或预算变化仍需重新校验和批准。 |
| 独立评分器评估固定候选 | [`VerifiedResultPublisher`](../../grapheng/publication.py)、[`RSIOptimizationLab`](../../grapheng/optimization.py)、[`EvaluationLab`](../../grapheng/evaluation.py) | 调用方测量的候选、评估器与数据版本绑定已补齐，仍不是独立执行证明；候选生成者不能改独立验证规则，单一 fitness 不替代质量与安全门槛。 |
| 分批执行后由元代理更新指导 | `RSIOptimizationLab`、未来 `ExecutionPolicy` | 在已完成批次的安全边界提出版本化指导候选；已有任务继续使用原批准版本，更新仍走评估、批准、灰度和回滚。 |
| 检索和工具使用关系来自运行日志 | [`events.py`](../../grapheng/events.py)、`ContextCompiler`、[`RunEconomics`](../../grapheng/economics.py) | 区分 Agent 声称采用的想法与运行时观察到的检索/注入；工具返回不代表 Agent 理解或采纳。 |

经验关系描述“尝试如何演化”，与执行依赖 DAG、codebase-memory 的源码索引分别承担不同职责。
优先用现有版本化本地记录表达关系；确有查询瓶颈时再使用标准库索引。`ProjectionIndex`
仍只是可重建缓存，不能成为经验、评估或审批的事实源。

以下内容暂不引入：必需的 Neo4j/MCP 常驻服务、多容器后端矩阵、HPC 调度、元代理生成任意
Python/Shell 工具后直接执行。先把结构化指导与受限检索接到已有 seam；可执行扩展仍按
0.8 的权限、来源、兼容与沙箱门槛处理。固定 provider 的五小时窗口和自动增长超时也不
作为通用默认值，继续由 provider 状态和批准的硬预算约束。

独立评估不等于 OS 隔离。当前 Adapter 的安全参数不能证明网络或主机隔离，边界见
[威胁模型](../threat-model.md)。将来需要运行不可信候选时，必须先具备可验证的外部隔离，
才能接入无网络评估流程。

## 首轮实施与验证（2026-10-08）

本轮完成证据基础切片，ROI 计划 5 整体仍未达到完成定义。单元测试、CLI 模拟运行和
此前双模型探索不计入真实收益样本；执行器 CLI 认证也不能替代可比任务结果。

| 范围 | 已落地行为 | 尚未完成的边界 |
| --- | --- | --- |
| G：证据投影与检索 | [`EvidenceProjection`](../../grapheng/evidence.py) 从 events、publications、engineering、learning、optimization 读取现有事实。稳定 attempt ID 保留失败与重试，publication 补充候选和验证引用，关系拒绝缺失父项、跨 scope 和派生环。 | 普通节点事件尚未自动绑定所有候选、修复关系和指导版本；父项与指导需要宿主显式关联。 |
| G：来源与上下文 | 候选声明保持 `agent_claim`；运行时检索、成功编译后的上下文注入分别留收据。按 scope、任务类别、policy digest、有效期及条数/字节上限检索；无匹配时回退当前源码与固定策略。 | 过滤依赖宿主提供的资格元数据，尚未接入完整执行器认证/capability 资格判断或 `ExecutionPolicy`。检索、注入不代表 Agent 理解或采纳。 |
| G：本地控制与导出 | `show/link/correct/revoke/forget/export` 可查看和控制复用。更正仅留摘要与历史链引用并使原项退出复用；撤销/遗忘使父项后代、相关指导使用及缓存失效。导出采用允许字段清单。 | 更正尚未形成新的可复用替代候选；控制历史不能替代原始执行事实。 |
| H：检查收据 | [`EngineeringWorkflow`](../../grapheng/engineering.py) 每条检查启动前落盘，终态逐条落盘，暂停/恢复保留历史。收据绑定工作区候选、输入、命令集合/单条命令、评估器版本、超时资源、plan 与 policy 摘要；读取时校验绑定。 | 项目检查明确为 `independent=False`，尚无独立冻结脚本、数据、阈值及外部隔离，因此 H 不能整体勾选。 |
| H：异常与总消耗基础 | timeout、invalid、harness_error、interrupted 分开记录；检查或 review 改变候选时拒绝成功。运行时调用前保存预算预留，SIGKILL 后恢复保守计账且保持 unknown；已完成副作用收据精确结算一次。 | 尚未汇总整个搜索/元指导流程的 objective 与全部消耗；预留记账不等于实测费用。 |
| A–F、I 与收益验收 | 继续执行现有固定策略。 | 等待至少 30 个真实 verified results、三个任务类别与两个认证执行器各 10 个可比结果，再评估策略、指导反馈和灰度收益。 |

CLI 已提供 `evidence show/search/export/link/correct/revoke/forget`。示例中的来源、scope、
资格与有效期由宿主指定，命令不会创建或激活策略：

```bash
python3 -m grapheng.cli evidence search \
  --source events /path/to/run/events.jsonl \
  --scope project-a --task-type fix --policy-digest approved-policy \
  --expires-at 1798761600 --max-items 10 --max-bytes 8192 \
  --controls /path/to/local/evidence-controls.jsonl \
  --access-log /path/to/local/evidence-access.jsonl
```

本轮验证使用项目 `.venv`，未调用外部模型或增加依赖：

```text
.venv/bin/python -m pytest -q
492 passed, 79 subtests passed

.venv/bin/python -m mypy grapheng
Success: no issues found in 54 source files

.venv/bin/python -m ruff check grapheng tests
All checks passed!

PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache .venv/bin/python -m compileall -q grapheng tests
exit 0
```

新增验收位于 [`test_evidence.py`](../../tests/test_evidence.py)、
[`test_engineering_evidence.py`](../../tests/test_engineering_evidence.py) 和
[`test_runtime_accounting.py`](../../tests/test_runtime_accounting.py)。全量回归后加强了指纹反例与
调用前落盘/旧 checkpoint 恢复断言，相关定向测试通过。`/tmp` 副本分别撤回指纹分帧、
声明来源限制、旧预留未知标记、调用前落盘和结果绑定校验，均触发对应测试失败；
调用前落盘变异同时触发两个 SIGKILL 子用例，其余 24 项保持通过。用户批准修正的认证
日期断言也在返回旧认证记录的变异下失败，其余 17 项 distribution 测试通过。
CLI 模拟运行验证了两次尝试的查看、限额检索、脱敏导出、关联和遗忘传播，源日志字节未变。

## 续轮实施与验证（2026-10-08）

在首轮基础上补齐以下行为。上表保留首轮交付时的边界，当前进度以下表为准；
G/H 整体和真实收益验收仍未完成。

| 范围 | 新增行为 | 剩余边界 |
| --- | --- | --- |
| G：执行事实关联 | Agent 节点完成事件自动绑定已提交产物摘要与实际执行器；同节点重试自动绑定上一尝试。events 与 publications 合并时拒绝候选、执行器或来源资格冲突。 | 普通节点不冒充 Agent 候选；跨任务派生和指导仍需宿主关联，尚未接入元指导反馈。 |
| G：上下文资格 | `ContextCompiler` 注入前重新检查当前版本/平台/协议认证、能力、工具、数据级别及静态费用/延迟约束，并与来源版本、scope、任务类别、policy 和有效期匹配；缓存命中也重新认证。资格缺失或漂移时回退当前源码/固定策略。 | 资格来源由宿主提供；检查不预留预算或执行模型，动态 provider admission 仍在实际执行时完成。检索/注入不能证明 Agent 采纳。 |
| G：更正与撤回 | 更正可以引用同 scope、任务类别和 policy 下已有的 verified runtime candidate；保留原始事实与摘要历史，拒绝未验证替代项和父项/更正混合环。撤销/遗忘穿过更正链及后代，使后续证据检索失效；仅提供替代项却省略原项时拒绝读取，防止来源裁剪恢复已遗忘项。 | 不从自由文本更正生成候选；本地复用控制还不是完整个人/项目记忆，也不删除原始执行事实或历史账目。 |
| H：已提供来源汇总 | `evidence summary` 汇总所有已提供来源的尝试，包括失败、中断和已撤回项；合并同一次执行，项目检查子记录不重复计账。缺失用量保持 unknown，预算预留和旧 publication 数字不冒充完整实测。 | `coverage=supplied_execution_sources`、`roi_admission=not_evaluated`；尚无整个搜索/元指导过程的墙钟、objective 和真实样本准入统计。项目检查仍为 `independent=False`，独立冻结评估与外部隔离未实现。 |

CLI 新增 `summary`、`--replacement-attempt-id` 和资格检索参数。`search` 未提供
`--request` 时仅查看并按宿主元数据筛选；上下文注入始终携带请求做资格检查。
下面的请求 JSON 是宿主提供的 `AgentRequest`（包含 task_id、prompt、inputs、output_keys、
workspace 与对应的 task_type/reuse_scope）；来源数据级别、执行器版本与有效期也由宿主指定，
`VERSION` 应来自该历史运行的认证记录：

```bash
python3 -m grapheng.cli evidence search \
  --source events /path/to/run/events.jsonl \
  --source publications /path/to/run/verified-publications.json \
  --scope project-a --task-type fix --policy-digest approved-policy \
  --expires-at 1798761600 --data-classification public \
  --executor-version codex VERSION --request /path/to/host-request.json \
  --max-items 10 --max-bytes 8192 \
  --controls /path/to/local/evidence-controls.jsonl \
  --access-log /path/to/local/evidence-access.jsonl

python3 -m grapheng.cli evidence summary \
  --source events /path/to/run/events.jsonl \
  --source publications /path/to/run/verified-publications.json \
  --scope project-a --controls /path/to/local/evidence-controls.jsonl
```

续轮新增验收位于 [`test_evidence_reuse.py`](../../tests/test_evidence_reuse.py)，
覆盖当前认证失效、版本和来源资格漂移、权限/预算拒绝、更正链撤回、混合关系环、
CLI 输入错误、自动执行事实绑定及未知用量汇总。检查只使用本地模拟执行器和 help/version
探针，未调用外部模型或增加依赖；这些结果不计入真实收益样本。

```text
.venv/bin/python -m pytest -q
514 passed, 117 subtests passed

.venv/bin/python -m mypy grapheng
Success: no issues found in 54 source files

.venv/bin/python -m ruff check grapheng tests
All checks passed!

PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache .venv/bin/python -m compileall -q grapheng tests
exit 0
```

在 `/tmp` 副本分别撤回能力/静态策略/scope 检查、当前认证、来源资格缓存键、候选绑定冲突、
字节上限、更正链传播与直接父项例外、替代项验证、关系环、自动父项及执行器绑定、
撤回项记账、unknown 标记、旧 publication 完整度和检查子项去重、CLI 类型校验，
共 18 项变异均触发目标用例失败，其他无关用例保持通过。省略更正原项的新用例也先在
未修正实现上精确失败，再通过最终全量回归；这些检查不改仓库中的正确实现或原始运行事实。

## 失败消耗与运行跨度增量（2026-10-08）

本次继续补 H 的消耗记录，不扩大到策略生成或元指导。失败与重试事件记录运行时已结算的
token/费用；调用未返回实测值时使用原有保守预留，并保持完整度为 false。预算拒绝保留
已返回的用量，不生成未提交产物的候选摘要。结算后发生提交或发布异常时复用已结算口径，
不重复扣账，也不自动重试已进入提交阶段的尝试。旧日志缺少用量时仍为 unknown。

`evidence summary` 新增 `wall_clock`。`aggregation=sum_per_run_spans` 表示各运行跨度的和，
不代表多个并行运行共同的批次 elapsed。`known_seconds` 只累计边界完整的运行；
`known_runs`、`unknown_runs` 与 `complete` 保留覆盖缺口，全未知时数值为 null，
空来源为零。按 scope/run 去重，重复来源、publication 附加信息及检查子收据不重复计时，
撤销与遗忘也不删除历史消耗。

普通运行的口径为 `run_start_to_execution_completion`：从原始 `run_started` 到实际执行的
终态，包含中断等待与恢复。失败终态之后有新节点活动时，需要新的 `run_completed`；
仅重放已完成检查点时不延长原跨度。只有 `run_resumed`、缺少终态、单独 publication
或候选声明时保持 unknown。多个来源中出现比旧终态更晚的未完成活动时同样保持 unknown；
来源边界冲突、倒置时间、无时区或无效时间被拒绝。

工程口径为 `preparation_plus_plan_to_finish`：已记录的准备耗时加上 `plan.created_at`
到终态 `finished_at` 的间隔，包含审批、暂停与恢复等待。它不使用恢复后重置的
`report.started_at` 充当全流程起点，也不把检查耗时相加。暂停或缺少终态报告时保持
unknown。原始时间字段留在本地，既有脱敏导出允许字段清单不变。

新增验收位于 [`test_evidence_totals.py`](../../tests/test_evidence_totals.py)，覆盖账本一致性、
预算拒绝、结算后的异常、旧记录、并发/重试/恢复、分段与重复来源、撤回、时间校验、
publication 去重以及工程准备与暂停等待。未调用外部模型或新增依赖：

```text
.venv/bin/python -m pytest -q
534 passed, 144 subtests passed

.venv/bin/python -m mypy grapheng
Success: no issues found in 54 source files

.venv/bin/python -m ruff check grapheng tests
All checks passed!

PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache .venv/bin/python -m compileall -q grapheng tests
exit 0
```

反向验证发现，原有无时区反例在撤回时区检查后仍会被倒置时间规则拒绝，因此补充了
正跨度的无时区输入，保持原断言。补强后的时间边界定向检查通过（1 passed、8 subtests
passed）。在 `/tmp` 副本分别撤回用量记录/完整度、预算拒绝记录、结算后保护、事件投影、
去重与 scope 身份、恢复终态、未知标记、时间约束和工程跨度口径，共 22 项变异均触发
目标验收失败，无关对照用例继续通过。

CLI 本地重试运行汇总与运行时账本相等：7 tokens、约 $0.30，token/费用完整度均为 false，
墙钟为一个已知运行跨度。这些验证不计入真实收益样本；检索、元指导和独立冻结评估的
完整搜索消耗仍未接通，因此 H 的总消耗项与整体完成状态均不勾选。

## 优化候选的评估绑定增量（2026-10-08）

本次继续补 H 的本地绑定，沿用 [`RSIOptimizationLab`](../../grapheng/optimization.py) 的
提示/拓扑候选、冻结基准与评估接口，不启动策略实验。评估保存版本化 binding，绑定候选
ID、类型、改动与灰度比例、suite 及其 fixture 摘要、评估器版本、规范化测量集合、阈值与
固定治理约束。测量按冻结 case 顺序保存，输入顺序不影响测量摘要；缺失或重复 case 被拒绝。
suite 重用时也重新校验实际 case，不能只信任文件中保存的 digest。

批准、激活、应用、canary 与历史恢复都会重新校验 suite，并从绑定输入重算评估结果。
聚合数字或 passed 字段不能单独改写；固定约束、来源标记及当前评估器版本也必须匹配。
批准保存 `approved_evaluation_digest`，此后替换成另一份完整合法评估仍需重新批准，
不能沿用旧审批。相同改动的另一候选不能复制使用这份收据。

回滚先复核最近历史项。历史项缺失、损坏、漂移或缺少新绑定时，撤下当前候选，退回固定
策略，保留更早历史而不自动跨项恢复；有效历史项仍按原流程恢复。拒绝原因写入本地
`rollback.jsonl`，该运行记录不随 Agent OS 状态导出。候选评估与审批绑定仍可导出、恢复。
旧候选可通过 `candidates()` 或证据投影读取，但缺少新绑定时不得自动批准、激活或应用；
需要按当前接口重新提出并评估候选。带失效活动候选的状态包也不会自动恢复为可用策略。

这些绑定提供本地完整性与批准版本校验。测量来源明确为 `caller_reported`、
`independent=False`，调用方提供的 Reality Anchor 布尔值不升级成独立运行证据。
评估器只比较已有数字，`candidate_execution=not_performed`；隔离为 unknown，CPU、
内存、执行墙钟和输出限额为 null。尚未冻结独立执行脚本/参考答案，也没有外部隔离或
认证执行器的实测来源证明，因此 H 第一项及整体仍不勾选。

新增验收位于 [`test_optimization_binding.py`](../../tests/test_optimization_binding.py)，
覆盖漂移拒绝、评估重算、审批错配、旧记录、来源限制、回滚恢复、CLI 与状态迁移。
未调用外部模型或新增依赖：

```text
.venv/bin/python -m pytest -q
556 passed, 194 subtests passed

.venv/bin/python -m mypy grapheng
Success: no issues found in 54 source files

.venv/bin/python -m ruff check grapheng tests
All checks passed!

PYTHONPYCACHEPREFIX=/tmp/agent-os-pycache .venv/bin/python -m compileall -q grapheng tests
exit 0
```

在 `/tmp` 副本撤回 suite 复核、各阶段绑定校验、评估重算、审批锁定、候选与灰度摘要、
来源/独立性/未知资源标记、输入排序、固定约束与回滚回退，共 21 项变异触发目标验收失败，
无关对照继续通过；状态导出的日志排除与排除清单两项变异也触发目标失败，合计 23 项。
真实 CLI 本地模拟验证冻结、评估、批准、激活与查看，随后确认 suite 漂移被拒绝、旧历史项
回滚到固定策略。图谱尚未索引本项目，调用链按当前源码和定向引用核对；这些验证不计入
真实收益样本，不改变数据准入门槛。

## 数据准入清单

- [ ] 至少 30 个验证成功结果，覆盖 fix/test/refactor 中至少 3 类。
- [ ] 至少两个已认证执行器，每个不少于 10 个可比结果。
- [ ] token 分项完整率足够计算候选收益；成本不完整时只优化 token/时间。
- [ ] 失败、人工介入和修复循环样本同时保留，不做幸存者偏差数据集。
- [ ] 任务特征脱敏，不保存 prompt、路径和原始响应。

## 实施清单

### A. 定义保守 profile

- [ ] 先定义少量离散 profile，不让策略直接生成任意参数。
- [ ] 例如：`cheap-readonly`、`balanced-engineering`、`high-assurance-review`。
- [ ] 每个 profile 明确模型/执行器资格、context budget、max concurrency、retry 和验证强度。
- [ ] 安全、数据级别、工具权限、Reality Anchor 和用户预算是硬约束，不参与收益权衡。

### B. 离线候选评估

- [ ] 使用计划 3 的冻结基准回放候选 policy。
- [ ] 目标函数按顺序比较：验证成功率、安全违规、人工介入、费用/token、墙钟。
- [ ] 质量和安全先作为门槛，不与费用做加权抵消。
- [ ] 样本不足的任务类型回退固定 profile，不外推。
- [ ] 每个候选输出“为何选择、使用了哪些证据、置信度和回退 profile”。

### C. 自适应并发

- [ ] 以 critical-path rank、ready width、provider 限流和资源 reservation 决定并发上限。
- [ ] 并发只在 GraphSpec 和 Resident 容量上限内调整。
- [ ] 已学习拓扑不得增加声明的并发上限，除非用户明确批准新预算。
- [ ] 关键链优先不变；增加并发不能让预算 admission 失效。

### D. 模型与执行器路由

- [ ] 先对纯读取、结构化提取、低风险节点尝试更低成本 profile。
- [ ] 实现/合并/安全审查继续使用高保障 profile，直到有足够反证。
- [ ] 熔断、限流、版本认证和 capability 是路由硬条件。
- [ ] provider 不返回成本时，路由依据 token、延迟和验证结果，不猜价格。

### E. 上下文与验证强度

- [ ] 只从计划 1 已验证的 context budget 集合中选择。
- [ ] 验证强度可增加，不得低于任务风险等级的最低门槛。
- [ ] 发生修复循环、人工纠正或 Reality Anchor 失败时，当前 run 立即升级 profile。
- [ ] 升级原因进入事件和结果报告，后续可审计。

### F. 灰度与回滚

- [ ] 候选 policy 有版本、摘要、适用范围、过期时间和回滚目标。
- [ ] 先影子决策，不改变执行；比较实际固定 profile 与建议 profile。
- [ ] 通过离线门槛后仅 10% canary，按任务类型分层。
- [ ] 任一安全违规、误报完成或验证成功率显著下降时自动回滚。
- [ ] 用户可查看、禁用和撤销任何学习策略。

### G. 尝试与经验的证据关系

- [x] 第一切片只补已有候选/报告的只读证据投影，再接检索；样本门槛满足前不生成或激活策略。
- [ ] 每次尝试绑定稳定 attempt ID、run/node、候选摘要、父尝试 ID、指导版本和评估引用；关系校验缺失父项、跨作用域引用和派生环。
- [ ] 成功、失败、超时、被拒候选都保留；失败类别与修复关系可查询，不只检索高分结果。
- [x] 想法、预测、经验结论标为 Agent 声明；验证结果、实际检索/上下文注入标为运行时证据。来源不能由 Agent 自报升级。
- [x] 跨任务检索按任务类别、策略资格与证据有效期过滤，限制条数和上下文字节；陈旧或不兼容证据回退当前源码/固定策略。
- [x] 原始轨迹、代码、项目路径和自由文本指导留在本地任务运行态。可迁移状态只接受明确允许的脱敏特征、指标和版本化策略，不复制 AgentDiscover 的完整 transcript/genome 存储方式。
- [x] 经验支持查看、纠正、撤销和遗忘；更正引用原版本，遗忘同时使检索投影和后续复用失效，不改写既有执行事实。

### H. 绑定候选的独立评估

- [ ] 每条评估绑定候选摘要、冻结 suite、评估器版本、输入/数据摘要、资源约束与策略摘要；任一项变化后旧结果不得自动复用。
- [ ] 区分生成阶段与评估阶段；生成者不能写独立评估边界内的冻结脚本、参考答案、通过阈值或测试数据。任务要求新增的项目测试仍属实现产物，不能替代独立冻结回归。自述成功与预测分数不能充当 Reality Anchor。
- [ ] 性能优化等有明确目标的任务同时记录 objective 名称、方向、单位和原始值；只有通过质量、安全和 Reality Anchor 后才比较收益。
- [ ] timeout、invalid、harness error 独立记录，不把未评估或解析失败写成零成本、已完成或普通低分成功。
- [ ] 不可信候选仅在确认可用的外部隔离中评估：无网络、受限挂载、无 Agent 凭据、明确 CPU/内存/时间/输出上限；隔离不可用时停止此类评估。
- [ ] 生成、检索、元指导和评估都计入总 token、费用与墙钟；成本缺失时继续保持 unknown，不用单次最好分数掩盖搜索总成本。

### I. 有上限的元指导反馈

- [ ] 仅在配置的完成批次与最低证据量满足后提议指导，记录其来源尝试、适用范围、版本、摘要和替代关系；失败或超时的批次也纳入统计。
- [ ] 指导先限制为结构化策略参数或已有模板片段，不生成并自动执行任意宿主脚本，也不动态修改正在执行的 DAG。
- [ ] 下一批获批准的执行在安全检查点锁定新版本；已有任务及其中断恢复继续使用已绑定的指导与评估版本，避免同一尝试混用两个版本。
- [ ] 搜索在执行前声明最大候选数、轮数、总预算、墙钟和无改进停止条件；元代理调用也受预算 admission 管理，不能自行延长超时或扩大配额。
- [ ] 元指导失败、预算不足或证据不足时保留原固定策略；候选能改善哪些任务必须通过冻结回归与灰度证明。

## 测试清单

- [ ] 相同输入和证据得到确定的 profile 与解释。
- [ ] 未认证 Adapter、权限不匹配和超预算 profile 永不入选。
- [ ] 缺失或陈旧证据时回退固定 profile。
- [ ] 冻结基准覆盖候选通过、拒绝、灰度和回滚。
- [ ] 变异测试：放宽安全门槛后，治理测试必须失败。
- [ ] 变异测试：允许拓扑提高并发上限后，预算测试必须失败。
- [ ] 历史策略和旧任务状态仍可读取，缺失 policy 字段使用默认 profile。
- [ ] 成功与失败尝试均可追溯到候选、父项、指导和评估版本，重复恢复不会产生重复记录。
- [x] Agent 声明不能伪造运行时检索、验证或可信经验；跨作用域关系与派生环被拒绝。
- [ ] 候选、评估器、数据或策略改变时，旧评估无法被复用；评估脚本被候选篡改时拒绝完成。
- [ ] 超时、无效输出与评估器故障不会成为验证成功样本或进入持久复用。
- [ ] 指导更新仅影响获批准的新执行，恢复不会混用版本；元指导失败和耗尽预算正确回退或停止。
- [x] 遗忘经验后相关检索缓存失效，状态导出不包含原始轨迹、代码、自由文本指导或路径。

## 验收指标

- [ ] 验证成功率不低于固定 profile 的置信区间下界。
- [ ] 误报完成、安全违规、不可追溯副作用均为零。
- [ ] verified-result token 或估算/实测成本下降至少 10%。
- [ ] 或在成本不增加超过 5% 时，墙钟下降至少 15%。
- [ ] 人工介入不增加，修复循环 p95 不恶化。
- [ ] 100% 决策可解释、可复现、可回滚。

## 风险与停止条件

- 样本偏差：不足 30 个 verified results 或任务分布单一时不启动。
- 策略震荡：profile 有最短稳定期，不按单次结果更新。
- 优化代理指标：只以 verified result 为单位，拒绝“调用更便宜但失败更多”。
- 复杂度超过收益：若首轮 canary 收益低于 10%，保留固定 profile 并停止扩展策略空间。

## 完成定义

- [ ] 策略复杂度集中在 `ExecutionPolicy` 深模块，运行时只消费 profile。
- [ ] 所有候选经过冻结评测、人工批准、灰度和自动回滚链路。
- [ ] 收益来自真实验证结果，不来自离线模拟或挑选样本。

## 借鉴来源

- [官方 README](https://github.com/mhdfb/AgentDiscover/blob/9fc570321cc5bc585cdd535ac175f6d2fa8e5887/README.md)：自主搜索的职责与实验适用范围。
- [图 schema](https://github.com/mhdfb/AgentDiscover/blob/9fc570321cc5bc585cdd535ac175f6d2fa8e5887/schema.cypher)：候选派生、指导替代、声明与日志事实的区分。
- [搜索工具](https://github.com/mhdfb/AgentDiscover/blob/9fc570321cc5bc585cdd535ac175f6d2fa8e5887/agentdiscover/server/tools_search.py)：只读图查询、候选提交与评分反馈。
- [评估器](https://github.com/mhdfb/AgentDiscover/blob/9fc570321cc5bc585cdd535ac175f6d2fa8e5887/agentdiscover/server/evaluator.py)：固定候选文件、无网络评估及超时/故障结果。
- [元指导](https://github.com/mhdfb/AgentDiscover/blob/9fc570321cc5bc585cdd535ac175f6d2fa8e5887/agentdiscover/orchestrator/meta.py)与[动态工具](https://github.com/mhdfb/AgentDiscover/blob/9fc570321cc5bc585cdd535ac175f6d2fa8e5887/agentdiscover/server/dynamic_tools.py)：分批反馈和按分支提供工具；本文仅吸收受限指导机制。
