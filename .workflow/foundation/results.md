# Foundation 验收结果

## 结论

Graph Engineering 基础阶段已完成。当前交付是一个 Python 3.9 标准库实现的本地内核，
不绑定 Codex、Orca 或其他具体 Agent 产品，可作为后续适配层的稳定执行语义。

## 已补齐

- GraphSpec、DAG 和声明式读写契约。
- 循环、缺失依赖、无上游读取、并发写冲突和 verifier 拓扑静态检查。
- 所有终点路径的 Reality Anchor 覆盖校验。
- 不可变 Artifact、版本、SHA-256 校验和恢复时完整性检查。
- 有并发上限的依赖调度、失败传播、选择性重试、预算和 gate。
- JSONL 事件、原子 checkpoint、GraphSpec 指纹和跳过已完成节点的恢复。
- Worker 类型运行前预检、CLI、最小示例和 18 项单元测试。

## 验证记录

```text
PYTHONPATH=. python3 -m unittest discover -s tests -v
Ran 18 tests in 0.009s — OK

PYTHONPATH=. python3 -m grapheng.cli validate examples/minimal_graph.json
{"graph_id": "grounded-report", "valid": true, "nodes": 3}

PYTHONPATH=. python3 -m grapheng.cli demo examples/minimal_graph.json --work-dir <temp-dir>
success=true, tokens_used=35
```

事件记录已确认包含 `run_id / graph_id / node_id / attempt`；checkpoint 已确认包含节点状态、
尝试次数、token 用量，以及带 producer、version、checksum 的 Artifact。

## 尚未包含

- Codex / Orca Worker 适配器。
- 图运行控制面、审批 UI 和事件回放视图。
- 分布式执行、远程 Artifact Store、身份权限和 schema 迁移。

这些内容已拆入 plan.md 的 Phase 2—4，不阻塞基础内核独立运行。
