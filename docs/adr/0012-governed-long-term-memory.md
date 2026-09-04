# ADR 0012：受治理的跨 Session 长期记忆

- 状态：Accepted
- 日期：2026-09-04

## 背景

短期上下文只覆盖同一 Session。旅行偏好需要在新 Session 中复用，但模型从聊天自动推断出的内容可能是临时条件、错误事实或提示注入，不能直接成为永久用户画像。系统还必须支持用户查看、纠正、删除，并保证跨用户隔离。

## 决策

1. 使用 `MemoryService` 隔离 Agent 与具体记忆框架。PostgreSQL `travel_memories` 业务表是记忆生命周期、来源和审计的事实来源；LangGraph `PostgresStore` 是跨 Session 召回索引，不接管 User、Session、Message 或 Run。
2. LangGraph Store namespace 固定包含 `user_id`。API 的查询、修改和删除同时按当前用户过滤；未知或他人 UUID 统一返回 404。
3. 表单中提取的兴趣、预算和补充偏好默认保存为 `CANDIDATE`。只有 `CONFIRMED` 且未过期的记忆可进入 Agent 上下文；明确的“请记住/以后/我喜欢/不喜欢”表达可直接确认。
4. 自动提取采用确定性、保守规则，不新增 LLM 调用。以后引入模型提取时仍必须保留候选状态、来源、置信度、去重与用户确认。
5. 删除是软删除墓碑，并同步删除 Store 项。自动提取不能静默复活用户删除的同一规范化记忆。
6. 召回记忆有独立 Token 预算与条数上限。注入内容被标记为背景偏好，不具备系统指令权限，当前用户请求优先。
7. Store 表通过一次性 `python -m app.memory_setup` 显式初始化。API 和 Worker 启动不隐式修改数据库 Schema。
   Alembic 的 autogenerate 明确忽略 LangGraph 自管理的 `store` 和 `store_migrations`，避免把框架表误判成应删除的业务漂移。
8. SSE 和 Trace 只记录召回数量与 Token，不传输记忆正文。用户通过同源、Cookie 鉴权和 CSRF 保护的 REST API 管理记忆。

## 后果

- 新 Session 可以复用稳定旅行偏好，同时避免一次性表单输入未经确认就长期生效。
- 业务表和 Store 存在双写；Store 同步失败时以业务表回退，后续需要补充异步修复和同步失败告警。
- 当前召回先同步已确认业务记录，再使用 LangGraph Store 检索并做本地相关性排序，数据量增大后应增加 embedding 索引和离线召回评测。
- 删除墓碑会占用少量存储，但防止用户删除内容被自动重建。

## 验证

单元测试覆盖候选/确认规则、幂等提取、删除墓碑、跨 Session 召回、ContextUsage 和 API 用户隔离。PostgreSQL 集成测试真实执行 `PostgresStore.setup()`，验证 user namespace、召回和删除。
