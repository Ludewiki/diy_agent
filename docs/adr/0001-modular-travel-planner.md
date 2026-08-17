# ADR 0001：按职责拆分旅行规划工具

- 状态：Accepted
- 日期：2026-08-17

## 背景

原 `travel_planner_tool.py` 同时承担 Schema、第三方数据访问、内容解析、评分、聚类、路径计算、天气分配和 LangChain Tool 适配，接近 1400 行。任何一层变化都会扩大回归范围，也难以在不联网的情况下测试算法。

## 决策

建立 `travel_planner` 包，分为 `schemas`、`sources`、`scoring`、`clustering`、`routing`、`weather_assignment`、`service` 和 `tool`。根目录的 `travel_planner_tool.py` 只作为兼容导出层。

领域算法保持确定性；LLM 只负责识别意图、调用工具和组织答案。第三方客户端允许注入 HTTP Session，便于后续契约测试。

## 后果

- 纯算法可以独立测试，不需要 API Key。
- 数据源和地图供应商可以替换，不必改动 Agent 提示词。
- 兼容层需要在未来一个明确的主版本中再决定是否移除。
