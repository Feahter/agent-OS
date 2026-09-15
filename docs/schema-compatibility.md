# Schema 与最小公共 API 兼容性

Agent OS `0.0.x` 只冻结恢复所必需的持久化格式和少量操作者入口。未列入本页的 Python 对象、私有方法、内部字典字段与 CLI 人类可读输出仍属于 pre-alpha 内部实现，可在小版本中调整。

## 持久化 schema

所有已版本化文档只支持向当前版本前向迁移；不会把新版本数据降级写回旧格式。未知版本一律 fail-closed，避免旧程序误读未来语义。

| 文档 | 当前版本 | 可读取的旧版本 | 迁移与写回规则 |
| --- | ---: | --- | --- |
| Graph checkpoint | 1 | v0（无 `schema_version`） | 读取为 v1；下次 checkpoint 提交时写回 v1。 |
| Control Plane run state | 1 | v0（无 `schema_version`） | 读取为 v1；下次受锁保护的状态变更时写回 v1。 |
| Effect receipt | 1 | v0（无 `schema_version`） | 读取为 v1；下次 reconcile/reset/execute 写入时写回 v1。 |
| User task metadata/status | 1 | 无 | 只接受 v1；未知版本拒绝。 |
| Resident queue | 4 | v1、v2、v3 | 按 `v1 → v2 → v3 → v4` 顺序迁移，校验通过后立即原子写回 v4。 |

对应类型位于 `grapheng.schemas`；版本常量仍由各持久化边界的拥有模块执行校验。旧版与未知新版 fixture 由 `tests/test_schema_contracts.py` 以及已有 task/queue 迁移测试覆盖。

## 冻结的最小 Python API

当前只对以下入口的参数名称、关键字约束和返回类型做 contract fixture：

- `LocalControlPlane.prepare`、`LocalControlPlane.inspect`
- `EffectJournal.inspect`、`EffectJournal.reconcile`、`EffectJournal.reset`
- `UserTaskModule.status`
- `ResidentCoordinator.inspect_job`

破坏这些签名的变更必须显式升级 `tests/fixtures/contracts/public-api-v1.json`，并在发布说明中给出迁移方式。`grapheng.__all__` 中的其余对象仍可供试验使用，但在 `0.0.x` 阶段不承诺签名稳定。

## 冻结的最小 CLI JSON

`agent-os orca-effect inspect ...` 的顶层 JSON 对象与 `effects` 数组已冻结。CLI 的人类可读输出、诊断文本、退出错误文案和未列出的命令投影仍可变化；自动化调用方应只依赖带 `--json` 的已记录输出或本页明确列出的 JSON 契约。
