# ROI 计划 4：Resident 安全多任务并发

## 结论

GraphRuntime 已证明 DAG 内并发有效，但 Resident 仍一次 claim/execute 一个顶层 job。扩大顶层并发可以显著降低排队时间，却会同时放大重复执行、workspace 冲突、预算超卖和恢复竞态风险。因此本计划 ROI 潜力高、实现成本也高，只有真实队列压力达到阈值后才启动。

预计投入 3–5 人周。第一阶段只支持两个互相独立的 job 并行，目标吞吐提升 1.7× 以上，重复副作用保持为零。

当前决策（2026-09-15）：未发现连续一周 `active_jobs >= 2`、p95 queue wait 超过 30 秒或用户明确并行需求的证据。按停止条件保持 `max_workers=1`，本轮不实施 SQLite 队列、worker 池或并发灰度；这不是待补的离线编码项。

## 启动门槛

满足任一条件才进入实现：

- [ ] 连续一周每日存在至少两个同时 active 的 job。
- [ ] p95 queue wait > 30 秒，且主要等待来自 Resident 串行而非模型限流。
- [ ] 用户明确需要多个项目后台并行，并接受相应资源预算。

未满足门槛时，计划 1 和计划 2 的 ROI 更高。

## 模块与 seam

建立深模块 `ResidentQueue`，让 `ResidentCoordinator` 继续通过小接口使用：

```text
submit(job)
claim(worker, capacity) -> Lease | None
renew(lease)
settle(lease, result)
control(job, action)
snapshot(query)
```

SQLite 是本地持久化 Adapter，测试使用临时 SQLite，而不是另造一个行为不同的内存队列。claim、lease、控制意图和状态转换都在模块内部；调用方不接触事务和锁。

## 实施清单

### A. 持久队列与迁移

- [ ] 设计 SQLite schema：jobs、leases、control intents、events 和 schema metadata。
- [ ] 使用事务原子完成“选择候选 + claim + lease”，禁止先读后写竞态。
- [ ] 从 `queue.json` 单向迁移，保留 job ID、kind、reference、priority、sequence、attempts 和 requested_action。
- [ ] 迁移前保留可恢复备份；迁移失败继续使用旧队列，不产生半迁移状态。
- [ ] terminal 历史从 active selection 中分离，避免队列随历史无限扫描。

### B. Lease 与崩溃恢复

- [ ] lease 包含 worker ID、token、claimed_at、renewed_at 和 expires_at。
- [ ] settle/renew 必须携带 lease token，陈旧 worker 不能覆盖新 owner。
- [ ] worker 崩溃后只在 lease 到期且 handler reconciliation 未发现终态时重新排队。
- [ ] 对不确定副作用继续依赖 Effect Receipt，不因 lease 到期直接重放。
- [ ] cancel/pause 与 lease 状态有明确状态机，控制意图不会在重新 claim 时丢失。

### C. Workspace 与资源互斥

- [ ] 每个 job 声明 workspace identity；同一可写 workspace 默认互斥。
- [ ] 只读 job 可共享 workspace，但必须由策略显式确认。
- [ ] Orca 隔离 workspace 按真实 workspace ID 互斥，不只按 task ID。
- [ ] 增加全局 `max_workers`、每执行器并发上限和 token/cost reservation。
- [ ] provider 限流或熔断时降低 admission，不让更多 worker 放大失败。

### D. Worker 池

- [ ] 从 `max_workers=1` 保持兼容，灰度增加到 2。
- [ ] worker 只执行 lease 指定 job；队列模块负责公平性和控制优先级。
- [ ] 保留 priority `-100..100`、60 秒 aging 和 cancel_requested 抢占。
- [ ] 状态中心显示 running worker、queue wait、lease age 和资源 reservation。
- [ ] shutdown 等待安全检查点，超时后留下可恢复 lease，不假装任务已取消。

### E. 公平性与背压

- [ ] 跨 kind 共享公平队列，不让长工程任务永久饿死 graph/orca 或反之。
- [ ] 资源不足时保持 queued，不把 admission 失败写成任务失败。
- [ ] 为每 workspace、executor 和任务类型提供内部容量桶，但不扩张公共 CLI 参数。
- [ ] 连续失败任务进入退避，不占满全部 worker。

## 并发故障矩阵

| 场景 | 必须结果 |
| --- | --- |
| 两 worker 同时 claim | 同一 job 最多一个有效 lease |
| owner 崩溃 | lease 到期后可恢复，副作用不重复 |
| cancel 与 settle 竞争 | 只有一个合法终态，控制意图可审计 |
| 同 workspace 两写任务 | 串行执行 |
| 两个独立 workspace | 可并行 |
| provider 限流 | admission 收缩，不形成重试风暴 |
| 进程重启 | active lease 被校验、续租或安全回收 |

## 测试清单

- [ ] 多进程并发 claim 10,000 次，无重复 owner。
- [ ] lease 过期、续租、陈旧 settle、时钟边界和 PID 重用均有覆盖。
- [ ] JSON v2 到 SQLite 的迁移 fixture 覆盖所有控制态和 terminal 状态。
- [ ] workspace 互斥测试覆盖 symlink/路径规范化边界。
- [ ] 故障注入覆盖事务提交前后崩溃、fsync 失败和数据库 busy。
- [ ] 变异测试：移除 lease token 校验后，陈旧 settle 测试必须失败。
- [ ] 变异测试：移除 workspace mutex 后，冲突任务测试必须并发冲突。

## 灰度与验收

- [ ] 先在只读、不同 workspace 的 job 上启用 `max_workers=2`。
- [ ] 20 个独立等长 job 的吞吐相对 worker=1 提升至少 1.7×。
- [ ] 单 job p95 延迟增加不超过 10%。
- [ ] 重复执行、重复副作用和双重终态均为零。
- [ ] cancel/status p95 仍满足计划 2 的目标。
- [ ] 资源使用超过配置上限时 admission 正确收缩。
- [ ] 连续 100 个真实或受控 canary job 无恢复异常后再扩大任务类型。

## 风险与停止条件

- SQLite 迁移面过大：先以 queue module 替换存储，不同时重写 handler 生命周期。
- workspace identity 不可靠：任何无法确定 identity 的可写 job 继续串行。
- 吞吐提升被 provider 限流抵消：若 2 workers 的 verified throughput 提升低于 20%，保持默认 1。
- 并发导致 token/成本上升：只对有排队压力的 profile 开启，不全局默认。

## 完成定义

- [ ] 队列、lease、公平性和资源 admission 藏在一个深模块内。
- [ ] `ResidentCoordinator` 的外部接口和现有 CLI 保持兼容。
- [ ] 并发收益达到门槛，且没有用可靠性换吞吐。
