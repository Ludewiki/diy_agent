# ADR 0011：版本化 Run 结果与 SSE 职责分离

- 状态：Accepted
- 日期：2026-09-03

## 背景

旧版 `AgentRun.output_json` 只在成功结束时保存 `answer` 和 `reference`。天气窗口、路线和 Tool 降级信息主要存在持久化 SSE Event 中，页面刷新后必须重放并解析事件才能恢复；如果天气成功而路线失败，正式 Run 查询无法返回已经完成的天气成果。多轮 Session 还需要区分规划版本，并说明哪些组件沿用上一成功版本。

## 决策

1. `GET /v1/runs/{run_id}` 始终返回 `RunResultV1`。结果包括 `schema_version`、`result_status`、生成时间、规划版本、被替代 Run、输入快照、Assistant 回答、天气、行程、结构化来源/警告、组件状态和 ContextUsage。
2. 创建 Run 时立即写入部分结果。`plan_revision` 是 Session 内 Run 的顺序，`supersedes_run_id` 指向上一次成功 Run。
3. 新 Run 以被替代 Run 的天气、路线、来源和警告作为可恢复基线，并通过 `inherited_from_run_id` 明确标记。对应 Tool 本次成功后替换继承组件和标记。
4. Worker 在保存 `CONTEXT_PREPARED` 或 Tool 完成事件时，在同一数据库事务中更新 `output_json`。租约和 fencing 校验同时约束 Event 与结果，失去租约的 Worker 不能写入陈旧成果。
5. Tool 原始输出只经过显式白名单后才能进入结果。SSE Event 不再携带 Tool 结果正文，只携带 Tool 名和生命周期状态；前端收到通知后查询 Run 正式结果。
6. 失败、取消和最大尝试次数耗尽时保留已有组件并写入结构化错误。`result_status=failed` 描述 Run 结局，组件状态说明哪些部分仍可展示。
7. 没有 `schema_version` 的历史 `answer/reference` 在读取时转换为 V1，不原地改写历史 JSON，也不要求数据库迁移。

## 后果

- 页面刷新和失败恢复不再依赖 SSE 事件正文或自然语言解析。
- 每个规划版本都是可独立查询的快照，同时能区分本次计算和沿用结果。
- `output_json` 继续使用现有 JSON/JSONB 列，本次变更不新增数据库表或迁移。
- SSE 带宽和敏感数据暴露面缩小；正式结果仍必须保持字段白名单和用户所有权校验。
- Result Schema 后续破坏性变更必须增加新的 `schema_version` 和兼容读取逻辑。

## 验证

测试覆盖完整成功结果、输入快照、版本继承、Tool 正文不进入 SSE、部分失败保留天气、取消结果、legacy 输出升级、Worker fencing 以及跨用户 Run 隔离。
