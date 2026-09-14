# ROI 计划 2：Resident 热路径收口

## 结论

这是低风险、较快见效的工程 ROI 计划。Resident 已有优先级 aging、取消抢占和故障隔离，但实测审计仍发现三类热路径问题：waiting 探测没有退避、telemetry 写入位于队列锁内、Projection 缓存可能让控制状态与摘要短暂不一致。

预计投入 1–2 人周。不扩大 worker 并发，先让单 Resident 的控制面稳定、低 I/O、可预测，为后续安全并发扫清前置问题。

## 模块与 seam

保持 `ResidentCoordinator` 的公开接口不变。把持久队列选择、waiting 探测计划和状态投影下沉到两个内部深模块：

- `ResidentQueue`：负责原子读写、控制意图、优先级 aging、claim 和 settle。
- `JobProjection`：把 handler 状态、队列控制态和 usage 合成为一个一致快照。

telemetry 接收已经提交的事实，不参与队列事务。调用方不需要知道锁、JSON/SQLite、退避或缓存失效规则。

## 实施清单

### A. 先量化热路径

- [ ] 记录 `serve_once`、`_refresh_waiting`、claim、settle 的耗时分布。
- [ ] 记录每分钟 queue 写入、telemetry fsync、waiting inspect 次数和失败次数。
- [ ] 建立 10、100、1,000 个 job 的控制操作基准。
- [ ] 记录 `control/status/center` 的 p50/p95 延迟和锁等待时间。

### B. waiting 探测退避

- [ ] 为 waiting job 持久化 `probe_failures` 和 `next_probe_at`，并提供旧队列迁移。
- [ ] 首次等待保持当前响应速度；连续失败采用有上限的指数退避和小幅 jitter。
- [ ] 成功探测、人工 resume/cancel 或 handler 状态变化时重置退避。
- [ ] cancel_requested 永远绕过 waiting 退避并进入最高控制优先级。
- [ ] 守护进程休眠到最近到期时间或控制事件，不固定高频扫全量 waiting 集合。

建议默认参数先从以下区间灰度，不直接固化为公共接口：

| 参数 | 初始值 | 上限 |
| --- | ---: | ---: |
| 正常 waiting 探测间隔 | 1 秒 | 5 秒 |
| 失败退避基数 | 1 秒 | — |
| 失败退避上限 | 30 秒 | 60 秒 |
| jitter | ±10% | ±20% |

### C. 缩短队列锁临界区

- [ ] 锁内只完成校验、状态转换和原子持久化。
- [ ] 锁内生成 telemetry payload，释放锁后再 emit。
- [ ] handler `inspect/execute/record_failure`、通知和外部 CLI 永不在 queue lock 内运行。
- [ ] 对 emit 失败保持尽力语义，不能回滚已提交的队列状态。
- [ ] 增加两个控制线程并发 cancel/status 时的延迟回归。

### D. 修复投影一致性

- [ ] 定义状态优先级：队列控制态覆盖 handler 的陈旧摘要和 next action。
- [ ] pause/resume/cancel/reprioritize 后主动 `forget` 对应投影，或把控制版本纳入 fingerprint。
- [ ] `paused`、`cancel_requested` 等状态的 summary 与 next action 从同一快照计算。
- [ ] 缓存不可用时回退实时计算，但不得返回半新半旧字段。
- [ ] ProjectionIndex 的 close 生命周期接入 Resident 退出路径。

### E. 控制队列增长

- [ ] 记录 terminal job 数量和 queue 文件大小。
- [ ] 提供保守 prune 策略：只归档超过保留期且已有最终报告的 terminal job。
- [ ] 归档必须可审计，不删除任务自身的事实来源。
- [ ] 在进入计划 4 前证明 1,000 job 状态操作不会超线性恶化。

## 测试清单

- [ ] waiting inspect 连续失败不会杀死 daemon，也不会忙循环。
- [ ] 失败退避期间提交健康 job，健康 job仍能立即执行。
- [ ] cancel 在任意退避阶段都能抢占。
- [ ] telemetry journal 人为阻塞时，status/cancel 不等待 journal I/O。
- [ ] pause 后 `center` 不再出现“paused + ready to run”的组合。
- [ ] 旧 queue schema 迁移保留 priority、sequence、requested_action 和 attempts。
- [ ] 变异测试：移除退避判断后，探测次数上界断言必须失败。
- [ ] 变异测试：不失效投影后，控制态一致性断言必须失败。

## 验收指标

- [ ] 无工作时每分钟持久写次数降低至少 80%。
- [ ] waiting handler 持续失败时，单 job 每分钟 inspect 不超过 4 次。
- [ ] 100 job 下 `control/status` p95 < 100 ms；1,000 job 下 p95 < 250 ms。
- [ ] queue 锁内不出现 telemetry、通知、handler 或 subprocess 调用。
- [ ] 已知状态/摘要不一致复现归零。
- [ ] 既有优先级范围 `-100..100`、60 秒 aging 和取消抢占语义保持不变。

## 风险与停止条件

- 退避让外部终态发现变慢：正常 waiting 与失败 waiting 使用不同节奏，并保留事件唤醒入口。
- 缓存强制失效增加读取：先保证一致，再用命中率数据决定是否优化。
- 归档造成用户看不到历史任务：本计划只从调度队列移除，不删除 task/report/event 事实来源。
- 若在 100 job 下基线已经满足延迟和 I/O 目标，可缩减为一致性修复，不引入新 schema。

## 完成定义

- [ ] 热路径复杂度位于内部深模块，Resident 公共接口无新增参数负担。
- [ ] 性能和一致性指标均有前后对照。
- [ ] daemon 故障隔离、恢复、aging、取消抢占的现有行为不退化。
